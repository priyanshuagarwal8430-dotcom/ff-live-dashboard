"""
FF_market_update.py  --  builds docs/data/market.json for the dashboard's
market strip. Runs daily, before FF_dashboard_data.py.

Two sources, chosen by what each one can actually answer:

  NSE /api/allIndices   the five Nifty rows, MICROCAP 250 included.
      Probing settled this: NSE's historical endpoint is blocked from a
      GitHub runner (it serves a noindex block page, for the NIFTY 500
      control too), but allIndices answers and carries last, 1D, one-week-ago,
      30-day and 365-day changes ready-made. Yahoo has no history at all for
      MICROCAP 250 - its own Historical Data tab returns one row for a
      one-year range - so NSE is the only source that can carry that row.

  Yahoo                 gold, crude, bitcoin, USD/INR, dollar index.
      MCX is 403 on every path, so rupee gold and crude are DERIVED from the
      dollar contract times USD/INR. That is international gold, not the MCX
      screen: it carries no import duty and will read below MCX. The row is
      labelled so, and the label is not decoration - anyone comparing it to a
      broker screen must be able to see why the numbers differ.

ONE definition of every window, on both sources: N CALENDAR days back, using
the last close at or before that date. NSE's own 365-day change is calendar
based; a trading-day count would put a different meaning in the same column
as a calendar-day count, and nothing on the page would show it. Measured on
8 Sep 2026 the two conventions disagreed by 1.9 points on the Nifty 500 1Y -
large enough to matter, quiet enough to miss.

6M is the one window NSE does not publish. Rather than leave it out or fake
it, this appends every day's close to indices/market_history.csv and computes
6M from that file once it reaches back far enough. Until then the field is
null and the dashboard shows a dash. It fills itself in.

Never fatal. A market strip that fails must not take the signal dashboard
down with it, so every failure path writes what it has and exits 0.
"""

import csv
import json
import os
import sys
from datetime import date, datetime, timedelta

OUT_DIR = "docs/data"
OUT = os.path.join(OUT_DIR, "market.json")
HIST = os.path.join("indices", "market_history.csv")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 30
TROY_OZ_G = 31.1034768          # grams in a troy ounce; gold quotes per 10g

# Calendar days back for each window. Not trading days - see the note above.
WINDOWS = [("w", 7), ("m", 30), ("s", 182), ("y", 365)]

# The five index rows, in display order, with the exact name NSE returns.
NSE_ROWS = [
    ("Nifty 50",           "NIFTY 50"),
    ("Nifty 500",          "NIFTY 500"),
    ("Nifty Midcap 150",   "NIFTY MIDCAP 150"),
    ("Nifty Smallcap 250", "NIFTY SMALLCAP 250"),
    ("Nifty Microcap 250", "NIFTY MICROCAP 250"),
]


# ------------------------------------------------------------- formatting ---
def inr_group(x, dp=2):
    """Indian digit grouping: 1,35,522.40, not 135,522.40. Python's own
    thousands separator cannot do this, and a rupee figure grouped the
    western way looks wrong to every reader this page has."""
    if x is None:
        return "—"
    neg = x < 0
    s = f"{abs(float(x)):.{dp}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    out = whole + ("." + frac if frac else "")
    return ("−" if neg else "") + out


def pct(now, then):
    """Percent change, or None when the reference point is missing or zero.
    None is a real answer here and must survive to the page as a dash."""
    if now is None or then in (None, 0):
        return None
    try:
        return (float(now) / float(then) - 1.0) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def at_or_before(series, target):
    """series: list of (date, close) ascending. Returns the last close at or
    before target, or None. Markets close on weekends and holidays, so an
    exact-date lookup silently returns nothing roughly two days in seven."""
    prev = None
    for d, c in series:
        if d <= target:
            prev = c
        else:
            break
    return prev


# ------------------------------------------------------------------- NSE ---
def nse_all_indices():
    """Returns {NSE index name: row dict}, or {} on any failure."""
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        # NOT br: requests needs a separate package for brotli that the runner
        # does not have, and without it every body comes back as mojibake.
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    for url in ("https://www.nseindia.com/",
                "https://www.nseindia.com/market-data/live-market-indices"):
        try:
            s.get(url, timeout=TIMEOUT)
        except Exception as e:
            print(f"  warm-up {url}: {type(e).__name__}")
    try:
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=TIMEOUT,
                  headers={"Accept": "application/json, text/plain, */*",
                           "Referer": "https://www.nseindia.com/market-data/"
                                      "live-market-indices",
                           "X-Requested-With": "XMLHttpRequest"})
    except Exception as e:
        print(f"  allIndices: {type(e).__name__}: {e}")
        return {}
    if r.status_code != 200 or "json" not in (r.headers.get("content-type") or ""):
        print(f"  allIndices: HTTP {r.status_code}, "
              f"{r.headers.get('content-type')} - not usable")
        return {}
    try:
        rows = (r.json() or {}).get("data") or []
    except Exception as e:
        print(f"  allIndices: unparseable ({e})")
        return {}
    print(f"  allIndices: {len(rows)} indices")
    return {str(x.get("index") or "").upper(): x for x in rows}


