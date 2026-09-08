"""
FF_market_probe.py  --  probe v2. One-shot, not part of the daily run.

v1 settled the easy half of the market strip on Yahoo. It also turned up two
things Yahoo cannot do:

  * NIFTY_MICROCAP250.NS and NIFTYGS10YR.NS quote live but carry NO history -
    confirmed by hand on Yahoo's own Historical Data tab, which returns a
    single row for a one-year range. So 1W/1M/6M/1Y can never be computed for
    them from Yahoo.
  * ^TNX was picked up as "India 10Y". It is the US 10-year. v1's auto-picker
    took the first working symbol and that symbol was a control I should not
    have put in the list.

So v2 asks NSE instead, which Phase A already proved answers a GitHub runner.
Three separate questions, reported separately - a failure in one says nothing
about the others:

  A. NSE index history - can we move the Nifty rows off Yahoo entirely?
  B. India 10-year YIELD - a real yield, not the G-Sec total-return index.
  C. MCX gold and crude - the rupee prices Yahoo has no feed for.

B and C are folded into this run rather than left for a later one. They cost
nothing extra here and each saved run is a push, a workflow run and a wait.

Nothing is written. It only prints.
"""

import json
import sys
import warnings
from datetime import date, timedelta

warnings.filterwarnings("ignore")

try:
    import requests
except ImportError as e:
    print("MISSING DEPENDENCY:", e)
    sys.exit(0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HDRS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}
TIMEOUT = 25
LINE = "-" * 96


def rule(title):
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)


def nse_session():
    """NSE needs cookies. The handshake itself returned 403 from the runner in
    Phase A while the API endpoints still worked, so a failed warm-up is not
    treated as fatal - it is reported and the probe carries on."""
    s = requests.Session()
    s.headers.update(HDRS)
    for url in ("https://www.nseindia.com/",
                "https://www.nseindia.com/market-data/live-market-indices"):
        try:
            r = s.get(url, timeout=TIMEOUT)
            print(f"  warm-up {url[:58]:58s} -> HTTP {r.status_code}, "
                  f"{len(s.cookies)} cookies")
        except Exception as e:
            print(f"  warm-up {url[:58]:58s} -> {type(e).__name__}: {e}"[:96])
    return s


# --------------------------------------------------------------- A. indices ---
# NSE matches index names exactly, so each one is tried under every spelling it
# plausibly uses. Nifty 500 is the control: we already publish it daily, so if
# it fails here the endpoint is the problem, not the name.
INDEX_NAMES = {
    "Nifty 500 (CONTROL)": ["NIFTY 500"],
    "Nifty 50":            ["NIFTY 50"],
    "Nifty Midcap 150":    ["NIFTY MIDCAP 150", "NIFTY MIDCAP150"],
    "Nifty Smallcap 250":  ["NIFTY SMALLCAP 250", "NIFTY SMLCAP 250",
                            "NIFTY SMALLCAP250"],
    "Nifty Microcap 250":  ["NIFTY MICROCAP250", "NIFTY MICROCAP 250",
                            "NIFTY MICRO CAP 250"],
    "Nifty GS 10Yr":       ["NIFTY GS 10YR", "NIFTY GS 10 YR",
                            "NIFTY G-SEC 10 YR"],
}

CLOSE_KEYS = ("EOD_CLOSE_INDEX_VAL", "CLOSE", "close")
DATE_KEYS = ("EOD_TIMESTAMP", "TIMESTAMP", "mTIMESTAMP", "date")


def index_history(s, name, days=400):
    to_d = date.today()
    from_d = to_d - timedelta(days=days)
    url = ("https://www.nseindia.com/api/historical/indicesHistory"
           f"?indexType={requests.utils.quote(name)}"
           f"&from={from_d.strftime('%d-%m-%Y')}&to={to_d.strftime('%d-%m-%Y')}")
    try:
        r = s.get(url, timeout=TIMEOUT)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:70]
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        j = r.json()
    except Exception:
        return None, f"not JSON (first 60 chars: {r.text[:60]!r})"
    recs = (j.get("data") or {}).get("indexCloseOnlineRecords")
    if recs is None and isinstance(j.get("data"), list):
        recs = j["data"]
    if not recs:
        return None, "200 but no records"

    rows = []
    for rec in recs:
        c = next((rec[k] for k in CLOSE_KEYS if k in rec), None)
        d = next((rec[k] for k in DATE_KEYS if k in rec), None)
        if c is None or d is None:
            continue
        try:
            rows.append((str(d), float(c)))
        except (TypeError, ValueError):
            continue
    if len(rows) < 2:
        return None, f"{len(recs)} records but {len(rows)} usable closes"
    return rows, None


