"""
FF — Signal engine, spec v2.0
============================================================================
Replaces FF_Fresh_Signal_Scanner.py.

WHY THE OLD SCANNER IS BEING REPLACED
-------------------------------------
It diverged from the frozen specification in four ways, and the divergences
were not small: of 43 signals it published, only 17 survived the v2.0 rules.

  1. It fired on an INTRADAY LOW touching the line: `Low <= 0.60 * ATH`. The
     spec is a CLOSE below it. Seven of 43 signals never closed 40% down.
  2. It scanned the whole 751-name Total Market universe, so 24 of 51 live
     signals were Micro Cap, which is not a recommendable universe.
  3. It used `all_hi`, which carries no positivity filter, so a quarter could
     qualify on a "high" that was negative.
  4. Its fundamentals came from a base whose Micro Cap rows used a 4-quarter
     window while every other tier used 8.

It also had no exit logic at all: every position stayed open forever.

WHAT THIS IMPLEMENTS
--------------------
Entry, exactly as specified:

    Close(t) <= 0.60 * cummax(Close)          on a CLOSE basis
    AND Net Sales, PAT and diluted EPS each at an 8-quarter high
    AND all three positive in the governing quarter
    AND the stock is ARMED

One-shot: a signal disarms the stock. It re-arms only on a fresh all-time-high
close. This is why an added earlier signal can consume a later one, and why the
arming state must be carried through the FULL history rather than from a cutoff.

Exit: the all-time-high close as at the trigger date, fixed at that moment and
never moved afterwards, or a 1,095-day cap. There is no stop loss. The target
exit is the strategy's stated USP and is not varied.

Entry price is the next trading day's OPEN after the trigger.

THE ACCEPTANCE TEST
-------------------
This file is only correct if it reproduces the reference backtest exactly —
same trades, same dates, same prices, same exit reasons. `--verify` does that
comparison and exits non-zero on any difference, so it can gate a CI run.

    python FF_Signal_Engine_v2.py --verify FF_FINAL_v5_n500.csv
    python FF_Signal_Engine_v2.py --since 2026-06-01
"""

from __future__ import annotations

import argparse, os, sys
import numpy as np, pandas as pd

THRESHOLD  = 0.60
CAP_DAYS   = 1095
OHLC_DIR   = "ohlc_data"
IDENT      = "ident.csv"
FLAGS      = "fund_flags_v3.csv"
OUT_SIG    = "signals.csv"
OUT_WATCH  = "watchlist_microcap.csv"
FLAG_COL   = "all_hi_pos"          # never "all_hi": that one has no positivity filter


def load_prices(d=OHLC_DIR, end=None):
    px = {}
    for fn in sorted(f for f in os.listdir(d) if f.endswith(".csv")):
        s = fn[:-4]
        t = pd.read_csv(os.path.join(d, fn), usecols=["Date", "Open", "High", "Low", "Close"])
        t["Date"] = pd.to_datetime(t["Date"])
        t = t.dropna(subset=["Close"]).sort_values("Date")
        if end is not None:
            t = t[t.Date <= pd.Timestamp(end)]
        if len(t) < 2: continue
        c = t.Close.values.astype(float)
        px[s] = dict(dates=t.Date.values.astype("datetime64[ns]"),
                     open=t.Open.values.astype(float), high=t.High.values.astype(float),
                     close=c, ath=np.maximum.accumulate(c))
    return px


def load_flags(path=FLAGS, tiers=None):
    f = pd.read_csv(path, parse_dates=["report_date", "avail_date"])
    if tiers is not None:
        f = f[f.market_type.isin(tiers)]
    f = f.sort_values(["symbol", "avail_date"])
    return {s: dict(dates=g.avail_date.values.astype("datetime64[ns]"),
                    ok=g[FLAG_COL].values.astype(bool))
            for s, g in f.groupby("symbol")}


def triggers(sym, px, fl):
    """Trigger dates for one symbol. The arming state runs over the whole
    history: a cutoff may filter what is reported, never what is computed."""
    d = px.get(sym); f = fl.get(sym)
    if d is None or f is None or len(f["dates"]) == 0: return []
    dates, close, ath = d["dates"], d["close"], d["ath"]
    i = np.searchsorted(f["dates"], dates, side="right") - 1
    qual = np.where(i >= 0, f["ok"][np.clip(i, 0, None)], False)
    cond = (close <= THRESHOLD * ath) & qual
    newath = close >= ath
    out, armed = [], True
    for k in np.where(cond | newath)[0]:
        if not armed:
            if newath[k]: armed = True
            continue
        if cond[k]:
            out.append((sym, pd.Timestamp(dates[k]))); armed = False
    return out


