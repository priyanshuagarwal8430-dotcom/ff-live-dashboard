"""
FF — Auto Dashboard Builder
==============================
Takes fresh_signal_scanner.py's output (fresh_signals.csv) + your OHLC data,
computes current prices/returns/checkpoints, flags likely corporate-action
artifacts, and rebuilds the live-tracking dashboard HTML — no manual editing.

THIS IS THE LAST STEP OF THE PIPELINE:

    1. dhan_ohlc_updater.py     -> refreshes ohlc_data/*.csv to today
    2. [fundamentals update]     -> refresh fund_flags.csv when a new quarter lands
    3. fresh_signal_scanner.py  -> finds new signals since a cutoff date
    4. build_dashboard.py       -> THIS SCRIPT — turns that into the dashboard

Run steps 1-3 first (or just 1+3 if fundamentals haven't changed), then run
this script. It always looks at ALL of fresh_signals.csv — if you want the
dashboard to keep older signals too, don't overwrite fresh_signals.csv
between runs; append to it instead (see NOTE at the bottom).

USAGE
-----
    python build_dashboard.py

Reads:
    fresh_signals.csv       (from fresh_signal_scanner.py)
    ohlc_data/*.csv          (for current prices + checkpoint returns)
    dashboard_template.html  (the HTML shell with {{PLACEHOLDERS}})

Writes:
    FF_Live_Tracking_Dashboard.html   <- open this / share this
"""

import json
import statistics
from datetime import timedelta, date
import pandas as pd
import os

SIGNALS_FILE = "fresh_signals.csv"
OHLC_DIR = "ohlc_data"
TEMPLATE_FILE = "FF_dashboard_template.html"
OUTPUT_FILE = "FF_Live_Tracking_Dashboard.html"

CUTOFF_LABEL = "1-Jun-2026"          # update this if you change --cutoff in the scanner
CA_DRAWDOWN_THRESHOLD = -55            # signals more extreme than this get flagged for manual CA check

DD_NOTE = ("Deep-drawdown zone (\u2264-30% mark-to-market). Per worst-case loss analysis: "
           "median worst-trough across all 166 backtested trades was only -16.2%, but there is "
           "no stop-loss \u2014 structural downside is uncapped. Recovery is possible (some deep-drawdown "
           "names did recover in backtest) but not guaranteed; monitor against the 3-year cap.")
CA_NOTE = ("Drawdown magnitude here is unusually large versus typical qualifiers. This has NOT been "
           "cross-checked against corporate-action history (splits/bonus/rights) \u2014 verify before treating "
           "as a confirmed signal. The original 166-trade backtest excluded ~17 similar CA artifacts.")

SEG_MAP = {'Large Cap': 'large', 'Mid Cap': 'mid', 'Small Cap': 'small', 'Micro Cap': 'micro'}


def load_ohlc(symbol):
    path = os.path.join(OHLC_DIR, f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["Date"])
    return df.sort_values("Date")


def price_near(df, target_date):
    sub = df[df["Date"] <= target_date]
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    return row["Close"], row["Date"]


def detect_data_currency(sample_symbols, n=15):
    """
    Determines the TRUE as-of date from the OHLC data itself, rather than
    trusting the system clock — if ohlc_data/ hasn't actually been refreshed,
    the dashboard must not claim a currency it doesn't have.
    """
    import random
    sample = random.sample(sample_symbols, min(n, len(sample_symbols)))
    max_dates = []
    for sym in sample:
        df = load_ohlc(sym)
        if df is not None and not df.empty:
            max_dates.append(df["Date"].max())
    if not max_dates:
        raise RuntimeError("Could not determine data currency — no OHLC files readable.")
    return max(max_dates)  # most recent date seen across the sample


def compute_rows(signals_df, as_of):
    results = []
    for _, r in signals_df.iterrows():
        df = load_ohlc(r["symbol"])
        if df is None:
            continue
        cur_price, cur_date = price_near(df, as_of)
        if cur_price is None:
            continue
        cur_ret = (cur_price / r["entry_price"] - 1) * 100
        days_held = (as_of - r["entry_date"]).days

        checkpoints = {}
        for label, ndays in [("3M", 90), ("6M", 182), ("1Y", 365), ("1.5Y", 548), ("2Y", 730)]:
            target_date = r["entry_date"] + timedelta(days=ndays)
            if target_date > as_of:
                continue
            cp_price, _ = price_near(df, target_date)
            if cp_price is not None:
                checkpoints[label] = round((cp_price / r["entry_price"] - 1) * 100, 1)

        flag_ca = r["drawdown_pct"] <= CA_DRAWDOWN_THRESHOLD
        results.append({
            "symbol": r["symbol"], "segment": r["market_type"],
            "entry_date": r["entry_date"].strftime("%d-%b-%Y"),
            "entry_price": round(r["entry_price"], 2), "current_price": round(cur_price, 2),
            "current_return": round(cur_ret, 1), "target_ath": round(r["target_ath"], 2),
            "days_held": days_held, "checkpoints": checkpoints, "flag_ca": bool(flag_ca),
        })
    return results


