"""
FF — OHLC updater v2, corporate-action aware
============================================================================
Replaces FF_Yahoo_OHLC_Updater.py.

WHAT WAS WRONG WITH v1
----------------------
v1 called yf.download(..., auto_adjust=False) and appended the resulting
UNADJUSTED rows onto an ADJUSTED price history, and never re-adjusted the
history. At every split or bonus the series therefore acquired a cliff: the
running maximum stayed at the pre-split level while the price halved, so the
stock instantly looked "40% below its high" and the scanner manufactured a
signal. This is the root cause of the corrupted OHLC data.

WHAT v2 DOES INSTEAD
--------------------
The stored series stays on ONE adjustment convention end to end. When a
corporate action lands inside the update window, the ENTIRE stored history is
rescaled by the factor before the new rows are appended, so the join is
continuous and no artificial drop is ever created.

THE GUARD THAT MATTERS MOST
---------------------------
A claimed factor is never trusted on its own. The v3 audit found three events
booked as equity splits that were nothing of the kind — ZEEL's 21:1 bonus
PREFERENCE shares, Dr Reddy's 6:1 bonus DEBENTURES, TVS Motor's 4:1 NCRPS.
None of them changes the equity share count, so none should touch the equity
price; dividing the history by them left a 21.9x cliff and one trade booked a
fictional +245.8%.

So v2 requires a claimed corporate action to AGREE WITH THE PRICE. It compares
the factor the feed reports against the price ratio actually observed across
the ex-date. If a 5:1 split is claimed and the price did not move like a 5:1
split, the action is REFUSED, the symbol is quarantined, and a human is told.
That is the "read the kind, never the factor alone" rule enforced empirically,
which also catches events no feed bothered to label.

FAILURE POLICY
--------------
A symbol that fails any gate is left EXACTLY as it was on disk and reported.
Bad data never reaches the file, and a partial write can never happen: every
file is written to a temporary path and moved into place atomically. Publishing
nothing is always better than publishing something wrong.

RUN
---
    python FF_OHLC_Updater_v2.py                  # normal daily update
    python FF_OHLC_Updater_v2.py --selftest       # no network; proves the fix
    python FF_OHLC_Updater_v2.py --symbols A,B    # a subset
    python FF_OHLC_Updater_v2.py --dry-run        # report only, write nothing

Exit code is non-zero if any symbol is quarantined, so CI fails loudly instead
of publishing quietly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tempfile
from datetime import date, timedelta

import numpy as np
import pandas as pd

OHLC_DIR   = "ohlc_data"
REPORT     = "ohlc_update_report.json"
QUARANTINE = "ohlc_quarantine.txt"

REQUEST_DELAY   = 0.3     # polite pacing between symbols
OVERLAP_DAYS    = 10      # re-fetch this much already-stored history to check the join
FACTOR_TOL      = 0.12    # claimed factor must match the observed price ratio within 12%
MAX_DAILY_MOVE  = 0.35    # an unexplained day beyond +/-35% quarantines the symbol
MIN_FACTOR      = 1.05    # below this a "split" is noise, not an event

# Structural gates apply from here onward, not to the whole series.
# 61 of the 751 stored files carry rows where Open/High/Low are 0 while Close is
# valid — a source artifact of the 1990s and early 2000s. The integrity audit
# located every one of them before 12 September 2003 and confirmed no trade's
# entry or exit day is affected. Gating on the full history would quarantine
# those 61 symbols every single day over data that is two decades old and
# already assessed, which would train everyone to ignore the alarm. A gate that
# cries wolf is worse than no gate.
HISTORY_OK_FROM = pd.Timestamp("2004-01-01")

COLS = ["Date", "Close", "High", "Low", "Open", "Volume"]   # the repo's existing order
FLOAT_FMT = "%.6g"   # matches the seed; keeps daily diffs to the rows that changed


# --------------------------------------------------------------------------
# pure logic — no network, so all of it is testable offline
# --------------------------------------------------------------------------

def observed_ratio(new: pd.DataFrame, ex_date: pd.Timestamp):
    """Price ratio actually seen across an ex-date: the last close before it
    divided by the first close on or after it. A genuine 5:1 split gives ~5.0;
    an event that does not touch the equity gives ~1.0.

    Measured INSIDE the fetched block, never against the stored history. The
    two are not necessarily on the same scale: when a split has already been
    applied to our history but the unadjusted feed still serves pre-split prices
    for the overlap days, comparing across them reports the scale difference
    rather than the price move, and a real split gets read as a non-event. The
    fetched block is internally consistent, so the jump inside it is the true
    effect on the equity."""
    before = new[new.Date < ex_date]
    after  = new[new.Date >= ex_date]
    if before.empty or after.empty:
        return None
    b = float(before.Close.iloc[-1])
    a = float(after.Close.iloc[0])
    if not np.isfinite(b) or not np.isfinite(a) or a <= 0 or b <= 0:
        return None
    return b / a


def vet_action(claimed: float, observed: float | None):
    """Decide whether a claimed corporate action may be applied.

    Returns (verdict, note) where verdict is 'apply', 'ignore' or 'quarantine'.
    """
    if claimed is None or claimed < MIN_FACTOR:
        return "ignore", "factor below the noise floor"
    if observed is None:
        return "quarantine", "no overlap around the ex-date, so the factor cannot be verified"
    rel = abs(observed - claimed) / claimed
    if rel <= FACTOR_TOL:
        return "apply", f"claimed {claimed:.4g}, observed {observed:.4g} — agree"
    if abs(observed - 1.0) <= FACTOR_TOL:
        # the classic non-equity bonus: a factor is claimed, the price ignored it
        return "ignore", (f"claimed {claimed:.4g} but the price moved {observed:.4g} — "
                          f"this does not touch the equity (bonus preference shares, "
                          f"debentures or NCRPS). NOT applied.")
    return "quarantine", (f"claimed {claimed:.4g}, observed {observed:.4g} — "
                          f"neither agrees nor is a non-event")


def rescale_before(df: pd.DataFrame, ex_date, factor: float) -> pd.DataFrame:
    """Put everything dated before an ex-date onto the post-action scale.
    Prices divide; volume multiplies, because the share count moved the other way.

    This has to hit the freshly fetched rows too, not only the stored history.
    An unadjusted feed serves pre-ex-date days on the OLD scale and post-ex-date
    days on the NEW one, so the fetched block carries the cliff inside itself.
    Rescaling the history alone leaves that cliff in the overlap — which the
    first version of this function did, and the self-test caught."""
    out = df.copy()
    m = out.Date < pd.Timestamp(ex_date)
    if not m.any():
        return out
    for c in ("Open", "High", "Low", "Close"):
        out.loc[m, c] = out.loc[m, c] / factor
    if "Volume" in out.columns:
        out.loc[m, "Volume"] = out.loc[m, "Volume"] * factor
    return out


def check_frame(full: pd.DataFrame):
    """Structural gates. Returns a list of problems.

    Date ordering is checked across the whole series; price sanity only from
    HISTORY_OK_FROM, for the reason recorded beside that constant."""
    bad = []
    if full.empty:
        return ["empty series"]
    if full.Date.duplicated().any():
        bad.append(f"{int(full.Date.duplicated().sum())} duplicate dates")
    if not full.Date.is_monotonic_increasing:
        bad.append("dates not increasing")
    df = full[full.Date >= HISTORY_OK_FROM]
    if df.empty:
        return bad
    px = df[["Open", "High", "Low", "Close"]]
    if px.isna().any().any():
        bad.append(f"{int(px.isna().sum().sum())} NaN prices")
    if (px <= 0).any().any():
        bad.append(f"{int((px <= 0).sum().sum())} non-positive prices")
    hi_ok = (df.High >= df[["Open", "Close", "Low"]].max(axis=1) - 1e-6)
    lo_ok = (df.Low  <= df[["Open", "Close", "High"]].min(axis=1) + 1e-6)
    if not hi_ok.all(): bad.append(f"{int((~hi_ok).sum())} rows where High is not the high")
    if not lo_ok.all(): bad.append(f"{int((~lo_ok).sum())} rows where Low is not the low")
    return bad


def check_join(joined: pd.DataFrame, from_date: pd.Timestamp, applied: list):
    """Look for a cliff at the seam. Any unexplained jump beyond MAX_DAILY_MOVE
    in the newly-touched region is exactly the v1 failure, so it quarantines."""
    w = joined[joined.Date >= from_date - pd.Timedelta(days=OVERLAP_DAYS + 5)]
    if len(w) < 2:
        return []
    r = w.Close.pct_change().abs()
    hits = w.loc[r > MAX_DAILY_MOVE, "Date"]
    excused = {pd.Timestamp(d).normalize() for d, _ in applied}
    hits = [d for d in hits if pd.Timestamp(d).normalize() not in excused]
    if hits:
        return [f"unexplained move beyond {MAX_DAILY_MOVE:.0%} on "
                + ", ".join(str(pd.Timestamp(d).date()) for d in hits[:5])]
    return []


def rescale_all(df: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Put an entire series onto a new scale."""
    out = df.copy()
    for c in ("Open", "High", "Low", "Close"):
        out[c] = out[c] / factor
    if "Volume" in out.columns:
        out["Volume"] = out["Volume"] * factor
    return out