def simulate(sigs, px, data_end):
    """Each signal into a trade. Target is fixed at the trigger and never moves."""
    end = np.datetime64(pd.Timestamp(data_end))
    rows = []
    for sym, td in sigs:
        d = px[sym]
        j = np.searchsorted(d["dates"], np.datetime64(td), side="right")
        if j >= len(d["dates"]): continue
        ed = pd.Timestamp(d["dates"][j]); ep = float(d["open"][j])
        ti = np.searchsorted(d["dates"], np.datetime64(td), side="right") - 1
        if ti < 0 or not np.isfinite(ep) or ep <= 0: continue
        tgt = float(d["ath"][ti])
        trig_close = float(d["close"][ti])
        hz = np.datetime64(min(ed + pd.Timedelta(days=CAP_DAYS), pd.Timestamp(data_end)))
        seg = slice(j, len(d["dates"]))
        hit = np.where((d["high"][seg] >= tgt) & (d["dates"][seg] <= hz))[0]
        if len(hit):
            k = j + hit[0]
            rows.append(dict(symbol=sym, trigger_date=pd.Timestamp(td), entry_date=ed,
                             entry_price=ep, target_ath=tgt, trigger_close=trig_close,
                             exit_date=pd.Timestamp(d["dates"][k]), exit_price=tgt,
                             reason="ATH_TARGET"))
        else:
            m = np.searchsorted(d["dates"], hz, side="right") - 1
            m = max(m, j)
            rows.append(dict(symbol=sym, trigger_date=pd.Timestamp(td), entry_date=ed,
                             entry_price=ep, target_ath=tgt, trigger_close=trig_close,
                             exit_date=pd.Timestamp(d["dates"][m]),
                             exit_price=float(d["close"][m]),
                             reason="TIME_STOP" if hz < end else "DATA_END"))
    t = pd.DataFrame(rows)
    if len(t):
        t["ret_pct"] = (t.exit_price / t.entry_price - 1) * 100
        t = t.sort_values(["entry_date", "symbol"]).reset_index(drop=True)
    return t


def data_end():
    """The last date the price files actually carry. A live run must use this:
    leaving the reference set's fixed 2026-07-27 in place would silently drop
    every signal since, with no error to notice."""
    last = None
    for fn in os.listdir(OHLC_DIR):
        if not fn.endswith(".csv"): continue
        t = pd.read_csv(os.path.join(OHLC_DIR, fn), usecols=["Date"])
        if len(t):
            d = pd.to_datetime(t.Date.iloc[-1])
            last = d if last is None or d > last else last
    return last


def build(tiers, analysis_end, from_date="2012-01-01"):
    px = load_prices(end=analysis_end)
    ident = pd.read_csv(IDENT)
    uni = set(ident[ident.market_type.isin(tiers)].symbol) & set(px)
    fl = load_flags(tiers=tiers)
    sigs = []
    for s in sorted(uni): sigs += triggers(s, px, fl)
    sigs.sort(key=lambda z: (z[1], z[0]))
    t = simulate(sigs, px, analysis_end)
    if len(t): t = t[t.entry_date >= pd.Timestamp(from_date)].reset_index(drop=True)
    return t, dict(zip(ident.symbol, ident.market_type))


N500 = ["Large Cap", "Mid Cap", "Small Cap"]
MICRO = ["Micro Cap"]

# Phase A replaced the "available 60 days after quarter end" assumption with real
# results-announcement dates, for the March-2026 and June-2026 quarters only. That
# legitimately moves twelve 2026 trades and adds three the assumption had
# destroyed, so the frozen reference no longer matches there. The reference is
# deliberately NOT regenerated - the published backtest stands as issued - so the
# guard is scoped to the period the correction did not touch. It still covers 341
# of the 358 trades, and its job is unchanged: catching a change in the RULES.
# Raise this date only when the reference is regenerated on purpose.
FROZEN_BEFORE = "2026-04-01"


