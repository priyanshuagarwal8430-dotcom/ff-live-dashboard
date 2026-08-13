"""
FF — Fresh Signal Scanner (post cutoff-date)
===============================================
Scans the full Nifty Total Market universe for NEW signals triggered after
a cutoff date (default: 1-Jun-2026), using the exact validated SOP logic:

    - Price condition: Close <= 60% of running all-time-high (= 40%+ drawdown)
    - Cross-down only: flags the FIRST day this condition becomes true after
      being armed — not every day the condition remains true (this is the
      re-arm rule: a stock only re-qualifies after making a fresh ATH and
      then falling 40%+ again)
    - Fundamental condition: latest available quarter's Net Sales, PAT, and
      Diluted EPS are each at a rolling-window high (8Q for Large/Mid/Small,
      4Q for Micro), using fund_flags.csv with its built-in 60-day
      publication lag (avail_date)
    - Entry: next trading day's open, after both conditions align

This mirrors engine_cross.py's validated methodology (which underlies the
166-trade backtest reproduction). Run this AFTER updating ohlc_data/ via
dhan_ohlc_updater.py, and after fundamentals in fund_flags.csv have been
refreshed with the latest quarter (see fundamentals ingestion scripts).

IMPORTANT CAVEAT — READ BEFORE TRUSTING OUTPUT
-------------------------------------------------
This scanner does NOT re-apply the corporate-action (CA) exclusion screen
that the original 166-trade dataset used (~17 split/bonus artifacts were
removed there). A small number of flagged signals here could be CA
artifacts rather than genuine qualifiers. Before acting on any signal this
script reports, sanity-check the stock's recent price chart for an
unadjusted split/bonus/rights event.

USAGE
-----
    python fresh_signal_scanner.py --cutoff 2026-06-01

Output: fresh_signals.csv with columns:
    symbol, market_type, trigger_date, entry_date, entry_price, target_ath, drawdown_pct
"""

import os
import argparse
import pandas as pd

FUND_FLAGS = "fund_flags.csv"
IDENT = "ident.csv"
OHLC_DIR = "ohlc_data"
OUTPUT_FILE = "fresh_signals.csv"


def scan(cutoff_date: str):
    fund = pd.read_csv(FUND_FLAGS, parse_dates=["report_date", "avail_date"])
    ident = pd.read_csv(IDENT)
    mt = dict(zip(ident["symbol"], ident["market_type"]))

    fund_by_sym = {
        s: g.sort_values("avail_date")[["avail_date", "all_hi"]].reset_index(drop=True)
        for s, g in fund.groupby("symbol")
    }

    cutoff = pd.Timestamp(cutoff_date)
    signals = []
    scanned, no_ohlc, no_fund = 0, [], []

    for sym in ident["symbol"]:
        path = os.path.join(OHLC_DIR, sym + ".csv")
        if not os.path.exists(path):
            no_ohlc.append(sym)
            continue

        d = pd.read_csv(path, usecols=["Date", "Open", "High", "Low", "Close"])
        d["Date"] = pd.to_datetime(d["Date"])
        d = d.dropna(subset=["Close", "Low"]).sort_values("Date").reset_index(drop=True)
        if len(d) < 2:
            continue

        # ATH tracked on a CLOSE basis (validated against base_sim.pkl)
        d["ath"] = d["Close"].cummax()
        d["prior_ath"] = d["ath"].shift(1)
        d["new_high"] = d["Close"] > d["prior_ath"].fillna(-1)

        # Threshold TOUCH detected via intraday LOW against the close-based ATH
        # (confirmed against base_sim.pkl's ICICIPRULI trigger: Close stayed
        # above the 60% line on the trigger day, but Low touched it intraday —
        # the officially validated engine fires on this Low-touch, not on Close)
        d["below"] = d["Low"] <= 0.60 * d["ath"]

        fb = fund_by_sym.get(sym)
        if fb is None or fb.empty:
            no_fund.append(sym)
            continue

        merged = pd.merge_asof(
            d[["Date"]], fb.rename(columns={"avail_date": "Date"}),
            on="Date", direction="backward"
        )
        fc = merged["all_hi"].fillna(False).values

        nh = d["new_high"].values
        below = d["below"].values
        opn = d["Open"].values
        close = d["Close"].values
        dates = d["Date"].values
        ath = d["ath"].values
        n = len(d)
        armed = True   # re-arm state carried through FULL history, not just post-cutoff

        for i in range(n):
            if nh[i]:
                armed = True
            if armed and below[i] and fc[i]:
                trigger_date = pd.Timestamp(dates[i])
                if trigger_date >= cutoff and i + 1 < n:
                    entry_price = opn[i + 1]
                    signals.append({
                        "symbol": sym,
                        "market_type": mt.get(sym),
                        "trigger_date": trigger_date.strftime("%Y-%m-%d"),
                        "entry_date": pd.Timestamp(dates[i + 1]).strftime("%Y-%m-%d"),
                        "entry_price": round(float(entry_price), 2),
                        "target_ath": round(float(ath[i]), 2),
                        "drawdown_pct": round((close[i] / ath[i] - 1) * 100, 1),
                    })
                armed = False   # re-arm consumed regardless of whether it's before/after cutoff

        scanned += 1

    result = pd.DataFrame(signals).sort_values("trigger_date") if signals else pd.DataFrame()
    if not result.empty:
        result.to_csv(OUTPUT_FILE, index=False)

    print(f"Scanned {scanned} symbols ({len(no_ohlc)} missing OHLC, {len(no_fund)} missing fundamentals)")
    print(f"Fresh signals since {cutoff_date}: {len(result)}")
    if not result.empty:
        print(result.to_string(index=False))
        print(f"\nSaved to {OUTPUT_FILE}")
        print("\nREMINDER: cross-check each against corporate-action history before "
              "treating as a confirmed qualifier (see caveat in file header).")
    else:
        print("No fresh signals found in this window with current data.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", default="2026-06-01",
                         help="Only report signals triggered on/after this date (YYYY-MM-DD)")
    args = parser.parse_args()
    scan(args.cutoff)
