"""
FF — fundamentals ingest, v3 basis
============================================================================
Replaces FF_fundamentals_ingest.py.

WHY IT HAD TO BE REPLACED
-------------------------
The old ingest writes `fund_flags.csv` in the pre-v3 format. The signal engine
reads `fund_flags_v3.csv`. Nothing connected the two, so the corrected base was
a frozen snapshot ending Jun-2026: the weekly scraper would have kept running,
writing into a file nothing reads, and the engine would have gone quietly stale
at the next results season. That is the worst kind of failure — no error, no
alarm, just an increasingly out-of-date answer.

Three things change:

  1. It writes fund_flags_v3.csv, with `all_hi_pos`, `eps_reported`,
     `split_factor` and `eps_adj`.
  2. The rolling window is **8 quarters for every tier**. The old file used 4
     for Micro Cap, which made the Micro rows incomparable with the rest.
  3. **EPS is restated when a split happens.** This is the correction v3 was
     built around and the old ingest had no notion of it. EPS is a per-share
     figure: after a 1:5 split the reported EPS drops fivefold, so an
     unrestated pre-split quarter sitting in the trailing window makes it
     arithmetically impossible for EPS to print a new 8-quarter high. The rule
     then suppresses qualifications silently. Left alone, this pipeline would
     have re-created the exact bug the v3 audit found.

WHAT IS AND IS NOT RECOMPUTED
-----------------------------
Nothing is rebuilt from scratch. A control test during the v3 work showed that
recomputing the Prowess-era flags with a from-scratch function reproduces the
stored ones only 91-95% of the time, so a blanket rebuild would rewrite history
under the guise of an update.

Split factors are applied incrementally: each ledger entry is used once and then
marked. Flags are recomputed only where they can actually have changed — the
quarters added by this run, and, when a split restates a quarter's EPS, that
quarter and the seven after it, which are the ones whose eight-quarter window
contains it. Appending a new quarter cannot change an older quarter's flag,
because the window only looks backwards, so older rows are left alone.

A historical row whose flag would differ from its stored value is reported and
not changed.

    python FF_fundamentals_ingest_v3.py
"""

import os, sys
from datetime import timedelta
import numpy as np, pandas as pd

SCRAPER_OUTPUT = "fundamentals_master.csv"
FLAGS          = "fund_flags_v3.csv"
IDENT          = "ident.csv"
CA_LEDGER      = "corporate_actions.csv"     # written by the OHLC updater
LAG_FALLBACK   = 60                          # only if the scrape has no pulled_at
WINDOW         = 8                           # every tier, no exceptions


def parse_quarter(label):
    dt = pd.to_datetime(label, format="%b %Y", errors="coerce")
    if pd.isna(dt):
        for fmt in ("%B %Y", "%Y-%m", "%b-%y"):
            dt = pd.to_datetime(label, format=fmt, errors="coerce")
            if not pd.isna(dt): break
    return None if pd.isna(dt) else dt + pd.offsets.MonthEnd(0)


def eps_plausible(pat_cr, eps):
    """PAT / EPS implies a share count. Below about five lakh shares the EPS is
    almost certainly a mis-scrape rather than a real figure. The test is on the
    implied share count, not on EPS size, so a genuinely high-EPS stock like MRF
    passes."""
    if pd.isna(pat_cr) or pd.isna(eps) or eps == 0: return True
    return abs(pat_cr / eps) >= 0.05


def apply_new_actions(f, ca):
    """Multiply in only the actions that have NOT been applied before.

    The first version of this recomputed split_factor from the ledger every run.
    With no ledger present that produced split_factor = 1 everywhere, which set
    eps_adj back to eps_reported and destroyed the v3 split adjustment on 9,732
    rows in a single pass — silently, with a cheerful summary line. A rebuild
    that can erase a correction is not an update.

    So the stored split_factor is the baseline and is never recomputed. Each
    ledger row is applied at most once and then marked, which makes the run
    idempotent and makes doing nothing the default."""
    if ca is None or ca.empty:
        return f, 0
    if "applied" not in ca.columns:
        ca["applied"] = False
    todo = ca[~ca.applied.fillna(False).astype(bool)]
    n = 0
    for r in todo.itertuples():
        m = (f.symbol == r.symbol) & (f.report_date < pd.Timestamp(r.ex_date))
        if not m.any():
            continue
        f.loc[m, "split_factor"] = f.loc[m, "split_factor"] * float(r.factor)
        n += int(m.sum())
    ca.loc[todo.index, "applied"] = True
    ca.to_csv(CA_LEDGER, index=False)
    return f, n


def hi8(s):
    return s == s.rolling(WINDOW, min_periods=2).max()