def num(v):
    try:
        return None if v in (None, "", "-") else float(v)
    except (TypeError, ValueError):
        return None


def row_from_nse(label, rec, hist_series, today):
    """One index row. 1D/1W/1M/1Y come from NSE's own fields; 6M comes from
    our appended history, and is None until that file reaches back far
    enough."""
    last = num(rec.get("last"))
    out = {"n": label, "p": inr_group(last), "raw": last, "src": "NSE",
           "d": num(rec.get("percentChange")),
           "w": pct(last, num(rec.get("oneWeekAgoVal"))),
           "m": num(rec.get("perChange30d")),
           "s": None,
           "y": num(rec.get("perChange365d"))}
    ref = at_or_before(hist_series, today - timedelta(days=182))
    out["s"] = pct(last, ref)
    return out


# ----------------------------------------------------------------- Yahoo ---
def yahoo_series(sym, period="2y"):
    """[(date, close)] ascending, or [] on failure."""
    import warnings
    warnings.filterwarnings("ignore")
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError as e:
        print(f"  yahoo: missing dependency {e}")
        return []
    try:
        df = yf.download(sym, period=period, interval="1d", progress=False,
                         auto_adjust=False, threads=False)
    except Exception as e:
        print(f"  {sym}: {type(e).__name__}: {e}")
        return []
    if df is None or len(df) == 0:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    ser = df["Close"].dropna()
    return [(d.date(), float(c)) for d, c in ser.items()]


def combine(a, b, fn):
    """Two daily series to one, on the dates both have. Gold in rupees is a
    dollar price times a rupee rate, and the two feeds do not share a holiday
    calendar - pairing by position instead of by date would quietly shift one
    against the other."""
    mb = dict(b)
    return [(d, fn(ca, mb[d])) for d, ca in a if d in mb]


def row_from_series(label, series, today, dp=2, note=None):
    if len(series) < 2:
        return {"n": label, "p": "—", "raw": None, "src": "Yahoo",
                "d": None, "w": None, "m": None, "s": None, "y": None,
                "note": note}
    last_d, last = series[-1]
    out = {"n": label, "p": inr_group(last, dp), "raw": last, "src": "Yahoo",
           "d": pct(last, series[-2][1]), "note": note}
    for key, days in WINDOWS:
        out[key] = pct(last, at_or_before(series, last_d - timedelta(days=days)))
    return out


# --------------------------------------------------------------- history ---
def read_history():
    """{index name: [(date, close)] ascending}"""
    out = {}
    if not os.path.exists(HIST):
        return out
    with open(HIST, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d = datetime.strptime(row["date"], "%Y-%m-%d").date()
                out.setdefault(row["index"], []).append((d, float(row["close"])))
            except (KeyError, ValueError):
                continue
    for k in out:
        out[k].sort()
    return out


def append_history(rows, today):
    """Append today's closes, once per index per day. This file is what makes
    the 6M column possible later; it is append-only and never rewritten."""
    have = read_history()
    new = []
    for label, _ in NSE_ROWS:
        r = rows.get(label)
        if not r or r.get("raw") is None:
            continue
        if any(d == today for d, _ in have.get(label, [])):
            continue
        new.append({"date": today.isoformat(), "index": label,
                    "close": f"{r['raw']:.6g}"})
    if not new:
        return 0
    os.makedirs(os.path.dirname(HIST) or ".", exist_ok=True)
    exists = os.path.exists(HIST)
    with open(HIST, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "index", "close"])
        if not exists:
            w.writeheader()
        w.writerows(new)
    return len(new)


