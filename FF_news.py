"""
FF — the news card
==================
Writes docs/data/news.json in four blocks, and they are kept apart on purpose:

  portfolio  NSE filings for the stocks currently held
  market     NSE filings across the rest of the Nifty 500
  policy     RBI and SEBI, published in full
  press      Indian financial media, HEADLINE + SOURCE + LINK only

WHY FOUR AND NOT ONE
An NSE filing is the company's own document — a fact, primary source. A Livemint
headline is somebody's writing about a fact. Mixed into one list a reader cannot
tell which is which, and on a paid advisory that difference matters. So they sit
in separate blocks with the source named.

WHY THE PRESS BLOCK CARRIES NO TEXT
That copy belongs to the outlet. Headline, source and a link to their page is the
form aggregators use and the only one defensible on a product someone pays for.
Never the article, never a summary of it. Bloomberg and Reuters are absent
because their feeds are licensed through the Terminal and through LSEG and
forbid redistribution — there is no free door, only an unpaid one.

WHY THE NSE BLOCKS ARE ALLOW-LISTED
The first run pulled 4,866 announcements and the top of the list read
"Certificate under SEBI (Depositories and Participants) Regulations", "Press
Release", "Updates", "Copy of Newspaper Publication". NSE's `desc` is a filing
category, and most Regulation 30 filings are routine compliance paper. One of the
five was worth reading. So only the categories that move a position get through.

WHY MEDIA ITEMS ARE FILTERED
The NDTV feed's newest item was a cricket live-stream and Livemint's was American
crop prices. A client opening a paid equity advisory and finding sport does not
find it livelier; they find it unserious.

NEVER FATAL. Any source that fails is skipped and reported; the run continues and
the previous news.json stands.
"""
import json, os, re, sys, time
from datetime import date, timedelta
import pandas as pd, requests

OUT, LED, IDENT = "docs/data/news.json", "signal_ledger.csv", "ident.csv"
NSE_WINDOW, PRESS_DAYS = 7, 4
KEEP = dict(portfolio=40, market=40, policy=25, press=30)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
     "Accept": "application/json, text/plain, */*"}
REF = {"Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions"}

# Only what can move a position. Everything else is compliance paper.
KEEP_RE = re.compile(r"""
 financial\s+result|unaudited|audited\s+result|quarterly\s+result
|dividend|bonus|stock\s+split|sub-?division|rights\s+issue|buy\s?back|record\s+date
|board\s+meeting|credit\s+rating|rating\s+action
|acquisi|merger|amalgamat|demerger|scheme\s+of\s+arrangement|open\s+offer|delisting
|fund\s+rais|qip|preferential\s+(issue|allotment)|rights\s+entitlement
|order\s+win|receipt\s+of\s+order|letter\s+of\s+award|contract\s+worth|bags?\s+order
|capacity\s+expansion|commission(ing|ed)|new\s+plant
|resignation|appointment\s+of\s+(managing|chief|whole)
""", re.I | re.X)

# A paid equity advisory does not carry these, however lively they are.
DROP_RE = re.compile(r"""
 cricket|\bipl\b|\bt20\b|\bodi\b|world\s+cup|football|kabaddi|tennis|olympic
|live\s+stream|match\s+(preview|report)|playing\s+xi
|box\s+office|bollywood|movie|film\b|trailer|web\s+series|actor|actress|celebrit
|horoscope|astrolog|recipe|lifestyle|travel\s+guide|wedding|fashion
""", re.I | re.X)

OFFICIAL = [("RBI", "https://www.rbi.org.in/pressreleases_rss.xml"),
            ("RBI", "https://www.rbi.org.in/notifications_rss.xml"),
            ("SEBI", "https://www.sebi.gov.in/sebirss.xml")]