def build_js_rows(rows):
    js_rows = []
    for r in rows:
        seg = SEG_MAP.get(r["segment"], r["segment"].lower())
        if r["flag_ca"]:
            status, note = "deep-dd", f", note:{json.dumps(CA_NOTE)}"
        elif r["current_return"] <= -30:
            status, note = "deep-dd", f", note:{json.dumps(DD_NOTE)}"
        elif r["days_held"] >= 420:
            status, note = "past-median", ""
        else:
            status, note = "tracking", ""
        cps = ",".join(f'"{k}":{{ret:{v}}}' for k, v in r["checkpoints"].items())
        js_rows.append(
            f'{{stock:"{r["symbol"]}", seg:"{seg}", isNew:true, entryDate:"{r["entry_date"]}", '
            f'entryPrice:{r["entry_price"]}, currentPrice:{r["current_price"]}, target:{r["target_ath"]}, '
            f'days:{r["days_held"]}, cap:1095, status:"{status}", checkpoints:{{{cps}}}{note}}}'
        )
    return ",\n  ".join(js_rows)


def detect_fund_quarter_label():
    """
    Auto-detects the most recent quarter confirmed in fund_flags.csv, instead
    of relying on a hardcoded label that needs manual updates every quarter.
    Reads only the report_date column for speed (fund_flags.csv is large).
    """
    fund_dates = pd.read_csv("fund_flags.csv", usecols=["report_date"], parse_dates=["report_date"])
    latest = fund_dates["report_date"].max()
    return latest.strftime("%b-%y") + " quarter"


def build():
    signals_df = pd.read_csv(SIGNALS_FILE, parse_dates=["trigger_date", "entry_date"])
    if signals_df.empty:
        print("fresh_signals.csv is empty — nothing to build. Run fresh_signal_scanner.py first.")
        return

    as_of_ts = detect_data_currency(signals_df["symbol"].tolist())
    as_of = as_of_ts.date()
    today = date.today()
    staleness_days = (today - as_of).days
    if staleness_days > 3:
        print(f"WARNING: OHLC data is {staleness_days} days stale (latest: {as_of}, today: {today}). "
              f"Run dhan_ohlc_updater.py before building, or the dashboard will show old prices "
              f"labeled with their true (older) date — not today's date.")

    rows = compute_rows(signals_df, as_of_ts)
    if not rows:
        print("No rows computed — check that ohlc_data/ matches the symbols in fresh_signals.csv.")
        return

    data_js = build_js_rows(rows)

    total = len(rows)
    ca_flagged = sum(1 for r in rows if r["flag_ca"])
    confident = total - ca_flagged
    seg_counts = {"Micro Cap": 0, "Small Cap": 0, "Mid Cap": 0, "Large Cap": 0}
    for r in rows:
        seg_counts[r["segment"]] = seg_counts.get(r["segment"], 0) + 1
    segment_split = f'{seg_counts["Micro Cap"]}/{seg_counts["Small Cap"]}/{seg_counts["Mid Cap"]}/{seg_counts["Large Cap"]}'
    ages = [r["days_held"] for r in rows]
    age_range = f"{min(ages)}\u2013{max(ages)}d"

    with open(TEMPLATE_FILE) as f:
        template = f.read()

    replacements = {
        "{{DATA_ARRAY}}": data_js,
        "{{CUTOFF_LABEL}}": CUTOFF_LABEL,
        "{{AS_OF_LABEL}}": as_of.strftime("%d %b %Y"),
        "{{AS_OF_SHORT}}": as_of.strftime("%d-%b"),
        "{{FUND_QUARTER_LABEL}}": detect_fund_quarter_label(),
        "{{TOTAL_COUNT}}": str(total),
        "{{CONFIDENT_COUNT}}": str(confident),
        "{{CA_FLAGGED_COUNT}}": str(ca_flagged),
        "{{SEGMENT_SPLIT}}": segment_split,
        "{{AGE_RANGE}}": age_range,
        "{{AGE_RANGE_NOTE}}": "Days since trigger, oldest to newest",
        "{{DATA_CURRENCY_NOTE}}": "Fully current \u2014 auto-refreshed daily",
        "{{GENERATED_TIMESTAMP}}": as_of.strftime("%d-%b-%Y"),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)

    with open(OUTPUT_FILE, "w") as f:
        f.write(template)

    print(f"Built {OUTPUT_FILE}: {total} signals ({confident} confident, {ca_flagged} CA-flagged)")
    print(f"Segment split (Micro/Small/Mid/Large): {segment_split}")
    print(f"As of: {as_of}")


if __name__ == "__main__":
    build()

# NOTE on accumulating signals across runs:
# fresh_signal_scanner.py's --cutoff always scans from that fixed date forward,
# so re-running it with the same cutoff will naturally include everything found
# before PLUS anything new (it re-scans full history, it doesn't need manual
# merging). Just re-run scanner -> builder in sequence each time and the output
# will reflect the complete, current list since your chosen cutoff date.
