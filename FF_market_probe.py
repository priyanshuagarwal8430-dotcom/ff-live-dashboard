"""
FF_market_probe.py  --  probe v4. One-shot, not part of the daily run.

v3 answered nothing. It sent "Accept-Encoding: gzip, deflate, br"; NSE replied
in brotli; requests cannot decode brotli without a package the runner lacks, so
every body came back as mojibake - including /api/allIndices and the G-Sec feed,
both of which had parsed perfectly in v2. That was a self-inflicted wound from
one header. v4 drops br and prints Content-Encoding on every call so the same
mistake cannot hide again.

The one thing v3 did establish: visiting the historical-data page during warm-up
yields an "nsit" cookie that v2 never had, and nsit is what NSE gates its
historical endpoints on. So A is worth asking once more.

What v2 established, so v3 does not repeat it:

  * MCX is 403 on every path. Gold and crude stay on GC=F and CL=F converted
    with USDINR. That question is CLOSED - not probed here.
  * Yahoo already carries Nifty 50, 500, Midcap 150 and Smallcap 250 with
    ~490 daily closes each. Those rows do not need NSE. Only MICROCAP 250
    does, because Yahoo has no history for it at all.
  * NSE's /api/historical/indicesHistory returned an HTML page, not JSON -
    and it did so for the NIFTY 500 control too, so the index NAMES were
    never the problem. Meanwhile /api/liveBonds-traded-on-cm on the SAME
    session returned 200 and 5.4 MB of JSON. So the block is specific to the
    historical endpoint, not to the session.

v3 therefore asks three narrow questions and prints evidence rather than
verdicts. Where something fails it prints what the server actually said, so
the next step is a decision and not another guess.

  A. Why does the historical endpoint refuse, and can a better warm-up fix it?
  B. /api/allIndices - does it carry MICROCAP 250 with ready-made changes?
     If it does, Microcap needs no history endpoint at all: we take today's
     value and append it daily, exactly as indices/Nifty500.csv already does.
  C. What is actually inside the G-Sec JSON - is a 10-year yield derivable?

Nothing is written. It only prints.
"""

import json
import sys
import warnings

warnings.filterwarnings("ignore")

try:
    import requests
except ImportError as e:
    print("MISSING DEPENDENCY:", e)
    sys.exit(0)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
LINE = "-" * 96


def rule(t):
    print("\n" + "=" * 96)
    print(t)
    print("=" * 96)