PRESS = [("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
         ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
         ("Mint", "https://www.livemint.com/rss/markets"),
         ("NDTV Profit", "https://feeds.feedburner.com/ndtvprofit-latest")]

log = []
def note(m): log.append(m)

def clean(t):
    t = re.sub(r"<!\[CDATA\[|\]\]>", "", t or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
          .replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", t).strip()

def norm_date(v):
    if not v: return None
    t = pd.to_datetime(str(v), errors="coerce", dayfirst=True, utc=True)
    return None if pd.isna(t) else str(t.date())

# ------------------------------------------------------------------- NSE ---
def nse_session():
    s = requests.Session(); s.headers.update(H)
    # The handshake answers 403 from a GitHub runner and the APIs work anyway.
    # Proved 5 September 2026. A failed handshake is not a reason to stop.
    try: s.get("https://www.nseindia.com/", timeout=20)
    except Exception: pass
    return s

def pick(d, *names):
    for n in names:
        if n in d and d[n] not in (None, ""): return d[n]
    return None

def headline_of(r):
    """attchmntText carries the sentence; desc is only the filing's category.
    "X Limited has informed the Exchange about Y" -> "Y"."""
    txt = pick(r, "attchmntText")
    if txt:
        t = clean(txt)
        m = re.match(r".{2,80}?\bhas informed the Exchange about\b\s*(.+)$", t, re.I)
        t = (m.group(1) if m else t).strip(" .")
        if len(t) > 12: return t[:200]
    d = pick(r, "desc", "purpose", "subject", "bm_desc")
    return clean(d)[:200] if d else None

def category(text):
    t = (text or "").lower()
    if re.search(r"financial\s+result|unaudited|audited\s+result|quarterly\s+result", t): return "Results"
    if re.search(r"dividend|bonus|split|sub-?division|rights|buy\s?back|record\s+date", t): return "Corporate action"
    if "board meeting" in t: return "Board meeting"
    if "rating" in t: return "Credit rating"
    if re.search(r"acquisi|merger|amalgamat|demerger|open\s+offer", t): return "Deal"
    if re.search(r"order|contract|award", t): return "Order win"
    return "Company update"

def from_nse():
    d1 = (date.today() - timedelta(days=NSE_WINDOW)).strftime("%d-%m-%Y")
    d2 = date.today().strftime("%d-%m-%Y")
    s, out = nse_session(), []
    for label, path, dk in (
        ("event-calendar", f"/api/event-calendar?index=equities&from_date={d1}&to_date={d2}",
         ("date",)),
        ("corporate-announcements", f"/api/corporate-announcements?index=equities&from_date={d1}&to_date={d2}",
         ("an_dt", "sort_date", "exchdisstime")),
        ("corporate-actions", f"/api/corporates-corporateActions?index=equities&from_date={d1}&to_date={d2}",
         ("exDate", "ex_dt", "exdate"))):
        try:
            r = s.get("https://www.nseindia.com" + path, headers={**H, **REF}, timeout=45)
            rows = r.json() if r.status_code == 200 else []
            rows = rows if isinstance(rows, list) else rows.get("data", [])
        except Exception as e:
            note(f"  {label:24} {type(e).__name__}: {str(e)[:44]}"); continue
        kept = 0
        for x in rows:
            sym, dt = pick(x, "symbol", "SYMBOL"), norm_date(pick(x, *dk))
            head = headline_of(x)
            if not (sym and dt and head): continue
            if not KEEP_RE.search(head + " " + str(pick(x, "desc", "purpose", "subject") or "")):
                continue
            out.append(dict(date=dt, symbol=str(sym).upper(), category=category(head),
                            headline=head, url=pick(x, "attchmntFile"), source="NSE"))
            kept += 1
        note(f"  {label:24} {len(rows):>5} fetched, {kept:>3} kept")
        time.sleep(1.5)
    return out

# -------------------------------------------------------------------- RSS ---
def from_rss(feeds, full, cutoff_days, tag):
    out = []
    for name, url in feeds:
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                           "Accept": "application/rss+xml,*/*"}, timeout=30)
            if r.status_code != 200:
                note(f"  {name:24} HTTP {r.status_code}"); continue
            body = r.content.decode("utf-8", "replace")
            items = re.findall(r"<item[ >].*?</item>", body, re.S) or \
                    re.findall(r"<entry[ >].*?</entry>", body, re.S)
        except Exception as e:
            note(f"  {name:24} {type(e).__name__}: {str(e)[:44]}"); continue
        cut, kept = str(date.today() - timedelta(days=cutoff_days)), 0
        for it in items[:60]:
            t = re.search(r"<title[^>]*>(.*?)</title>", it, re.S)
            l = re.search(r"<link[^>]*>(.*?)</link>", it, re.S)
            d = re.search(r"<(?:pubDate|updated|dc:date)[^>]*>(.*?)</", it, re.S)
            head = clean(t.group(1)) if t else None
            when = norm_date(clean(d.group(1))) if d else None
            if not head or not when or when < cut: continue
            if DROP_RE.search(head): continue
            out.append(dict(date=when, symbol=name, category=tag, headline=head[:190],
                            url=clean(l.group(1)) if l else None, source=name,
                            full=bool(full)))
            kept += 1
        note(f"  {name:24} {len(items):>5} items,   {kept:>3} kept")
        time.sleep(0.8)
    return out

# ------------------------------------------------------------------- main ---
def main():
    nse = from_nse()
    policy = from_rss(OFFICIAL, True, 10, "Policy")
    press  = from_rss(PRESS,   False, PRESS_DAYS, "Press")

    if not (nse or policy or press):
        print("\n".join(log)); print("nothing fetched — news.json left as it was"); return 0

    held = set(pd.read_csv(LED).symbol.astype(str).str.upper()) if os.path.exists(LED) else set()
    uni  = set(pd.read_csv(IDENT).symbol.astype(str).str.upper()) if os.path.exists(IDENT) else set()

    def dedup(rows):
        seen, out = set(), []
        for r in sorted(rows, key=lambda x: (x["date"], x["headline"]), reverse=True):
            k = (r["symbol"], r["headline"][:70])
            if k in seen: continue
            seen.add(k); out.append(r)
        return out

    nse = dedup(nse)
    out = dict(as_of=str(date.today()),
               portfolio=[r for r in nse if r["symbol"] in held][:KEEP["portfolio"]],
               market=[r for r in nse if r["symbol"] in uni and r["symbol"] not in held][:KEEP["market"]],
               policy=dedup(policy)[:KEEP["policy"]],
               press=dedup(press)[:KEEP["press"]])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print("\n".join(log))
    print(f"  -> {OUT}: holdings {len(out['portfolio'])}, market {len(out['market'])}, "
          f"policy {len(out['policy'])}, press {len(out['press'])}")
    for k in ("portfolio", "policy", "press"):
        for i in out[k][:3]:
            print(f"       {k:9} {i['date']}  {i['symbol']:16} [{i['category']}] {i['headline'][:58]}")
    return 0

if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as e:
        print(f"news build failed, not fatal: {type(e).__name__}: {e}"); sys.exit(0)
