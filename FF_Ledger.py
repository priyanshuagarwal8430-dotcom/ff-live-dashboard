"""
FF — append-only signal ledger and exit engine
============================================================================

THE DEFECT THIS EXISTS TO REMOVE
--------------------------------
The scanner rebuilds every signal from scratch on each run and overwrites its
output file. A recommendation already issued can therefore change or disappear
when the data underneath it moves. That is not a theoretical risk: on 4
September 2026 SUNTV vanished from the published list and GODREJCP appeared with
a trigger date three weeks old, with no market event behind either. A research
analyst's record cannot behave like that.

WHAT THIS GUARANTEES
--------------------
Once a signal is written it is never rewritten. Its entry fields are fixed at
first publication:

    signal_id, symbol, market_type, trigger_date, entry_date, entry_price,
    target_ath, trigger_close, drawdown_pct, first_published, record

Exit fields are blank until the exit happens, filled once, and then also fixed:

    exit_date, exit_price, exit_reason, status

The engine may only APPEND rows and FILL blank exit fields. Anything else is a
defect and the run says so instead of doing it. If the scanner stops producing a
signal that is already in the ledger, the ledger keeps it and reports the
disagreement — the published record is the ledger, not the scanner.

`record` is "reconstructed" for signals dated before this ledger first ran, and
"live" for ones observed on the day they triggered. The distinction is kept
because a reconstructed signal was never actually published at the time, and
presenting it as though it were would be a claim about a track record that did
not exist.

EXITS
-----
The all-time-high close as at the trigger date, fixed then and never moved, or
the 1,095-day cap. No stop loss. Exactly the backtest's rules, using the same
price files.

    python FF_Ledger.py                 # daily: append new, fill exits
    python FF_Ledger.py --dry-run
"""

from __future__ import annotations

import argparse, os, sys
import numpy as np, pandas as pd

from FF_Signal_Engine_v2 import (
    OHLC_DIR, CAP_DAYS, N500, MICRO, build, data_end, load_prices,
)

LEDGER      = "signal_ledger.csv"
WATCH       = "watchlist_ledger.csv"
RECORD_FROM = "2026-06-01"          # the live record begins here, by decision

FIXED = ["symbol", "market_type", "trigger_date", "entry_date", "entry_price",
         "target_ath", "trigger_close", "drawdown_pct", "first_published", "record"]
EXITF = ["exit_date", "exit_price", "exit_reason", "status"]
COLS  = ["signal_id"] + FIXED + EXITF


def sid(symbol, trigger_date):
    return f"{symbol}@{pd.Timestamp(trigger_date).date()}"


def read_ledger(path):
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLS)
    d = pd.read_csv(path, parse_dates=["trigger_date", "entry_date", "exit_date",
                                       "first_published"])
    for c in COLS:
        if c not in d.columns: d[c] = pd.NA
    return d[COLS]


def find_exit(px, symbol, entry_date, target, end):
    """Target first, then the cap. Returns (date, price, reason) or None while
    the position is still open."""
    d = px.get(symbol)
    if d is None: return None
    j = np.searchsorted(d["dates"], np.datetime64(pd.Timestamp(entry_date)))
    if j >= len(d["dates"]): return None
    cap = pd.Timestamp(entry_date) + pd.Timedelta(days=CAP_DAYS)
    seg = slice(j, len(d["dates"]))
    hit = np.where((d["high"][seg] >= target) &
                   (d["dates"][seg] <= np.datetime64(cap)))[0]
    if len(hit):
        k = j + hit[0]
        return pd.Timestamp(d["dates"][k]), float(target), "ATH_TARGET"
    if pd.Timestamp(end) >= cap:
        m = np.searchsorted(d["dates"], np.datetime64(cap), side="right") - 1
        m = max(m, j)
        return pd.Timestamp(d["dates"][m]), float(d["close"][m]), "TIME_STOP"
    return None