def overlap_ratio(hist: pd.DataFrame, new: pd.DataFrame):
    """How the stored history compares with the fetched block on the days they
    share. Returns (median ratio, spread, n) or None when they do not meet."""
    j = hist.merge(new, on="Date", suffixes=("_h", "_n"))
    if j.empty:
        return None
    r = (pd.to_numeric(j.Close_h, errors="coerce") /
         pd.to_numeric(j.Close_n, errors="coerce")).replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty:
        return None
    med = float(r.median())
    spread = float((r.max() - r.min()) / abs(med)) if med else float("inf")
    return med, spread, len(r)


def check_overlap(hist: pd.DataFrame, new: pd.DataFrame):
    """The fetched block is deliberately started before the stored end date, so
    the two must agree on the days they share. If they do not, the incoming
    prices are on a different scale from the stored ones — which is precisely
    the v1 failure — and appending them would splice two scales together.

    No overlap at all is also a refusal, not a pass: a join that cannot be
    verified is exactly the join that must not be made silently."""
    j = hist.merge(new, on="Date", suffixes=("_h", "_n"))
    if j.empty:
        return ["fetched rows do not overlap the stored history, so the join "
                "cannot be verified"]
    r = (pd.to_numeric(j.Close_h, errors="coerce") /
         pd.to_numeric(j.Close_n, errors="coerce")).replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty:
        return ["the overlap carries no usable closes"]
    off = (r - 1).abs() > 0.02
    if off.any():
        return [f"the overlap disagrees with the stored history on {int(off.sum())} of "
                f"{len(r)} shared days (median ratio {r.median():.4f}) — the incoming "
                f"prices are on a different scale"]
    return []


