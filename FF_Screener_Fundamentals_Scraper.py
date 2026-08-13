"""
FF — Quarterly Fundamentals Scraper (Screener.in, ticker-based)
=================================================================
PERSONAL / INTERNAL USE ONLY — not for resale or redistribution of the
underlying data. Screener.in does not offer an official public API; this
script reads publicly visible company pages using your existing NSE tickers.

WHY THIS EXISTS
----------------
Prowess access is borrowed/temporary — not a stable long-term dependency.
This script removes that dependency for internal signal-generation by
pulling the same three fields the FF strategy needs (Net Sales, PAT,
Diluted EPS) directly from Screener.in's public quarterly-results table,
using the ticker list you already have (from the validated 751-stock
OHLC universe).

RECOMMENDATION: treat this as a bridge solution. Once you or Himanshu
purchase a Screener Premium subscription (~Rs 5-6k/yr), switch to the
official "Export to Excel" feature instead — same output schema below,
but on a sanctioned, stable channel instead of page-scraping.

OUTPUT SCHEMA (this is the fixed internal format the FF engine expects —
any future data source, Prowess/Screener/other, must be adapted to match
this exact schema):

    symbol, quarter_end, net_sales_cr, pat_cr, diluted_eps

HOW TO RUN
----------
1. pip install requests beautifulsoup4 pandas lxml
2. Put your ticker list in tickers.txt (one NSE symbol per line) — or
   point TICKER_SOURCE_DIR below at your ohlc_data/ folder and it will
   derive tickers from the filenames automatically (*.csv -> symbol).
3. python screener_fundamentals_scraper.py
4. Output: fundamentals_master.csv — feed this straight into the FF
   rolling 8Q/4Q engine.

RATE LIMITING
-------------
Deliberately slow (1.5–2.5s random delay per ticker) and uses a single
session with a normal browser User-Agent. For 751 tickers this run takes
roughly 25–35 minutes. Do not remove the delay — a fast/bulk hammer is
exactly the kind of traffic that gets an IP blocked, and this is meant
to be a light, respectful, personal-use pull, not a scraping operation.
"""

import os
import time
import random
import argparse
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import pandas as pd

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
TICKER_SOURCE_DIR = "ohlc_data"       # derive tickers from *.csv filenames here
TICKER_FILE = "tickers.txt"            # fallback: one symbol per line
OUTPUT_FILE = "fundamentals_master.csv"
FAILED_LOG = "failed_tickers.txt"
DELAY_MIN, DELAY_MAX = 1.5, 2.5        # seconds between requests — keep polite
RETRY_DELAY_MIN, RETRY_DELAY_MAX = 8.0, 12.0   # much longer — for suspected rate-limited retries
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler("scraper_run.log"), logging.StreamHandler()],
)
log = logging.getLogger("ff_scraper")


# ---------------------------------------------------------------------
# TICKER LOADING
# ---------------------------------------------------------------------
def load_tickers(override_file=None):
    if override_file:
        with open(override_file) as f:
            tickers = [line.strip().upper() for line in f if line.strip()]
        log.info(f"Loaded {len(tickers)} tickers from override file: {override_file}")
        return tickers
    if os.path.isdir(TICKER_SOURCE_DIR):
        tickers = sorted(
            f[:-4] for f in os.listdir(TICKER_SOURCE_DIR) if f.lower().endswith(".csv")
        )
        if tickers:
            log.info(f"Loaded {len(tickers)} tickers from {TICKER_SOURCE_DIR}/")
            return tickers
    if os.path.isfile(TICKER_FILE):
        with open(TICKER_FILE) as f:
            tickers = [line.strip().upper() for line in f if line.strip()]
        log.info(f"Loaded {len(tickers)} tickers from {TICKER_FILE}")
        return tickers
    raise FileNotFoundError(
        "No ticker source found. Point TICKER_SOURCE_DIR at your ohlc_data/ "
        "folder, or create tickers.txt (one NSE symbol per line)."
    )


