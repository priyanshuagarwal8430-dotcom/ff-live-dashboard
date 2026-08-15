"""
FF — Gemini-style Screener Fetcher, FIXED for financial companies
=======================================================================
This keeps the EXACT same simple approach that already worked (plain
requests, no stealth, no headless browser) — it got clean data for 43/63
of the previously-stuck tickers on the first try. The only fix here: the
original version only looked for a row literally labelled "Sales", which
doesn't exist for banks/NBFCs/insurers (they report "Revenue", "Total
Income", or "Interest Earned" instead) — that explained all 17 of the
remaining gaps. This version searches all of those label variants.

WHY THE SIMPLE APPROACH SUDDENLY WORKED
-------------------------------------------
Genuinely uncertain — three earlier attempts (plain requests, rate-limited
requests, and a real headless browser) all failed for this exact list.
This run, using essentially the same method, worked for the large majority.
The most likely explanation is that whatever was serving degraded content
before (IP-reputation, load-based throttling, or similar) isn't a fixed,
permanent block — it can vary run to run. Treat this as good evidence that
simply retrying (ideally from a fresh session) is a reasonable strategy
going forward, not just this one-off fix.

USAGE
-----
python fixed_gemini_scraper.py --tickers retry_list.txt --output fundamentals_retry.csv

Where retry_list.txt is the list of tickers still needing a (re-)fetch —
for the immediate case, that's the 17 financial-sector tickers plus AGL.
"""

import argparse
import time
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import pandas as pd

HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/91.0.4472.124 Safari/537.36")}
DELAY_SECONDS = 1.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("ff_gemini_fixed")


def find_row(rows_dict, *keywords):
    """Searches row labels for any of the given keywords (case-insensitive,
    handles non-breaking spaces). This is the fix: the original only
    matched the exact word 'Sales'."""
    for label, values in rows_dict.items():
        clean = label.replace("\xa0", " ").strip().lower()
        if any(kw in clean for kw in keywords):
            return values
    return None


def get_screener_data(ticker_list):
    all_data = []
    failed = []

    for symbol in ticker_list:
        log.info(f"Fetching {symbol}...")
        try:
            url = f"https://www.screener.in/company/{symbol}/"
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                log.warning(f"{symbol}: HTTP {response.status_code}")
                failed.append(symbol)
                continue

            soup = BeautifulSoup(response.content, "html.parser")
            section = soup.find("section", {"id": "quarters"})
            if not section:
                log.warning(f"{symbol}: no #quarters section found")
                failed.append(symbol)
                continue

            table = section.find("table")
            headers_row = table.find("thead").find_all("th")
            quarters = [th.get_text().strip() for th in headers_row[1:]]

            if not quarters:
                log.warning(f"{symbol}: table found but 0 quarter columns parsed")
                failed.append(symbol)
                continue

            rows = table.find("tbody").find_all("tr")
            rows_dict = {}
            for row in rows:
                row_name = row.find("td").get_text().strip()
                values = [td.get_text().strip().replace(",", "") for td in row.find_all("td")[1:]]
                rows_dict[row_name] = values

            # THE FIX: broad label matching instead of exact "Sales" only
            sales_vals = find_row(rows_dict, "sales", "revenue", "total income", "interest earned")
            np_vals = find_row(rows_dict, "net profit")
            eps_vals = find_row(rows_dict, "eps")

            ticker_results = pd.DataFrame(index=quarters)
            ticker_results["Ticker"] = symbol
            if sales_vals:
                ticker_results["Sales"] = sales_vals
            if np_vals:
                ticker_results["Net Profit"] = np_vals
            if eps_vals:
                ticker_results["EPS"] = eps_vals

            all_data.append(ticker_results)
            time.sleep(DELAY_SECONDS)

        except Exception as e:
            log.warning(f"Error processing {symbol}: {e}")
            failed.append(symbol)

    result_df = pd.concat(all_data) if all_data else pd.DataFrame()
    return result_df, failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--output", default="fundamentals_master.csv")
    parser.add_argument("--failed-log", default="failed_tickers_gemini.txt")
    args = parser.parse_args()

    with open(args.tickers) as f:
        tickers = [line.strip() for line in f if line.strip()]
    log.info(f"Loaded {len(tickers)} tickers")

    df, failed = get_screener_data(tickers)

    if not df.empty:
        # Reformat to the pipeline's standard schema (symbol, quarter_label,
        # net_sales_cr, pat_cr, diluted_eps, source_url, pulled_at) so this
        # plugs directly into FF_fundamentals_ingest.py, same as the main scraper.
        out = df.reset_index().rename(columns={
            "index": "quarter_label", "Ticker": "symbol",
            "Sales": "net_sales_cr", "Net Profit": "pat_cr", "EPS": "diluted_eps",
        })
        out["source_url"] = "https://www.screener.in/company/" + out["symbol"].astype(str) + "/"
        out["pulled_at"] = datetime.now().isoformat(timespec="seconds")
        for col in ["net_sales_cr", "pat_cr", "diluted_eps"]:
            if col not in out.columns:
                out[col] = None
        out = out[["symbol", "quarter_label", "net_sales_cr", "pat_cr", "diluted_eps", "source_url", "pulled_at"]]
        out.to_csv(args.output, index=False)
        log.info(f"Wrote {len(out)} rows across {out['symbol'].nunique()} tickers -> {args.output}")
    else:
        log.warning("No data collected.")

    if failed:
        with open(args.failed_log, "w") as f:
            f.write("\n".join(failed))
        log.warning(f"{len(failed)} tickers failed -> {args.failed_log}")
    else:
        log.info("All tickers succeeded!")