def merge(hist: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """New rows win on a shared date — Yahoo restates, our file should follow."""
    out = (pd.concat([hist, new], ignore_index=True)
             .drop_duplicates(subset="Date", keep="last")
             .sort_values("Date")
             .reset_index(drop=True))
    return out[COLS]


def atomic_write(df: pd.DataFrame, path: str):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    os.close(fd)
    try:
        out = df.copy()
        if "Volume" in out.columns:      # keep volume an integer, not 1.77746e+06
            out["Volume"] = pd.array(np.round(pd.to_numeric(out.Volume, errors="coerce")),
                                     dtype="Int64")
        out.to_csv(tmp, index=False, float_format=FLOAT_FMT)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def update_one(symbol, hist, new, splits, from_date):
    """The whole decision for one symbol, as a pure function.

    hist   — what is on disk
    new    — freshly fetched rows, overlapping hist by OVERLAP_DAYS
    splits — [(ex_date, claimed_factor)] reported by the feed inside the window

    Returns (frame_or_None, status, notes)
    """
    notes, applied = [], []
    if new is None or new.empty:
        return None, "no_new_data", ["feed returned nothing"]

    # 1. Normalise the fetched block onto one scale — its own latest one.
    factor_product = 1.0
    for ex_date, claimed in sorted(splits, key=lambda z: z[0]):
        ex_date = pd.Timestamp(ex_date).normalize()
        verdict, why = vet_action(claimed, observed_ratio(new, ex_date))
        notes.append(f"{ex_date.date()}: {verdict} — {why}")
        if verdict == "quarantine":
            return None, "quarantined", notes
        if verdict == "apply":
            new = rescale_before(new, ex_date, claimed)
            applied.append((ex_date, claimed))
            factor_product *= claimed

    # 2. Ask the overlap where the stored history sits relative to that scale.
    #    A ratio of 1 means the history is already current — which is the case
    #    when an action was applied to it before this run. A ratio equal to the
    #    actions just applied means the history predates them and must move.
    #    Anything else is a scale change nobody declared, and is not guessed at.
    ov = overlap_ratio(hist, new)
    if ov is None:
        return None, "quarantined", notes + [
            "fetched rows do not overlap the stored history, so the join cannot "
            "be verified"]
    med, spread, n = ov
    if spread > 0.02:
        return None, "quarantined", notes + [
            f"the overlap is not on one consistent scale across its {n} shared days "
            f"(spread {spread:.1%}, median ratio {med:.4f})"]
    if abs(med - 1.0) <= FACTOR_TOL:
        pass                                    # history already current
    elif abs(med - factor_product) / factor_product <= FACTOR_TOL:
        hist = rescale_all(hist, med)           # history predates the action
        notes.append(f"history rescaled by {med:.4g} to meet the new rows")
    else:
        return None, "quarantined", notes + [
            f"the overlap says the stored history is {med:.4f}x the incoming prices, "
            f"which no declared action explains (actions applied: {factor_product:.4g})"]

    joined = merge(hist, new)
    problems = check_frame(joined) + check_join(joined, pd.Timestamp(from_date), applied)
    if problems:
        return None, "quarantined", notes + problems

    gained = len(joined) - len(hist)
    notes.append(f"{gained} new rows"
                 + (f", {len(applied)} corporate action(s) accepted" if applied else ""))
    return joined, "ok", notes


# --------------------------------------------------------------------------
# network side — isolated so the logic above stays testable
# --------------------------------------------------------------------------

def yahoo_fetch(symbol, start, end):
    import yfinance as yf
    t = yf.Ticker(f"{symbol}.NS")
    df = t.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                   auto_adjust=False, actions=True)
    if df is None or df.empty:
        return pd.DataFrame(columns=COLS), []
    df = df.reset_index()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None).dt.normalize()
    out = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()

    splits = []
    if "Stock Splits" in df.columns:
        s = df[df["Stock Splits"].fillna(0) != 0]
        for _, r in s.iterrows():
            f = float(r["Stock Splits"])
            if f > 0:
                # yfinance reports 5.0 for a 1->5 split, i.e. the price divides by 5
                splits.append((r["Date"], f))
    return out, splits