# ---------------------------------------------------------------------
# PARSING HELPERS
# ---------------------------------------------------------------------
def clean_number(txt):
    """Screener numbers look like '1,234' or '-56' or '' — normalize to float/None."""
    if txt is None:
        return None
    txt = txt.strip().replace(",", "").replace("%", "")
    if txt in ("", "-", "—"):
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def find_quarterly_table(soup):
    """
    Screener's quarterly results table sits under a <section id="quarters">.
    Structure: first <tr> = column headers (quarter-end dates/labels),
    subsequent rows are labelled 'Sales', 'Net Profit', 'EPS in Rs' etc.
    NOTE: Screener occasionally tweaks markup — if this stops matching,
    inspect the page's #quarters section manually and adjust the selector.
    """
    section = soup.find("section", id="quarters")
    if section is None:
        return None
    table = section.find("table")
    return table


def parse_quarters(table):
    """
    Returns dict: {row_label: {quarter_label: value}}

    IMPORTANT FIX: previously assumed the table's FIRST <tr> was always the
    header row. For ~60 companies (banks, MNC subsidiaries, defense PSUs —
    no clear pattern by sector), this produced 0 parsed quarter columns —
    meaning the actual header row wasn't at position 0 for these specific
    pages (possibly a different Screener template variant, or a locked/
    premium-teaser row inserted above the real header for some companies).
    Now explicitly searches for the row containing multiple <th> cells
    (the structural marker of a header row) rather than assuming position.
    """
    rows = table.find_all("tr")
    if not rows:
        return {}, []

    header_row = None
    for tr in rows:
        th_cells = tr.find_all("th")
        if len(th_cells) > 1:  # more than just a leading label column
            header_row = tr
            break
    if header_row is None:
        header_row = rows[0]  # fallback to old behavior if no <th> row found at all

    header_cells = header_row.find_all(["th", "td"])
    quarter_labels = [c.get_text(strip=True) for c in header_cells[1:]]

    data = {}
    for tr in rows:
        if tr is header_row:
            continue
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        values = [clean_number(c.get_text(strip=True)) for c in cells[1:]]
        if label and quarter_labels:
            data[label] = dict(zip(quarter_labels, values))
    return data, quarter_labels


def extract_target_rows(parsed):
    """
    Map Screener's row labels to our three required fields.
    Screener label variants seen historically — kept broad on purpose.

    IMPORTANT FIX: the original version used a strict exact-match for the
    sales/revenue row, which silently failed for every company (0% match
    rate) — likely due to hidden non-breaking-space characters in Screener's
    HTML, or alternate labels for financial-sector companies (banks/NBFCs
    use "Revenue"/"Total Income" instead of "Sales"). Now uses the same
    lenient substring approach that already worked correctly for PAT/EPS,
    plus explicit non-breaking-space normalization.
    """
    def clean_label(k):
        return k.replace("\xa0", " ").replace("\u200b", "").strip().lower()

    def find_row(*keywords):
        for k, v in parsed.items():
            label = clean_label(k)
            if any(kw in label for kw in keywords):
                return v
        return {}

    sales_row = find_row("sales", "revenue", "total income", "interest earned")
    pat_row = find_row("net profit")
    eps_row = find_row("eps")
    return sales_row, pat_row, eps_row


# ---------------------------------------------------------------------
# MAIN SCRAPE LOOP
# ---------------------------------------------------------------------
def fetch_ticker(session, symbol):
    """Try consolidated page first, fall back to standalone."""
    urls = [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]
    status_codes = []
    for url in urls:
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            status_codes.append(resp.status_code)
            if resp.status_code == 200:
                return resp.text, url
            elif resp.status_code == 404:
                continue
            else:
                log.warning(f"{symbol}: HTTP {resp.status_code} at {url}")
        except requests.RequestException as e:
            log.warning(f"{symbol}: request failed ({e}) at {url}")
            status_codes.append(f"exception:{e}")
    # FIX: previously this case (both URLs 404, or all attempts exhausted)
    # returned silently with no log line at all — now always logged.
    log.warning(f"{symbol}: no page found — tried both URLs, got statuses {status_codes}")
    return None, None


