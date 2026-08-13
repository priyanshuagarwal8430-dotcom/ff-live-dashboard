"""
FF — Daily OHLC Updater via Yahoo Finance (yfinance)
=======================================================
Dhan-independent alternative for keeping ohlc_data/*.csv current. Since your
existing OHLC dataset (validated paisa-for-paisa against the 166-trade
backtest) is itself Yahoo Finance-sourced, this keeps the SAME source going
forward — no dependency on Dhan being available.

WHY THIS EXISTS
----------------
You asked: if Dhan access disappears tomorrow, does the pipeline still work?
With this script as the OHLC source instead of (or alongside) the Dhan
updater, the answer is yes — this has zero dependency on Dhan.

HONEST CAVEAT
-------------
yfinance is an unofficial library (Yahoo shut down its official API in
2017) — it works by reading Yahoo's own internal endpoints, the same way
your existing OHLC data was likely built. Yahoo's own terms describe this
kind of access as personal-use. For pure price/OHLC data (not fundamentals),
this is common practice and low-risk in this community, but it can break if
Yahoo changes their endpoints without notice — treat it the same way you'd
treat any single-source dependency: fine to run, but don't be surprised if
it occasionally needs a fix.

RUN THIS ON YOUR OWN MACHINE / COLAB — NOT INSIDE CLAUDE
-----------------------------------------------------------
Claude's sandbox cannot reach Yahoo Finance directly (network is restricted
to package registries only). Run this in Colab exactly like the other
scripts in your pipeline.

SETUP
-----
1. pip install yfinance pandas
2. Place this script in the same folder as ohlc_data/
3. python yfinance_ohlc_updater.py

WHAT IT DOES
------------
For each of your 751 symbols: reads the last date already in its CSV,
pulls daily candles from (last_date + 1) to today via yfinance (NSE tickers
need a ".NS" suffix, added automatically), and appends the new rows —
same output schema as your existing files, zero downstream changes needed.

NOTE ON TICKER SUFFIXES
------------------------
Yahoo Finance identifies NSE-listed stocks with a ".NS" suffix (e.g.
"INFY.NS"). A handful of symbols have naming quirks (e.g. symbols with
"&" like J&KBANK) that may need manual mapping — these get logged to
failed_tickers.txt for you to check individually if they fail.
"""

import os
import time
import logging
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

OHLC_DIR = "ohlc_data"
REQUEST_DELAY = 0.3          # seconds between pulls — polite pacing
FAILED_LOG = "yf_failed_tickers.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler("yfinance_update.log"), logging.StreamHandler()],
)
log = logging.getLogger("yf_updater")


def last_date_in_file(path):
    df = pd.read_csv(path, usecols=["Date"], parse_dates=["Date"])
    if df.empty:
        return None
    return df["Date"].max().date()


def fetch_and_append(symbol, from_date, to_date, path):
    yf_ticker = f"{symbol}.NS"
    try:
        df = yf.download(
            yf_ticker,
            start=from_date.isoformat(),
            end=(to_date + timedelta(days=1)).isoformat(),  # yfinance end date is exclusive
            progress=False,
            auto_adjust=False,
        )
    except Exception as e:
        log.warning(f"{symbol}: download failed ({e})")
        return 0

    if df is None or df.empty:
        return 0

    df = df.reset_index()
    # yfinance sometimes returns MultiIndex columns for single tickers — flatten if so
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    new_rows = pd.DataFrame({
        "Date": pd.to_datetime(df["Date"]).dt.normalize(),
        "Open": df["Open"],
        "High": df["High"],
        "Low": df["Low"],
        "Close": df["Close"],
        "Volume": df["Volume"],
    })

    existing = pd.read_csv(path, parse_dates=["Date"])
    combined = pd.concat([existing, new_rows]).drop_duplicates(subset="Date").sort_values("Date")
    combined.to_csv(path, index=False)
    return len(new_rows)


def run():
    today = date.today()
    files = sorted(f for f in os.listdir(OHLC_DIR) if f.endswith(".csv"))

    total_new_rows = 0
    updated, skipped, failed = [], [], []

    for i, fname in enumerate(files, 1):
        symbol = fname[:-4]
        path = os.path.join(OHLC_DIR, fname)

        last_date = last_date_in_file(path)
        if last_date is None:
            skipped.append(symbol)
            continue
        from_date = last_date + timedelta(days=1)
        if from_date > today:
            skipped.append(symbol)
            continue

        log.info(f"[{i}/{len(files)}] {symbol}: pulling {from_date} -> {today}")
        n = fetch_and_append(symbol, from_date, today, path)
        if n > 0:
            updated.append(symbol)
            total_new_rows += n
        else:
            failed.append(symbol)
        time.sleep(REQUEST_DELAY)

    log.info("=" * 60)
    log.info(f"Updated: {len(updated)} symbols, {total_new_rows} new rows total")
    log.info(f"Already up to date: {len(skipped)} symbols")
    log.info(f"No new data returned: {len(failed)} symbols")

    if failed:
        with open(FAILED_LOG, "w") as f:
            f.write("\n".join(failed))
        log.warning(f"{len(failed)} tickers returned nothing — see {FAILED_LOG}. "
                    f"Could be a holiday with no trading, a suspended stock, or a "
                    f"ticker-symbol mismatch (check the .NS suffix manually for these).")

    log.info("Done. Re-run the fresh-signal scanner next.")


if __name__ == "__main__":
    run()
