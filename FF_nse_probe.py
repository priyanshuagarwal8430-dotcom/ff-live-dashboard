"""
FF — is NSE reachable from a GitHub Actions runner?
===================================================
TEMPORARY. Delete this file and its workflow step once the question is answered.

WHY IT EXISTS
Phase A proved NSE answers from Colab. Colab runs on Google's servers; this
workflow runs on GitHub's, and NSE is known to refuse some hosted ranges. Those
are different networks and one says nothing about the other. Three things wait on
this single answer:

  * the news section (event-calendar and corporate-announcements)
  * keeping results-announcement dates current without a manual notebook
  * switching the daily price feed from Yahoo to NSE bhavcopy

Building any of them on an assumption about the network is how today's two
failures happened. So this asks first.

It READS ONLY. It writes no file, changes no data, and always exits 0 — a probe
that could fail the run would be worse than the doubt it resolves.
"""
import sys, time, json, io, zipfile
from datetime import date, timedelta
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
     "Accept": "application/json, text/plain, */*"}

def last_weekday(n=3):
    d = date.today() - timedelta(days=n)
    while d.weekday() > 4: d -= timedelta(days=1)
    return d

def line(name, ok, detail):
    print(f"  {'OK  ' if ok else 'FAIL'}  {name:34} {detail}")
    return ok

def main():
    print("=" * 72)
    print("NSE reachability from this runner — read only, never fatal")
    print("=" * 72)
    s = requests.Session(); s.headers.update(H)
    results = {}

    # --- the cookie handshake everything else depends on
    try:
        r = s.get("https://www.nseindia.com/", timeout=25)
        results["handshake"] = line("handshake www.nseindia.com", r.status_code == 200,
                                    f"HTTP {r.status_code}, {len(s.cookies)} cookies")
    except Exception as e:
        results["handshake"] = line("handshake www.nseindia.com", False,
                                    f"{type(e).__name__}: {str(e)[:60]}")

    d1 = (date.today() - timedelta(days=45)).strftime("%d-%m-%Y")
    d2 = date.today().strftime("%d-%m-%Y")
    REF = {"Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions"}

    for name, path in (
        ("event-calendar",         f"/api/event-calendar?index=equities&from_date={d1}&to_date={d2}"),
        ("corporate-announcements",f"/api/corporate-announcements?index=equities&from_date={d1}&to_date={d2}"),
        ("corporate actions",      f"/api/corporates-corporateActions?index=equities&from_date={d1}&to_date={d2}"),
    ):
        try:
            r = s.get("https://www.nseindia.com" + path, headers={**H, **REF}, timeout=40)
            n = None
            if r.status_code == 200:
                try:
                    j = r.json(); n = len(j if isinstance(j, list) else j.get("data", []))
                except Exception: n = "not JSON"
            results[name] = line(name, r.status_code == 200 and isinstance(n, int) and n > 0,
                                 f"HTTP {r.status_code}, {len(r.content):>8} bytes, records {n}")
        except Exception as e:
            results[name] = line(name, False, f"{type(e).__name__}: {str(e)[:60]}")
        time.sleep(1.5)

    # --- bhavcopy: the exact URLs FF_NSE_OHLC_Updater.py would use
    d = last_weekday()
    yy, mm, dd = d.strftime("%Y"), d.strftime("%m"), d.strftime("%d")
    mon = d.strftime("%b").upper()
    b = requests.Session()
    b.headers.update({**H, "Referer": "https://www.nseindia.com/all-reports"})
    try: b.get("https://www.nseindia.com/all-reports", timeout=25)
    except Exception: pass
    for u in (f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yy}{mm}{dd}_F_0000.csv.zip",
              f"https://archives.nseindia.com/content/historical/EQUITIES/{yy}/{mon}/cm{dd}{mon}{yy}bhav.csv.zip",
              f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dd}{mm}{yy}.csv"):
        tag = "bhavcopy " + u.split("/")[-1][:28]
        try:
            r = b.get(u, timeout=40)
            rows = ""
            if r.status_code == 200 and len(r.content) > 500:
                try:
                    if u.endswith(".zip"):
                        z = zipfile.ZipFile(io.BytesIO(r.content))
                        rows = f", {len(z.read(z.namelist()[0]).splitlines())} rows"
                    else:
                        rows = f", {len(r.content.splitlines())} rows"
                except Exception: rows = ", unreadable"
            results[tag] = line(tag, r.status_code == 200 and len(r.content) > 500,
                                f"HTTP {r.status_code}, {len(r.content):>8} bytes{rows}")
        except Exception as e:
            results[tag] = line(tag, False, f"{type(e).__name__}: {str(e)[:60]}")
        time.sleep(1.5)

    print("=" * 72)
    good = [k for k, v in results.items() if v]
    print(f"reachable: {len(good)} of {len(results)}  ->  {', '.join(good) if good else 'NOTHING'}")
    if not good:
        print("NSE refuses this runner. The news feed needs another source, and the")
        print("bhavcopy switch is off the table until it is fetched somewhere else.")
    print("This step never fails the run.")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"probe crashed, which is still not fatal: {type(e).__name__}: {e}")
        sys.exit(0)
