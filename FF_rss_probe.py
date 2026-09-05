"""
FF — which macro news feeds answer a GitHub runner?
====================================================
TEMPORARY. Delete with its workflow step once a source is chosen.

NSE covers the portfolio and the index. It has nothing on RBI policy, CPI, GST,
flows or global cues, so the "India & world" pane needs a different source. This
asks which candidates are actually reachable and actually publish, before any of
them is built on.

Two classes, and they are not the same thing:

  OFFICIAL   RBI, PIB, SEBI, MoSPI. Public documents. Free to publish in full.
  MEDIA      NDTV Profit, Moneycontrol, Trading Economics, ET, Mint, Business
             Standard. Reachable and free to READ, but the copy belongs to them.
             If any of these is used it is HEADLINE + SOURCE + LINK only - never
             the article text, never a rewritten summary. That is the form
             aggregators use and the only one defensible on a paid product.

Bloomberg and Reuters are absent on purpose: their content is licensed through
the Terminal and through LSEG, priced for institutions, and their terms forbid
redistribution. There is no free door.

Read only. Never fatal.
"""
import sys, time, re
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FEEDS = [
  ("OFFICIAL", "RBI press releases",   "https://www.rbi.org.in/pressreleases_rss.xml"),
  ("OFFICIAL", "RBI notifications",    "https://www.rbi.org.in/notifications_rss.xml"),
  ("OFFICIAL", "PIB releases",         "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"),
  ("OFFICIAL", "SEBI press releases",  "https://www.sebi.gov.in/sebirss.xml"),
  ("MEDIA",    "NDTV Profit",          "https://feeds.feedburner.com/ndtvprofit-latest"),
  ("MEDIA",    "NDTV business",        "https://feeds.feedburner.com/ndtvnews-business"),
  ("MEDIA",    "Moneycontrol latest",  "https://www.moneycontrol.com/rss/latestnews.xml"),
  ("MEDIA",    "Moneycontrol economy", "https://www.moneycontrol.com/rss/economy.xml"),
  ("MEDIA",    "Moneycontrol markets", "https://www.moneycontrol.com/rss/marketreports.xml"),
  ("MEDIA",    "Trading Economics IN", "https://tradingeconomics.com/india/rss"),
  ("MEDIA",    "ET markets",           "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
  ("MEDIA",    "Business Standard mkt","https://www.business-standard.com/rss/markets-106.rss"),
  ("MEDIA",    "Livemint markets",     "https://www.livemint.com/rss/markets"),
]

def probe(url):
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,*/*"},
                     timeout=30)
    if r.status_code != 200: return False, f"HTTP {r.status_code}", None
    body = r.text
    items = re.findall(r"<item[ >].*?</item>", body, re.S) or \
            re.findall(r"<entry[ >].*?</entry>", body, re.S)
    if not items: return False, f"HTTP 200, {len(r.content)} bytes, no <item>", None
    m = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", items[0], re.S)
    d = re.search(r"<(?:pubDate|updated|dc:date)[^>]*>(.*?)</", items[0], re.S)
    title = re.sub(r"\s+", " ", (m.group(1) if m else "")).strip()[:74]
    when  = re.sub(r"\s+", " ", (d.group(1) if d else "")).strip()[:31]
    return True, f"HTTP 200, {len(r.content):>7} bytes, {len(items):>3} items", (when, title)

def main():
    print("=" * 78)
    print("Macro news feeds — reachable from this runner? read only, never fatal")
    print("=" * 78)
    ok = {"OFFICIAL": [], "MEDIA": []}
    for kind, name, url in FEEDS:
        try:
            good, note, newest = probe(url)
        except Exception as e:
            good, note, newest = False, f"{type(e).__name__}: {str(e)[:44]}", None
        print(f"  {'OK  ' if good else 'FAIL'}  {kind:8} {name:22} {note}")
        if newest: print(f"          newest: {newest[0]}  {newest[1]}")
        if good: ok[kind].append(name)
        time.sleep(1.0)
    print("=" * 78)
    print(f"OFFICIAL reachable ({len(ok['OFFICIAL'])}): {', '.join(ok['OFFICIAL']) or 'none'}")
    print(f"MEDIA    reachable ({len(ok['MEDIA'])}): {', '.join(ok['MEDIA']) or 'none'}")
    print("Official feeds can be published in full. Media feeds, if used at all,")
    print("are headline + source + link only.")
    return 0

if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as e:
        print(f"probe crashed, still not fatal: {type(e).__name__}: {e}"); sys.exit(0)