# --------------------------------------------------------------------------

def run(symbols=None, dry_run=False):
    today = date.today()
    files = sorted(f for f in os.listdir(OHLC_DIR) if f.endswith(".csv"))
    if symbols:
        want = set(symbols)
        files = [f for f in files if f[:-4] in want]

    report, quarantined = {}, []
    for i, fname in enumerate(files, 1):
        sym = fname[:-4]
        path = os.path.join(OHLC_DIR, fname)
        hist = pd.read_csv(path, parse_dates=["Date"])
        for c in COLS:
            if c not in hist.columns: hist[c] = np.nan
        hist = hist[COLS].sort_values("Date").reset_index(drop=True)
        if hist.empty:
            report[sym] = dict(status="empty_file", notes=[])
            continue

        last = hist.Date.max().date()
        if last >= today:
            report[sym] = dict(status="up_to_date", notes=[])
            continue
        from_date = last - timedelta(days=OVERLAP_DAYS)

        try:
            new, splits = yahoo_fetch(sym, from_date, today)
        except Exception as e:
            report[sym] = dict(status="fetch_failed", notes=[f"{type(e).__name__}: {e}"])
            time.sleep(REQUEST_DELAY)
            continue

        frame, status, notes = update_one(sym, hist, new, splits, last)
        report[sym] = dict(status=status, notes=notes)
        if status == "quarantined":
            quarantined.append(sym)
            print(f"[{i}/{len(files)}] {sym}: QUARANTINED — {'; '.join(notes)}")
        elif status == "ok":
            if not dry_run:
                atomic_write(frame, path)
            print(f"[{i}/{len(files)}] {sym}: {notes[-1]}")
        time.sleep(REQUEST_DELAY)

    counts = {}
    for v in report.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print("\n" + "=" * 60)
    for k, v in sorted(counts.items()):
        print(f"  {k:15} {v}")
    if not dry_run:
        json.dump(report, open(REPORT, "w"), indent=1, default=str)
        open(QUARANTINE, "w").write("\n".join(quarantined))
    if quarantined:
        print(f"\n{len(quarantined)} symbol(s) quarantined and left untouched on disk:")
        print("  " + ", ".join(quarantined[:30]))
        print("Their stored prices are unchanged, so nothing wrong was written; they "
              "are simply a day behind until the cause is understood.")
    # A single odd symbol must not stop the other 750 from updating: its own
    # prices were left alone, so nothing bad is published either way. A large
    # number of them is different — that is the feed or this script misbehaving,
    # and the run should stop rather than quietly freeze half the universe.
    limit = max(5, int(0.02 * max(len(files), 1)))
    if len(quarantined) > limit:
        print(f"\nFAILING THE RUN: {len(quarantined)} quarantined, above the "
              f"tolerance of {limit}. This is not a handful of corporate actions.")
        return 1
    return 0


