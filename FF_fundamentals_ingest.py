"""
FF — Quarterly Fundamentals Ingestion
========================================
Takes the raw output of FF_Screener_Fundamentals_Scraper.py (fundamentals_master.csv)
and merges the newest quarter's data into fund_flags.csv — computing whether each
company's latest Net Sales, PAT, and Diluted EPS is a rolling-window high (8Q for
Large/Mid/Small, 4Q for Micro), exactly the same logic used to build the original
validated dataset.

RUN THIS EVERY TIME A NEW QUARTER'S FUNDAMENTALS ARE SCRAPED
----------------------------------------------------------------
1. Run FF_Screener_Fundamentals_Scraper.py (or your Prowess export, converted to
   the same fundamentals_master.csv schema) to get the latest quarter's numbers.
2. Put fundamentals_master.csv in this folder.
3. python fundamentals_ingest.py
4. fund_flags.csv is updated in place — the next fresh_signal_scanner.py run will
   pick up any newly-qualifying stocks.

WHAT "ROLLING HIGH" MEANS HERE
---------------------------------
For each company, take its trailing N quarters (N=8 for Large/Mid/Small Cap, N=4
for Micro Cap) INCLUDING the new quarter just added. If the new quarter's value
is the maximum across that window, it counts as a high for that metric. This
must be computed per-metric (Net Sales, PAT, EPS) — all three must be highs for
"all_hi" (the actual FF entry condition) to be true.

SAFE TO RE-RUN
---------------
If a symbol+quarter combination already exists in fund_flags.csv, it's skipped
(not duplicated) — safe to run this multiple times on the same scraper output.

WHY AVAIL_DATE USES THE SCRAPE DATE, NOT REPORT_DATE+60 (IMPORTANT)
------------------------------------------------------------------------
The original backtest computed avail_date as report_date + 60 days — a proxy
for "how long it typically takes for results to become public," used to
avoid lookahead bias in a HISTORICAL simulation where the exact public
disclosure date per company wasn't tracked.

In LIVE ingestion, that reasoning doesn't apply: by the time this script
runs, the data has already been scraped from a public page — it is
unambiguously public knowledge on the day it was scraped (recorded in
fundamentals_master.csv's pulled_at column). Adding another 60-day delay
on top would double-count a lag that has already, concretely, passed —
costing real entry timing for no bias-prevention benefit.

So: avail_date for newly-ingested rows = the date this data was actually
scraped (pulled_at), not report_date + 60.

CRITICAL SAFETY RULE — never touch original historical rows
-----------------------------------------------------------------
This script adds a "source" column to fund_flags.csv: rows already present
before the FIRST run of this script are marked "historical" (part of the
validated 166-trade backtest — report_date+60 is CORRECT and PERMANENT for
these, and must never change). Rows added BY this script are marked
"live_scrape". Only "live_scrape" rows are ever eligible for the avail_date
correction pass below — historical rows are never touched, no matter what.
(An earlier version of this script did not make this distinction and
incorrectly overwrote avail_date on historical rows too — if you ran that
version, use the accompanying repair script to restore historical rows
before re-running this one.)
"""

import pandas as pd
from datetime import timedelta

SCRAPER_OUTPUT = "fundamentals_master.csv"
FUND_FLAGS = "fund_flags.csv"
IDENT = "ident.csv"
PUBLICATION_LAG_DAYS = 60   # ONLY used as a fallback if pulled_at is missing — see note below

ROLLING_WINDOW = {"Large Cap": 8, "Mid Cap": 8, "Small Cap": 8, "Micro Cap": 4}


def parse_quarter_label(label):
    """
    Screener's quarterly column headers look like 'Mar 2026', 'Jun 2026' etc
    (month-year of quarter END). Converts to that quarter's last calendar date.
    """
    dt = pd.to_datetime(label, format="%b %Y", errors="coerce")
    if pd.isna(dt):
        # try a couple of common alternate formats before giving up
        for fmt in ("%B %Y", "%Y-%m", "%b-%y"):
            dt = pd.to_datetime(label, format=fmt, errors="coerce")
            if not pd.isna(dt):
                break
    if pd.isna(dt):
        return None
    return dt + pd.offsets.MonthEnd(0)