# ------------------------------------------------------------- self-tests ---
def selftest():
    fails = []

    def eq(got, want, what):
        if got != want:
            fails.append(f"{what}: got {got!r}, want {want!r}")

    eq(inr_group(23635.1), "23,635.10", "lakh grouping")
    eq(inr_group(135522.5), "1,35,522.50", "indian grouping")
    eq(inr_group(7417004, 0), "74,17,004", "crore grouping")
    eq(inr_group(94.81), "94.81", "small number")
    eq(inr_group(None), "—", "none formats as dash")
    eq(inr_group(-1234.5), "−1,234.50", "negative")

    eq(round(pct(110, 100), 9), 10.0, "pct")
    eq(pct(100, 0), None, "pct by zero")
    eq(pct(None, 100), None, "pct of none")

    # A weekend target must fall back to the previous close, not vanish.
    ser = [(date(2026, 9, 1), 100.0), (date(2026, 9, 4), 110.0)]
    eq(at_or_before(ser, date(2026, 9, 6)), 110.0, "weekend falls back")
    eq(at_or_before(ser, date(2026, 8, 31)), None, "before start is none")

    # combine must pair on dates, not position.
    a = [(date(2026, 9, 1), 10.0), (date(2026, 9, 2), 20.0),
         (date(2026, 9, 3), 30.0)]
    b = [(date(2026, 9, 1), 2.0), (date(2026, 9, 3), 3.0)]
    eq(combine(a, b, lambda x, y: x * y),
       [(date(2026, 9, 1), 20.0), (date(2026, 9, 3), 90.0)], "combine on dates")

    # An NSE row with no history yet must still produce a row, with 6M null.
    rec = {"last": 23635.1, "percentChange": -0.61, "oneWeekAgoVal": 23000.0,
           "perChange30d": -3.81, "perChange365d": -4.59}
    r = row_from_nse("Nifty 50", rec, [], date(2026, 9, 8))
    eq(r["s"], None, "6M null without history")
    eq(round(r["w"], 4), round(pct(23635.1, 23000.0), 4), "1W from oneWeekAgoVal")
    eq(r["p"], "23,635.10", "nse row formats price")

    print("self-test:", "PASSED" if not fails else "FAILED")
    for f in fails:
        print("  -", f)
    return not fails


# -------------------------------------------------------------------- main ---
def main():
    today = date.today()
    hist = read_history()
    rows = {}

    print("NSE:")
    idx = nse_all_indices()
    for label, nse_name in NSE_ROWS:
        rec = idx.get(nse_name.upper())
        if not rec:
            print(f"  {label}: NOT RETURNED by allIndices")
            continue
        rows[label] = row_from_nse(label, rec, hist.get(label, []), today)
        print(f"  {label}: {rows[label]['p']}")

    print("Yahoo:")
    usdinr = yahoo_series("USDINR=X")
    gold_usd = yahoo_series("GC=F")
    crude_usd = yahoo_series("CL=F")

    if gold_usd and usdinr:
        gold_inr = combine(gold_usd, usdinr,
                           lambda g, fx: g * fx / TROY_OZ_G * 10.0)
        rows["Gold"] = row_from_series(
            "Gold (₹/10g)", gold_inr, today, 0,
            note="International gold in rupees, from COMEX and USD/INR. "
                 "No import duty, so it reads below the MCX screen.")
    if crude_usd and usdinr:
        crude_inr = combine(crude_usd, usdinr, lambda c, fx: c * fx)
        rows["Crude"] = row_from_series(
            "Crude oil (₹/bbl)", crude_inr, today, 0,
            note="WTI in rupees, from NYMEX and USD/INR.")

    for key, label, sym, dp in [
            ("Bitcoin", "Bitcoin (₹)", "BTC-INR", 0),
            ("USDINR", "USD / INR", "USDINR=X", 2),
            ("DXY", "Dollar index", "DX-Y.NYB", 2)]:
        ser = yahoo_series(sym)
        if ser:
            rows[key] = row_from_series(label, ser, today, dp)
    for k in ("Gold", "Crude", "Bitcoin", "USDINR", "DXY"):
        if k in rows:
            print(f"  {rows[k]['n']}: {rows[k]['p']}")

    added = append_history(rows, today)
    print(f"history: {added} rows appended to {HIST}")

    order = [lbl for lbl, _ in NSE_ROWS] + ["Gold", "Crude", "Bitcoin",
                                            "USDINR", "DXY"]
    out_rows = [rows[k] for k in order if k in rows]
    for r in out_rows:
        r.pop("raw", None)

    payload = {
        "as_of": today.isoformat(),
        "rows": out_rows,
        # The page must be able to say where a number came from without the
        # reader having to ask, so the convention travels with the data.
        "window_basis": "calendar days: 7, 30, 182, 365",
        "six_month_note": "6M for the Nifty rows is built from this repo's own "
                          "daily record and stays blank until it reaches back "
                          "six months.",
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"wrote {OUT} with {len(out_rows)} rows")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    try:
        main()
    except Exception as e:
        # Never fatal: the signal dashboard matters more than the strip.
        print(f"market update failed: {type(e).__name__}: {e}")
    sys.exit(0)