def sync(path, tiers, label, today, end, px, dry):
    led = read_ledger(path)
    known = set(led.signal_id.astype(str))
    first_run = led.empty

    found, _ = build(tiers, end)
    found = found[found.trigger_date >= pd.Timestamp(RECORD_FROM)].copy()
    found["signal_id"] = [sid(s, t) for s, t in zip(found.symbol, found.trigger_date)]

    # ---- appends only -----------------------------------------------------
    fresh = found[~found.signal_id.isin(known)]
    rows = []
    ident_mt = pd.read_csv("ident.csv").set_index("symbol").market_type
    for r in fresh.itertuples():
        # A signal seen on the day it triggered was genuinely published then.
        # One dated earlier is being reconstructed, and must say so.
        live = (not first_run) and (pd.Timestamp(r.trigger_date).date() >=
                                    (today - pd.Timedelta(days=4)).date())
        rows.append({"signal_id": r.signal_id, "symbol": r.symbol,
                     "market_type": ident_mt.get(r.symbol),
                     "trigger_date": r.trigger_date, "entry_date": r.entry_date,
                     "entry_price": round(float(r.entry_price), 4),
                     "target_ath": round(float(r.target_ath), 4),
                     "trigger_close": round(float(r.trigger_close), 4),
                     "drawdown_pct": round(100 * (r.trigger_close / r.target_ath - 1), 2),
                     "first_published": today,
                     "record": "live" if live else "reconstructed",
                     "exit_date": pd.NaT, "exit_price": pd.NA,
                     "exit_reason": pd.NA, "status": "OPEN"})
    if rows:
        led = pd.concat([led, pd.DataFrame(rows)], ignore_index=True)

    # ---- signals the scanner no longer produces ---------------------------
    vanished = sorted(known - set(found.signal_id.astype(str)))

    # ---- fill exits, once -------------------------------------------------
    filled = []
    for i in led.index:
        if str(led.at[i, "status"]) == "CLOSED":
            continue
        ex = find_exit(px, led.at[i, "symbol"], led.at[i, "entry_date"],
                       float(led.at[i, "target_ath"]), end)
        if ex is None: continue
        d, p, why = ex
        led.at[i, "exit_date"] = d
        led.at[i, "exit_price"] = round(p, 4)
        led.at[i, "exit_reason"] = why
        led.at[i, "status"] = "CLOSED"
        filled.append((led.at[i, "signal_id"], str(d.date()), why))

    led = led.sort_values(["trigger_date", "symbol"]).reset_index(drop=True)
    if not dry:
        led[COLS].to_csv(path, index=False)

    op = int((led.status == "OPEN").sum()); cl = int((led.status == "CLOSED").sum())
    print(f"\n{label}")
    print(f"  ledger rows        : {len(led)}   ({op} open, {cl} closed)")
    print(f"  appended this run  : {len(rows)}")
    print(f"  exits filled       : {len(filled)}")
    for f in filled[:15]: print(f"       {f[0]:22} {f[1]}  {f[2]}")
    if vanished:
        print(f"  NOT produced by the scanner any more: {len(vanished)}")
        print("    Kept. The published record is the ledger, not the scanner. Investigate,")
        print("    but never by deleting a row someone may already have acted on:")
        for v in vanished[:15]: print(f"       {v}")
    if not led.empty:
        print(f"  record type        : {led.record.value_counts().to_dict()}")
    return len(vanished)


def main(dry=False):
    end = data_end()
    today = pd.Timestamp.today().normalize()
    print(f"price data ends {end.date()}; ledger record starts {RECORD_FROM}")
    px = load_prices(end=end)
    v1 = sync(LEDGER, N500, "RECOMMENDABLE (Nifty 500)", today, end, px, dry)
    v2 = sync(WATCH, MICRO, "WATCHLIST (Micro Cap)", today, end, px, dry)
    if dry: print("\n(dry run — nothing written)")
    # A vanished signal is a data question, not a reason to fail the pipeline:
    # the ledger already protects the record by keeping the row.
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    sys.exit(main(a.dry_run))