DEBUG_HTML_DIR = "debug_html"


def run(override_file=None, output_file=None, debug=False, slow=False):
    tickers = load_tickers(override_file)
    session = requests.Session()
    delay_min, delay_max = (RETRY_DELAY_MIN, RETRY_DELAY_MAX) if slow else (DELAY_MIN, DELAY_MAX)
    if slow:
        log.info(f"SLOW MODE: using {delay_min}-{delay_max}s delays (testing for rate-limit sensitivity)")

    if debug:
        os.makedirs(DEBUG_HTML_DIR, exist_ok=True)

    all_rows = []
    failed = []

    for i, symbol in enumerate(tickers, 1):
        log.info(f"[{i}/{len(tickers)}] {symbol}")
        html, used_url = fetch_ticker(session, symbol)

        if html is None:
            failed.append(symbol)
            time.sleep(random.uniform(delay_min, delay_max))
            continue

        soup = BeautifulSoup(html, "lxml")
        table = find_quarterly_table(soup)
        if table is None:
            log.warning(f"{symbol}: no #quarters table found (page structure may differ)")
            failed.append(symbol)
            if debug:
                with open(os.path.join(DEBUG_HTML_DIR, f"{symbol}.html"), "w") as f:
                    f.write(html)
                log.info(f"{symbol}: saved raw response to {DEBUG_HTML_DIR}/{symbol}.html for inspection")
            time.sleep(random.uniform(delay_min, delay_max))
            continue

        parsed, quarter_labels = parse_quarters(table)
        if not quarter_labels:
            log.warning(f"{symbol}: #quarters table found but 0 quarter columns parsed "
                        f"(page structure may differ for this company type)")
            failed.append(symbol)
            if debug:
                with open(os.path.join(DEBUG_HTML_DIR, f"{symbol}.html"), "w") as f:
                    f.write(html)
                log.info(f"{symbol}: saved raw response to {DEBUG_HTML_DIR}/{symbol}.html for inspection")
            time.sleep(random.uniform(delay_min, delay_max))
            continue
        sales_row, pat_row, eps_row = extract_target_rows(parsed)

        for q in quarter_labels:
            all_rows.append({
                "symbol": symbol,
                "quarter_label": q,
                "net_sales_cr": sales_row.get(q),
                "pat_cr": pat_row.get(q),
                "diluted_eps": eps_row.get(q),
                "source_url": used_url,
                "pulled_at": datetime.now().isoformat(timespec="seconds"),
            })

        time.sleep(random.uniform(delay_min, delay_max))

    out_path = output_file or OUTPUT_FILE
    if all_rows:
        df = pd.DataFrame(all_rows)
        df.to_csv(out_path, index=False)
        log.info(f"Wrote {len(df)} rows across {df['symbol'].nunique()} tickers -> {out_path}")
    else:
        log.error("No data collected — check network access and page structure.")

    if failed:
        with open(FAILED_LOG, "w") as f:
            f.write("\n".join(failed))
        log.warning(f"{len(failed)} tickers failed — see {FAILED_LOG} for the list, "
                    f"and the log above for each one's specific reason.")

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None,
                         help="Optional: path to a file with one ticker per line, "
                              "to retry only specific symbols instead of the full universe.")
    parser.add_argument("--output", default=None,
                         help="Optional: output filename (default: fundamentals_master.csv). "
                              "Use a different name when retrying a subset, so you don't "
                              "overwrite your main file — merge manually afterward.")
    parser.add_argument("--slow", action="store_true",
                         help="Use much longer delays (8-12s) between requests — use this to test "
                              "whether empty-data failures are caused by rate-limiting.")
    parser.add_argument("--debug", action="store_true",
                         help="Save raw HTML for any symbol that fails to parse, into debug_html/ "
                              "— use this to diagnose parsing failures by comparing what the "
                              "scraper actually received vs what a browser shows.")
    args = parser.parse_args()
    run(override_file=args.tickers, output_file=args.output, debug=args.debug, slow=args.slow)