def eps_plausible(pat_cr, eps):
    """
    Sanity check: PAT (crores) / EPS roughly implies shares outstanding (crores).
    A listed Indian company should have at least ~0.1 crore (10 lakh) shares
    outstanding. If the implied share count is far below that, the EPS value
    is almost certainly a scraping error (wrong row/column picked up), not a
    genuine number — flag it rather than silently trusting it.
    Note: this deliberately allows genuinely high-EPS stocks (e.g. MRF, whose
    EPS legitimately runs into four figures because it has never split its
    stock) — the check is on implied share count, not on EPS magnitude itself.
    """
    if pd.isna(pat_cr) or pd.isna(eps) or eps == 0:
        return True  # nothing to check
    implied_shares_cr = abs(pat_cr / eps)
    return implied_shares_cr >= 0.05  # at least 5 lakh shares outstanding


def ingest():
    raw = pd.read_csv(SCRAPER_OUTPUT)
    fund = pd.read_csv(FUND_FLAGS, parse_dates=["report_date", "avail_date"])
    ident = pd.read_csv(IDENT)
    mt = dict(zip(ident["symbol"], ident["market_type"]))

    # Migrate: if fund_flags.csv doesn't have a "source" column yet, every
    # row present right now is by definition from the original historical
    # build — tag it "historical" so it can never be touched by the
    # avail_date-correction pass below, no matter how many times this
    # script runs in the future.
    if "source" not in fund.columns:
        fund["source"] = "historical"
        log_msg = "Migrated fund_flags.csv: added 'source' column, tagged all existing rows as 'historical'."
        print(log_msg)

    raw["report_date"] = raw["quarter_label"].apply(parse_quarter_label)
    raw = raw.dropna(subset=["report_date"])

    new_rows = []
    corrections = []
    flagged_implausible = []
    skipped_existing, skipped_no_market_type, skipped_insufficient_history = 0, 0, 0

    for symbol, g in raw.groupby("symbol"):
        market_type = mt.get(symbol)
        if market_type is None:
            skipped_no_market_type += 1
            continue
        window = ROLLING_WINDOW.get(market_type, 8)

        # take the latest quarter row for this symbol from the scraper output
        latest = g.sort_values("report_date").iloc[-1]
        report_date = latest["report_date"]

        pulled_at_raw = latest.get("pulled_at")
        correct_avail_date = (pd.to_datetime(pulled_at_raw).normalize()
                               if pd.notna(pulled_at_raw)
                               else report_date + timedelta(days=PUBLICATION_LAG_DAYS))

        # If this symbol+quarter already exists, don't re-add it — but DO
        # check whether its avail_date needs correcting. SAFETY: only ever
        # correct rows explicitly tagged "live_scrape" (added by a PREVIOUS
        # run of this exact script) — "historical" rows (the validated
        # backtest) are never touched, regardless of any date mismatch.
        existing = fund[(fund["symbol"] == symbol) & (fund["report_date"] == report_date)]
        if not existing.empty:
            skipped_existing += 1
            # NOTE: previously this block updated avail_date on every re-scrape,
            # even for rows that already had a correct, first-observed date —
            # that pushed avail_date forward each time (to whatever day the
            # re-scrape happened to run), which could push it PAST the latest
            # OHLC date and silently disable a stock's fundamental condition.
            # avail_date must be set ONCE, at first observation, and never
            # touched again. So: existing rows are now always just skipped.
            continue

        # pull this symbol's trailing history from fund_flags.csv, then add the new quarter
        hist = fund[fund["symbol"] == symbol].sort_values("report_date")
        combined_sales = list(hist["net_sales"].tail(window - 1)) + [latest["net_sales_cr"]]
        combined_pat = list(hist["pat"].tail(window - 1)) + [latest["pat_cr"]]
        combined_eps = list(hist["eps"].tail(window - 1)) + [latest["diluted_eps"]]

        if len(combined_sales) < 2:  # not enough history to meaningfully judge a "high"
            skipped_insufficient_history += 1
            continue

        if not eps_plausible(latest["pat_cr"], latest["diluted_eps"]):
            flagged_implausible.append((symbol, report_date.strftime("%Y-%m-%d"),
                                          latest["pat_cr"], latest["diluted_eps"]))
            continue

        sales_hi = latest["net_sales_cr"] == max(x for x in combined_sales if pd.notna(x))
        pat_hi = latest["pat_cr"] == max(x for x in combined_pat if pd.notna(x))
        eps_hi = latest["diluted_eps"] == max(x for x in combined_eps if pd.notna(x))
        all_hi = bool(sales_hi and pat_hi and eps_hi)

        # avail_date = the actual date this data was scraped (already public
        # by definition — see docstring note on why this differs from the
        # backtest's report_date+60 proxy). Falls back to report_date+60
        # only if pulled_at is somehow missing from the scraper output.
        pulled_at_raw = latest.get("pulled_at")
        if pd.notna(pulled_at_raw):
            avail_date = pd.to_datetime(pulled_at_raw).normalize()
        else:
            avail_date = report_date + timedelta(days=PUBLICATION_LAG_DAYS)

        new_rows.append({
            "symbol": symbol,
            "report_date": report_date,
            "net_sales": latest["net_sales_cr"],
            "pat": latest["pat_cr"],
            "eps": latest["diluted_eps"],
            "market_type": market_type,
            "net_sales_hi": bool(sales_hi),
            "pat_hi": bool(pat_hi),
            "eps_hi": bool(eps_hi),
            "all_hi": all_hi,
            "avail_date": avail_date,
            "source": "live_scrape",
        })

    if new_rows or corrections:
        if new_rows:
            new_df = pd.DataFrame(new_rows)
            updated = pd.concat([fund, new_df], ignore_index=True)
        else:
            updated = fund
        updated = updated.sort_values(["symbol", "report_date"])
        updated.to_csv(FUND_FLAGS, index=False)

    print(f"Added {len(new_rows)} new symbol-quarter rows to {FUND_FLAGS}")
    if corrections:
        print(f"Corrected avail_date on {len(corrections)} EXISTING rows "
              f"(old report_date+60 estimate -> actual scrape date):")
        for sym, dt, old, new in corrections[:20]:
            print(f"  {sym} ({dt}): {old} -> {new}")
        if len(corrections) > 20:
            print(f"  ... and {len(corrections)-20} more")
    print(f"Skipped (already present): {skipped_existing}")
    print(f"Skipped (no market_type match in ident.csv): {skipped_no_market_type}")
    print(f"Skipped (insufficient trailing history): {skipped_insufficient_history}")
    if flagged_implausible:
        print(f"\nFLAGGED — implausible EPS (likely scraping error), NOT merged: {len(flagged_implausible)}")
        for sym, dt, pat, eps in flagged_implausible:
            print(f"  {sym} ({dt}): PAT={pat} Cr, EPS={eps} — implausible, needs manual check on Screener")
    if new_rows:
        qualifying = sum(1 for r in new_rows if r["all_hi"])
        print(f"Of the new rows, {qualifying} are fundamentally at a rolling-window high "
              f"(net_sales + PAT + EPS all confirmed) — these are the ones that matter for "
              f"fresh_signal_scanner.py's fundamental condition.")
    print("\nNext step: run fresh_signal_scanner.py to pick up any newly-qualifying stocks, "
          "then build_dashboard.py as usual.")


if __name__ == "__main__":
    ingest()