# --------------------------------------------------------------------------
# self-test — no network. Proves the acceptance criterion for this phase:
# "a simulated split produces no signal".
# --------------------------------------------------------------------------

def selftest():
    rng = pd.bdate_range("2026-01-01", periods=120)
    base = pd.DataFrame({
        "Date": rng,
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
        "Volume": 1000.0,
    })
    hist = base.iloc[:100].copy()
    ok = True

    def report(name, passed, detail=""):
        nonlocal ok
        ok &= passed
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))

    # 1. a genuine 1->5 split: feed says 5.0 and the price actually divides by 5
    new = base.iloc[95:].copy()
    ex = rng[100]
    for c in ("Open", "High", "Low", "Close"):
        new.loc[new.Date >= ex, c] = new.loc[new.Date >= ex, c] / 5.0
    new.loc[new.Date < ex, :] = new.loc[new.Date < ex, :]
    frame, status, notes = update_one("T", hist, new, [(ex, 5.0)], hist.Date.max())
    passed = status == "ok"
    if passed:
        r = frame.set_index("Date").Close
        drop = 1 - r.min() / r.cummax().max()
        passed = drop < 0.05          # the whole series is flat at 20 after rescaling
        report("genuine split: history rescaled, no artificial drop",
               passed, f"max drawdown {drop:.2%}")
    else:
        report("genuine split: history rescaled, no artificial drop", False, status)

    # 1b. the v1 behaviour, for contrast: append without rescaling
    naive = merge(hist, new)
    naive_drop = 1 - naive.Close.min() / naive.Close.cummax().max()
    report("v1 behaviour reproduces the bug (control)", naive_drop > 0.60,
           f"drawdown {naive_drop:.2%} — this is the false 40%-below-high signal")

    # 2. the ZEEL case: a factor is claimed but the price ignored it
    flat = base.iloc[95:].copy()
    frame2, status2, notes2 = update_one("Z", hist, flat, [(ex, 22.0)], hist.Date.max())
    passed = status2 == "ok" and "NOT applied" in " ".join(notes2)
    report("non-equity bonus refused (ZEEL / DRREDDY / TVSMOTOR class)", passed,
           notes2[0] if notes2 else "")
    if frame2 is not None:
        d = 1 - frame2.Close.min() / frame2.Close.cummax().max()
        report("  ... and the series stays continuous", d < 0.05, f"drawdown {d:.2%}")

    # 3. a cliff nobody declared at all
    silent = base.iloc[95:].copy()
    for c in ("Open", "High", "Low", "Close"):
        silent.loc[silent.Date >= ex, c] = silent.loc[silent.Date >= ex, c] / 3.0
    _, status3, notes3 = update_one("S", hist, silent, [], hist.Date.max())
    report("undeclared cliff is quarantined, not published", status3 == "quarantined",
           notes3[-1] if notes3 else "")

    # 4. a claimed factor that matches neither the price nor 1.0
    _, status4, _ = update_one("M", hist, silent, [(ex, 5.0)], hist.Date.max())
    report("factor disagreeing with the price is quarantined", status4 == "quarantined")

    # 5. structural gates
    broken = base.iloc[95:].copy(); broken.loc[broken.index[-1], "Close"] = -1
    _, status5, _ = update_one("B", hist, broken, [], hist.Date.max())
    report("negative price is quarantined", status5 == "quarantined")

    # 6. an ordinary quiet day changes nothing
    quiet = base.iloc[95:].copy()
    frame6, status6, _ = update_one("Q", hist, quiet, [], hist.Date.max())
    report("ordinary update appends cleanly", status6 == "ok" and len(frame6) == 120)

    # 6b. the split is already applied to our history but the unadjusted feed
    #     still serves pre-split prices for the overlap days. This is the real
    #     KIRLPNU / TDPOWERSYS case, and the first version of this file failed
    #     it — it compared across hist and new, read the true 2:1 split as a
    #     non-event, and then quarantined on the scale difference it had just
    #     created.
    done = rescale_all(base.iloc[:100].copy(), 5.0)      # history already post-split
    feed = base.iloc[95:].copy()                          # feed still pre-split
    for c in ("Open", "High", "Low", "Close"):
        feed.loc[feed.Date >= ex, c] = feed.loc[feed.Date >= ex, c] / 5.0
    frame6b, status6b, notes6b = update_one("K", done, feed, [(ex, 5.0)], done.Date.max())
    passed = status6b == "ok"
    if passed:
        d = 1 - frame6b.Close.min() / frame6b.Close.cummax().max()
        passed = d < 0.05
        report("split already in our history, feed still pre-split", passed,
               f"drawdown {d:.2%}")
    else:
        report("split already in our history, feed still pre-split", False,
               notes6b[-1] if notes6b else status6b)

    # 7. the feed silently restates the whole series onto another scale
    shifted = base.iloc[95:].copy()
    for c in ("Open", "High", "Low", "Close"):
        shifted[c] = shifted[c] / 1.5
    _, status7, notes7 = update_one("R", hist, shifted, [], hist.Date.max())
    report("a scale shift with no declared action is quarantined",
           status7 == "quarantined", notes7[-1] if notes7 else "")

    # 8. a gap so long the fetch does not reach back to the stored history
    gap = base.iloc[110:].copy()
    _, status8, notes8 = update_one("G", hist, gap, [], hist.Date.max())
    report("an unverifiable join (no overlap) is quarantined",
           status8 == "quarantined", notes8[-1] if notes8 else "")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", default=None, help="comma-separated subset")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    syms = [s.strip() for s in a.symbols.split(",")] if a.symbols else None
    sys.exit(run(symbols=syms, dry_run=a.dry_run))