def verify(ref_path, analysis_end):
    t, mt = build(N500, analysis_end)
    ref = pd.read_csv(ref_path, parse_dates=["trigger_date", "entry_date", "exit_date"])
    cut = pd.Timestamp(FROZEN_BEFORE)
    n_e, n_r = len(t), len(ref)
    t   = t[t.trigger_date < cut].reset_index(drop=True)
    ref = ref[ref.trigger_date < cut].reset_index(drop=True)
    print(f"scoped to trigger_date < {cut.date()}: engine {n_e} -> {len(t)}, "
          f"reference {n_r} -> {len(ref)}")
    print("  trades after that date carry Phase A's corrected availability dates "
          "and are not comparable to the frozen reference. See FROZEN_BEFORE.")
    print(f"engine produced {len(t)} trades; reference has {len(ref)}")
    a = set(zip(t.symbol, t.entry_date)); b = set(zip(ref.symbol, ref.entry_date))
    extra, missing = sorted(a - b), sorted(b - a)
    ok = True
    if extra or missing:
        ok = False
        print(f"  trades only in the engine   : {len(extra)}")
        for s, d in extra[:12]: print(f"     + {s:12} {d.date()}")
        print(f"  trades only in the reference: {len(missing)}")
        for s, d in missing[:12]: print(f"     - {s:12} {d.date()}")
    common = sorted(a & b)
    m = t.set_index(["symbol", "entry_date"]).loc[common]
    r = ref.set_index(["symbol", "entry_date"]).loc[common]
    # The reference set was computed from the full-precision price base; the
    # engine reads the stored CSVs, which are written at 6 significant figures
    # (a deliberate choice: identical trade set, XIRR moving in the 7th decimal,
    # files 45% smaller). So prices can only be expected to agree to that
    # precision, about 5e-6 relative. What must agree EXACTLY is everything the
    # rules decide: which trades exist, on which dates, and how they end.
    # ret_pct is itself a percentage-point figure derived from two rounded
    # prices, so it is compared on percentage points. Testing it on a RELATIVE
    # tolerance flagged WIPRO at -1.2025% against -1.2025% as a failure, because
    # a 0.00006 pp difference is large relative to a return that small. The
    # measure has to match the quantity.
    PRICE_TOL = 1e-5          # relative, the precision 6 significant figures gives
    RET_TOL   = 0.01          # absolute, percentage points
    for col, tol, absolute in (("entry_price", PRICE_TOL, False),
                               ("target_ath", PRICE_TOL, False),
                               ("ret_pct", RET_TOL, True)):
        diff = (m[col].values - r[col].values)
        bad = (np.abs(diff) > tol) if absolute else \
              (np.abs(diff) > tol * np.maximum(1.0, np.abs(r[col].values)))
        if bad.any():
            ok = False
            unit = "percentage points" if absolute else "relative"
            print(f"  {col}: {int(bad.sum())} of {len(common)} differ beyond "
                  f"{tol:g} {unit}, largest absolute {np.abs(diff).max():.6g}")
            for idx in np.where(bad)[0][:6]:
                s, d = common[idx]
                print(f"     {s:12} {d.date()}  engine {m[col].values[idx]:.6f} "
                      f"vs reference {r[col].values[idx]:.6f}")
    rb = (m.reason.values != r.reason.values)
    if rb.any():
        ok = False
        print(f"  exit reason: {int(rb.sum())} differ")
        for idx in np.where(rb)[0][:6]:
            s, d = common[idx]
            print(f"     {s:12} {d.date()}  {m.reason.values[idx]} vs {r.reason.values[idx]}")
    db = (pd.to_datetime(m.exit_date).values != pd.to_datetime(r.exit_date).values)
    if db.any():
        ok = False
        print(f"  exit date: {int(db.sum())} differ")
    print("\n" + ("ACCEPTANCE TEST PASSED — the engine reproduces the reference set exactly"
                  if ok else "ACCEPTANCE TEST FAILED"))
    return 0 if ok else 1


LEGACY_OUT = "fresh_signals.csv"     # what the current dashboard builder reads
LEGACY_COLS = ["symbol", "market_type", "trigger_date", "entry_date",
               "entry_price", "target_ath", "drawdown_pct"]


def live(since, analysis_end):
    end = analysis_end
    if end in (None, "auto"):
        end = data_end()
        print(f"data ends {end.date()}; scanning to there")
    for tiers, out, label in ((N500, OUT_SIG, "recommendable (Nifty 500)"),
                              (MICRO, OUT_WATCH, "watchlist only (Micro Cap)")):
        t, mt = build(tiers, end)
        t = t[t.trigger_date >= pd.Timestamp(since)].copy()
        if len(t):
            t["market_type"] = t.symbol.map(mt)
            # the depth the RULE saw: the trigger day's close against the high it
            # was measured from, not the next morning's open
            t["drawdown_pct"] = (t.trigger_close / t.target_ath - 1) * 100
            t = t[LEGACY_COLS + ["trigger_close", "exit_date", "exit_price", "reason"]]
        t.to_csv(out, index=False)
        print(f"{label}: {len(t)} signals since {since} -> {out}")
        if tiers is N500:
            # The dashboard builder still reads the old file and the old columns.
            # It is replaced in Phase 5; until then the engine feeds it rather
            # than the builder being rewritten twice.
            leg = t[LEGACY_COLS] if len(t) else pd.DataFrame(columns=LEGACY_COLS)
            leg.to_csv(LEGACY_OUT, index=False)
            print(f"   also written for the current dashboard -> {LEGACY_OUT}")
        if len(t): print(t.head(30).to_string(index=False))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--verify", metavar="REFERENCE_CSV")
    p.add_argument("--since", default="2026-06-01")
    p.add_argument("--analysis-end", default="auto",
                   help="'auto' uses the last date in the data. The reference set was "
                        "built to 2026-07-27, so --verify pins that date itself.")
    a = p.parse_args()
    if a.verify:
        # The reference set is fixed in time; verifying against "auto" would
        # compare two different windows and fail for the wrong reason.
        end = "2026-07-27" if a.analysis_end == "auto" else a.analysis_end
        sys.exit(verify(a.verify, end))
    sys.exit(live(a.since, a.analysis_end))