rule("A.  NSE index history   -   can the Nifty rows leave Yahoo?")
s = nse_session()
for label, names in INDEX_NAMES.items():
    print(f"\n{label}")
    print(LINE)
    for nm in names:
        rows, why = index_history(s, nm)
        if why:
            print(f"  {nm:24s} FAIL   {why}")
            continue
        # Records come newest-first from NSE; sort by nothing, just report the
        # ends as received and let the dates speak.
        print(f"  {nm:24s} OK     {len(rows)} closes   "
              f"{rows[-1][0]} .. {rows[0][0]}")
        print(f"  {'':24s}        latest = {rows[0][1]:,.2f}   "
              f"oldest = {rows[-1][1]:,.2f}")
        print(f"  {'':24s}        last 5: "
              + ", ".join(f"{d}={c:,.2f}" for d, c in rows[:5]))
        break   # first spelling that works is enough


# ------------------------------------------------------------- B. 10Y yield ---
# The G-Sec INDEX is not a yield - it rises when yields fall. Himanshu asked
# for the yield, so these are candidates for the yield itself. Each is only
# checked for reachability and shape here; whichever answers gets a proper
# parser afterwards, not a guess now.
rule("B.  India 10-year YIELD   -   a yield, not the G-Sec index")

YIELD_TRIES = [
    ("NSE live G-Sec on CM",
     "https://www.nseindia.com/api/liveBonds-traded-on-cm?type=gsec", "nse"),
    ("NSE live bonds on CM",
     "https://www.nseindia.com/api/liveBonds-traded-on-cm?type=bonds", "nse"),
    ("FBIL (official benchmark)", "https://www.fbil.org.in/", "plain"),
    ("CCIL", "https://www.ccilindia.com/", "plain"),
    ("RBI", "https://www.rbi.org.in/", "plain"),
    # FRED needs no key for the CSV endpoint. India long-term govt bond yield,
    # MONTHLY - too coarse for a 1D change, probed only to see what is reachable.
    ("FRED IRLTLT01INM156N (monthly)",
     "https://fred.stlouisfed.org/graph/fredgraph.csv?id=IRLTLT01INM156N",
     "plain"),
]

for label, url, kind in YIELD_TRIES:
    try:
        r = (s if kind == "nse" else requests).get(
            url, timeout=TIMEOUT,
            headers=None if kind == "nse" else {"User-Agent": UA})
        ct = (r.headers.get("content-type") or "")[:40]
        body = r.text or ""
        note = ""
        if r.status_code == 200 and "json" in ct:
            try:
                j = r.json()
                keys = list(j)[:6] if isinstance(j, dict) else f"list[{len(j)}]"
                n = len(j.get("data", [])) if isinstance(j, dict) else 0
                note = f"  keys={keys}  data rows={n}"
            except Exception:
                note = "  (json header but unparseable)"
        elif r.status_code == 200:
            note = "  first line: " + body.splitlines()[0][:60] if body else ""
        print(f"  {label:32s} HTTP {r.status_code}  {len(body):8d} bytes  "
              f"{ct}{note}")
    except Exception as e:
        print(f"  {label:32s} {type(e).__name__}: {e}"[:96])


# ---------------------------------------------------------------- C. MCX -----
# NSE does not carry commodities; MCX does. Gold and crude were decided in
# rupees, and Yahoo has no MCX feed, so this asks whether MCX itself is
# reachable at all. Reachability only - no parsing on a guess.
rule("C.  MCX   -   is the real rupee gold / crude price reachable?")

for label, url in [
    ("MCX home", "https://www.mcxindia.com/"),
    ("MCX market watch", "https://www.mcxindia.com/backpage.aspx/GetMarketWatch"),
    ("MCX market data page",
     "https://www.mcxindia.com/market-data/get-quote"),
]:
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
        print(f"  {label:24s} HTTP {r.status_code}  {len(r.text):8d} bytes  "
              f"{(r.headers.get('content-type') or '')[:40]}")
    except Exception as e:
        print(f"  {label:24s} {type(e).__name__}: {e}"[:96])

rule("done")
print("A decides whether the Nifty rows move to NSE.")
print("B decides whether the bond row can be a real yield.")
print("C decides whether gold and crude can be real MCX prices.")
print("Nothing here is wired into the dashboard. Delete this file when done.")
