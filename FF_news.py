"""
FF — news feed from NSE filings
================================
Writes the portfolio and market panes of docs/data/news.json.

WHAT IT PUBLISHES, AND WHY ONLY THIS
NSE's own filings: results announcements, board meetings, corporate actions and
Regulation 30 disclosures. These are public documents filed by the companies
themselves, so there is no licensing question and no editorial middle-man. It is
also the only news that actually touches a position.

The "India & world" pane is NOT written here. NSE has nothing on RBI policy, CPI,
GST or global cues, and the media that do carry them own that copy. That pane
stays empty until a source is settled — an empty pane is honest; someone else's
copy on a paid product is not.

NEVER FATAL. If NSE refuses or the shape changes, the existing news.json is left
alone and the run continues. Stale news is a flaw on one card; a failed run stops
the prices, the signals and the ledger.

    python FF_news.py
"""
import json, os, sys, time
from datetime import date, timedelta
import pandas as pd, requests

OUT      = "docs/data/news.json"
LED      = "signal_ledger.csv"
IDENT    = "ident.csv"
WINDOW   = 7        # days. 45 days of announcements is 21 MB; a daily run needs a week.
KEEP     = 40       # per pane

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
     "Accept": "application/json, text/plain, */*"}
REF = {"Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions"}

# The handshake returns 403 from a GitHub runner and the API endpoints answer
# anyway. Proved by FF_nse_probe.py, 5 September 2026. So a failed handshake is
# not a reason to stop - it never was.
def session():
    s = requests.Session(); s.headers.update(H)
    for u in ("https://www.nseindia.com/",):
        try: s.get(u, timeout=20)
        except Exception: pass
    return s

def get(s, path):
    r = s.get("https://www.nseindia.com" + path, headers={**H, **REF}, timeout=45)
    if r.status_code != 200: return None, f"HTTP {r.status_code}"
    try: d = r.json()
    except Exception: return None, "not JSON"
    rows = d if isinstance(d, list) else (d.get("data") or [])
    return (rows if isinstance(rows, list) else []), f"{len(rows)} records"

def pick(d, *names):
    """Read whichever spelling this endpoint uses. NSE has renamed fields before,
    and a KeyError here would take the news card down for a cosmetic reason."""
    for n in names:
        if n in d and d[n] not in (None, ""): return d[n]
    return None

def norm_date(v):
    if not v: return None
    t = pd.to_datetime(str(v), errors="coerce", dayfirst=True)
    return None if pd.isna(t) else str(t.date())

def classify(text):
    t = (text or "").lower()
    if "financial result" in t or "result" in t:            return "Results"
    if any(k in t for k in ("dividend","bonus","split","sub-division","rights",
                            "buyback","record date")):      return "Corporate action"
    if "board meeting" in t:                                return "Board meeting"
    if "rating" in t:                                       return "Credit rating"
    return "Disclosure"

def main():
    d1 = (date.today() - timedelta(days=WINDOW)).strftime("%d-%m-%Y")
    d2 = date.today().strftime("%d-%m-%Y")
    s = session()
    items, log = [], []

    for label, path, fields in (
        ("event-calendar",
         f"/api/event-calendar?index=equities&from_date={d1}&to_date={d2}",
         ("symbol", ("date",), ("purpose","bm_desc"), None)),
        ("corporate-announcements",
         f"/api/corporate-announcements?index=equities&from_date={d1}&to_date={d2}",
         ("symbol", ("an_dt","sort_date","exchdisstime"), ("desc",),
          ("attchmntText","attchmntFile"))),
        ("corporate-actions",
         f"/api/corporates-corporateActions?index=equities&from_date={d1}&to_date={d2}",
         ("symbol", ("exDate","ex_dt","exdate"), ("subject","purpose"), None)),
    ):
        try:
            rows, note = get(s, path)
        except Exception as e:
            rows, note = None, f"{type(e).__name__}: {str(e)[:50]}"
        log.append(f"  {label:26} {note}")
        for r in (rows or []):
            sym = pick(r, fields[0], "SYMBOL", "Symbol")
            dt  = norm_date(pick(r, *fields[1]))
            head = pick(r, *fields[2])
            extra = pick(r, *fields[3]) if fields[3] else None
            if not sym or not dt or not head: continue
            body = str(extra) if extra and not str(extra).startswith("http") else ""
            items.append(dict(date=dt, symbol=str(sym).upper(),
                              category=classify(f"{head} {body}"),
                              headline=str(head).strip()[:180],
                              detail=body.strip()[:220],
                              url=str(extra) if extra and str(extra).startswith("http") else None,
                              source="NSE"))
        time.sleep(1.5)

    if not items:
        print("\n".join(log))
        print("nothing fetched — existing news.json left as it was; never fatal")
        return 0

    held = set(pd.read_csv(LED).symbol.astype(str).str.upper()) if os.path.exists(LED) else set()
    uni  = set(pd.read_csv(IDENT).symbol.astype(str).str.upper()) if os.path.exists(IDENT) else set()

    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: (x["date"], x["symbol"]), reverse=True):
        k = (it["symbol"], it["date"], it["headline"][:70])
        if k in seen: continue
        seen.add(k); uniq.append(it)

    portfolio = [i for i in uniq if i["symbol"] in held][:KEEP]
    market    = [i for i in uniq if i["symbol"] in uni and i["symbol"] not in held
                 and i["category"] in ("Results", "Corporate action")][:KEEP]

    prev = {}
    if os.path.exists(OUT):
        try: prev = json.load(open(OUT, encoding="utf-8"))
        except Exception: prev = {}
    out = dict(as_of=str(date.today()), portfolio=portfolio, market=market,
               macro=prev.get("macro", []))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print("\n".join(log))
    print(f"  -> {OUT}: {len(portfolio)} for holdings, {len(market)} market-wide, "
          f"{len(out['macro'])} macro")
    for i in portfolio[:5]:
        print(f"       {i['date']}  {i['symbol']:12} [{i['category']}] {i['headline'][:66]}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"news build failed, not fatal: {type(e).__name__}: {e}")
        sys.exit(0)