def main():
    if not os.path.exists(SCRAPER_OUTPUT):
        print(f"{SCRAPER_OUTPUT} not found — nothing to ingest"); return 0
    f = pd.read_csv(FLAGS, parse_dates=["report_date", "avail_date"])
    raw = pd.read_csv(SCRAPER_OUTPUT)
    mt = dict(zip(*pd.read_csv(IDENT)[["symbol", "market_type"]].values.T))
    ca = None
    if os.path.exists(CA_LEDGER):
        ca = pd.read_csv(CA_LEDGER, parse_dates=["ex_date"])
        print(f"corporate-action ledger: {len(ca)} events across {ca.symbol.nunique()} symbols")
    else:
        print(f"no {CA_LEDGER} — EPS will not be restated for splits this run")

    raw["report_date"] = raw["quarter_label"].apply(parse_quarter)
    raw = raw.dropna(subset=["report_date"])

    have = set(zip(f.symbol, f.report_date))
    new, skipped, implausible = [], 0, []
    for sym, g in raw.groupby("symbol"):
        tier = mt.get(sym)
        if tier is None: continue
        latest = g.sort_values("report_date").iloc[-1]
        rd = latest["report_date"]
        if (sym, rd) in have:
            skipped += 1; continue          # avail_date is set once and never revised
        if not eps_plausible(latest["pat_cr"], latest["diluted_eps"]):
            implausible.append((sym, str(rd.date()), latest["pat_cr"], latest["diluted_eps"]))
            continue
        pulled = latest.get("pulled_at")
        avail = (pd.to_datetime(pulled).normalize() if pd.notna(pulled)
                 else rd + timedelta(days=LAG_FALLBACK))
        new.append(dict(symbol=sym, report_date=rd, net_sales=latest["net_sales_cr"],
                        pat=latest["pat_cr"], eps=np.nan, market_type=tier,
                        net_sales_hi=False, pat_hi=False, eps_hi=False, all_hi=False,
                        avail_date=avail, source="live_scrape",
                        eps_reported=latest["diluted_eps"], split_factor=1.0,
                        eps_adj=np.nan, all_hi_pos=False))

    if new:
        f = pd.concat([f, pd.DataFrame(new)], ignore_index=True)
    f = f.sort_values(["symbol", "report_date"]).reset_index(drop=True)

    # --- restate EPS for any newly recorded split ---------------------------
    before = f.split_factor.copy()
    f, touched = apply_new_actions(f, ca)
    f["eps_adj"] = f.eps_reported / f.split_factor
    f["eps"] = f.eps_adj
    moved = f.index[(before - f.split_factor).abs() > 1e-12]
    print(f"rows restated for a newly recorded split: {len(moved)}")
    if len(moved) and len(moved) > 0.25 * len(f):
        print("REFUSING TO WRITE: a single run would restate more than a quarter of "
              "the file, which is a ledger problem, not a results season.")
        return 1

    # --- recompute flags, narrowly -----------------------------------------
    # A new quarter changes only its own flag: the rolling window looks back,
    # never forward. A restated EPS changes its own quarter and the seven after
    # it, because those are the windows it sits inside. Nothing else.
    added = set()
    for r in new:
        idx = f.index[(f.symbol == r["symbol"]) & (f.report_date == r["report_date"])]
        added.update(idx.tolist())
    affected = set(added)
    for i in moved:
        sym = f.at[i, "symbol"]
        g = f.index[f.symbol == sym]
        pos = list(g).index(i)
        affected.update(list(g)[pos:pos + WINDOW])
    eligible = pd.Series(f.index.isin(sorted(affected)), index=f.index)
    print(f"rows eligible for a flag recompute: {int(eligible.sum())}")
    changed_hist = []
    for sym, g in f.groupby("symbol"):
        ns, pt, ep = hi8(g.net_sales), hi8(g.pat), hi8(g.eps_adj)
        allhi = ns & pt & ep
        pos = allhi & (g.net_sales > 0) & (g.pat > 0) & (g.eps_reported > 0)
        for i in g.index:
            if not eligible.loc[i]:
                if bool(g.all_hi_pos.loc[i]) != bool(pos.loc[i]):
                    changed_hist.append((sym, str(g.report_date.loc[i].date()),
                                         bool(g.all_hi_pos.loc[i]), bool(pos.loc[i])))
                continue
            f.at[i, "net_sales_hi"] = bool(ns.loc[i]); f.at[i, "pat_hi"] = bool(pt.loc[i])
            f.at[i, "eps_hi"] = bool(ep.loc[i]);       f.at[i, "all_hi"] = bool(allhi.loc[i])
            f.at[i, "all_hi_pos"] = bool(pos.loc[i])

    f.to_csv(FLAGS, index=False)
    print(f"\nnew quarters added        : {len(new)}")
    print(f"already present, skipped  : {skipped}")
    print(f"qualifying rows in file   : {int(f.all_hi_pos.sum())} of {len(f)}")
    if implausible:
        print(f"\nEPS implausible, not ingested ({len(implausible)}):")
        for r in implausible[:10]: print("   ", r)
    if changed_hist:
        print(f"\nHISTORICAL rows whose flag would flip if rebuilt: {len(changed_hist)}")
        print("  Left unchanged deliberately — a from-scratch rebuild reproduces the")
        print("  Prowess-era flags only 91-95% of the time, so rebuilding would rewrite")
        print("  history rather than update it. Reported for a human to look at:")
        for r in changed_hist[:15]: print("   ", r)
    print(f"\n-> {FLAGS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