def session():
    """v2 warmed up on the homepage (403) and the live-indices page (200, 3
    cookies). The historical API is served off a different section of the
    site, so this walks the historical pages too and reports which cookies
    each step actually contributes."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        # NOT br. requests decompresses gzip and deflate on its own but needs a
        # separate brotli package for br, which the runner does not have. v3 asked
        # for br, got it, could not decode it, and turned every response into
        # mojibake - including two endpoints that had parsed fine in v2.
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Connection": "keep-alive",
    })
    for url in ("https://www.nseindia.com/",
                "https://www.nseindia.com/market-data/live-market-indices",
                "https://www.nseindia.com/reports-indices-historical-index-data",
                "https://www.nseindia.com/all-reports"):
        try:
            r = s.get(url, timeout=TIMEOUT)
            print(f"  {url[:62]:62s} HTTP {r.status_code}  "
                  f"cookies now: {sorted(c.name for c in s.cookies)}")
        except Exception as e:
            print(f"  {url[:62]:62s} {type(e).__name__}: {e}"[:96])
    return s


def api(s, url, referer):
    """One API call, reported honestly: status, type, and - when the answer is
    not JSON - the first 400 characters of whatever came back instead."""
    h = {"Accept": "application/json, text/plain, */*",
         "Referer": referer,
         "Sec-Fetch-Dest": "empty",
         "Sec-Fetch-Mode": "cors",
         "Sec-Fetch-Site": "same-origin",
         "X-Requested-With": "XMLHttpRequest"}
    try:
        r = s.get(url, headers=h, timeout=TIMEOUT)
    except Exception as e:
        print(f"    {type(e).__name__}: {e}"[:94])
        return None
    ct = (r.headers.get("content-type") or "")[:38]
    enc = r.headers.get("content-encoding") or "none"
    print(f"    HTTP {r.status_code}  {len(r.content):9,d} bytes  {ct}  "
          f"encoding={enc}")
    if enc not in ("none", "gzip", "deflate", "identity"):
        print(f"    WARNING: {enc} is not decoded by requests here - any body "
              f"below is raw bytes, not the real answer.")
    if "json" not in ct:
        body = (r.text or "").strip().replace("\n", " ")
        print(f"    NOT JSON. server said, first 400 chars:")
        print(f"      {body[:400]}")
        return None
    try:
        return r.json()
    except Exception as e:
        print(f"    json header but unparseable: {e}"[:94])
        return None


# ------------------------------------------------------- A. historical API ---
rule("A.  Why does /api/historical/indicesHistory refuse?")
print("warm-up chain:")
s = session()

REF_HIST = "https://www.nseindia.com/reports-indices-historical-index-data"
REF_HOME = "https://www.nseindia.com/"

for label, name in [("CONTROL  NIFTY 500", "NIFTY%20500"),
                    ("TARGET   NIFTY MICROCAP250", "NIFTY%20MICROCAP250")]:
    url = ("https://www.nseindia.com/api/historical/indicesHistory"
           f"?indexType={name}&from=01-09-2025&to=08-09-2026")
    for ref_label, ref in [("referer = historical page", REF_HIST),
                           ("referer = home", REF_HOME)]:
        print(f"\n  {label}   [{ref_label}]")
        j = api(s, url, ref)
        if j:
            recs = (j.get("data") or {}).get("indexCloseOnlineRecords") or []
            print(f"    JSON OK. records = {len(recs)}")
            if recs:
                print(f"    first record keys: {sorted(recs[0])[:10]}")
                print(f"    newest: {json.dumps(recs[0])[:220]}")
            break


# ------------------------------------------------------------ B. allIndices ---
# The important one. If this carries MICROCAP 250, the missing history stops
# mattering: NSE hands us today's level plus ready-made 30-day and 365-day
# changes, and we append the daily close ourselves from here on - the same
# pattern indices/Nifty500.csv already runs on.
rule("B.  /api/allIndices   -   does it carry MICROCAP 250?")
j = api(s, "https://www.nseindia.com/api/allIndices",
        "https://www.nseindia.com/market-data/live-market-indices")
if j:
    rows = j.get("data") or []
    print(f"    indices returned: {len(rows)}")
    if rows:
        print(f"    fields available: {sorted(rows[0])}")
    WANT = ("NIFTY 50", "NIFTY 500", "NIFTY MIDCAP 150",
            "NIFTY SMALLCAP 250", "NIFTY MICROCAP 250", "NIFTY MICROCAP250")
    print("\n    the rows we care about:")
    print(f"    {'index':26s} {'last':>12s} {'%1D':>8s} {'%30d':>8s} {'%365d':>8s}")
    print("    " + "-" * 66)
    found = set()
    for r in rows:
        nm = str(r.get("index") or r.get("indexSymbol") or "")
        if nm.upper() in WANT:
            found.add(nm.upper())
            g = lambda k: r.get(k)
            print(f"    {nm:26s} {str(g('last')):>12s} "
                  f"{str(g('percentChange')):>8s} {str(g('perChange30d')):>8s} "
                  f"{str(g('perChange365d')):>8s}")
    missing = [w for w in WANT if w not in found]
    print(f"\n    not found under these names: {missing}")
    print("\n    every index name NSE actually returns that mentions MICRO or CAP:")
    for r in rows:
        nm = str(r.get("index") or "")
        if "MICRO" in nm.upper() or "CAP" in nm.upper():
            print(f"      {nm}")


# ------------------------------------------------------------- C. G-Sec JSON ---
# 3,525 rows came back on this endpoint in v2. The question is only whether a
# 10-year benchmark yield can be read out of it - so print the shape and a few
# real rows, and decide afterwards.
rule("C.  G-Sec JSON   -   is a 10-year yield in there?")
j = api(s, "https://www.nseindia.com/api/liveBonds-traded-on-cm?type=gsec",
        "https://www.nseindia.com/market-data/bonds-traded-in-capital-market")
if j:
    rows = j.get("data") or []
    print(f"    rows: {len(rows)}")
    if rows:
        print(f"    fields: {sorted(rows[0])}")
        print("\n    first 3 rows in full:")
        for r in rows[:3]:
            print("      " + json.dumps(r)[:300])
        # A 10-year benchmark matures roughly ten years out. Rather than guess
        # which security is the benchmark, show what the maturity-ish and
        # yield-ish fields look like across a handful of rows.
        yk = [k for k in rows[0] if "yld" in k.lower() or "yield" in k.lower()]
        mk = [k for k in rows[0] if "mat" in k.lower() or "expiry" in k.lower()]
        print(f"\n    yield-looking fields  : {yk}")
        print(f"    maturity-looking fields: {mk}")
        if yk:
            vals = [r.get(yk[0]) for r in rows[:40]]
            print(f"    sample of {yk[0]}: {vals}")

rule("done")
print("A: if the historical endpoint answered, Microcap can have real history.")
print("B: if allIndices carries MICROCAP 250, we do not need A at all.")
print("C: decides whether the bond row can be a real yield or must be dropped.")
print("MCX is settled and not probed: gold and crude stay on GC=F / CL=F.")
