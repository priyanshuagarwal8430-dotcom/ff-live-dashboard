"""
FF — OHLC updater, NSE bhavcopy edition
============================================================================
Same guards as FF_OHLC_Updater_v2, different source. That file's decision logic
is imported unchanged rather than copied, so there is one implementation of the
rules and one set of tests over them.

WHY THE SOURCE CHANGED
----------------------
The price base this repository stands on is built from NSE bhavcopy
(archives.nseindia.com) with corporate actions from NSE's own
corporates-corporateActions API — see FF_NSE_Download.ipynb and
FF_Adjust_Final.ipynb. There is no yfinance anywhere in that chain.

The daily updater, however, used yfinance. Its docstring asserted that "your
existing OHLC dataset is itself Yahoo Finance-sourced", and that assertion was
simply wrong. Appending Yahoo rows onto an NSE base splices two vendors'
opinions of the same price together at the seam, and the difference between them
is invisible until it is large enough to matter.

Three things improve by going back to the exchange:

  1. One source end to end, so the join is not between vendors.
  2. Bhavcopy is the exchange's own record rather than anyone's derived feed.
  3. One file per trading day covers all 751 symbols, instead of 751 separate
     requests. The Yahoo run took about seven minutes; this takes well under one.

CORPORATE ACTIONS, AND THE LESSON THAT SHAPED THIS
--------------------------------------------------
Actions come from NSE's API, which gives a free-text `subject`. The v3 audit
found three events booked as equity splits that were nothing of the kind —
ZEEL's 21:1 bonus PREFERENCE shares, Dr Reddy's 6:1 bonus DEBENTURES, TVS
Motor's 4:1 NCRPS. None changes the equity share count; applying them left a
21.9x cliff and one trade that booked a fictional +245.8%.

So the subject is read for WHAT was issued, not merely for a ratio, and anything
that is not equity is refused before it reaches the price series. The observed-
price check in the shared logic then acts as a second, independent gate: even a
correctly-parsed equity action is not applied unless the price actually moved
like one.

RUN
---
    python FF_NSE_OHLC_Updater.py                # daily update
    python FF_NSE_OHLC_Updater.py --selftest     # no network
    python FF_NSE_OHLC_Updater.py --dry-run
"""

from __future__ import annotations

import argparse, io, json, os, re, sys, time, zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

from FF_OHLC_Updater_v2 import (
    OHLC_DIR, REPORT, QUARANTINE, OVERLAP_DAYS, COLS,
    update_one, atomic_write,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SERIES_OK = ("EQ", "BE")


# --------------------------------------------------------------------------
# corporate actions — the part that decides what may touch a price
# --------------------------------------------------------------------------

# Issued instead of equity. A bonus of any of these leaves the equity share
# count untouched, so the equity price must not move for it.
NON_EQUITY = ("preference", "pref share", "ncrps", "ncps", "debenture", "ncd",
              "warrant", "sdi", "bond")

def parse_action(subject: str):
    """Read an NSE corporate-action subject into (factor, note).

    factor is what the price divides by. Returns (None, reason) when the action
    does not touch the equity price, which is the normal case for most subjects
    (dividends, meetings, buybacks and so on)."""
    if not subject:
        return None, "empty subject"
    s = " ".join(str(subject).lower().split())

    for word in NON_EQUITY:
        if word in s:
            return None, f"not an equity action — mentions '{word}'"

    # A single subject often carries BOTH a bonus and a face-value split -
    # "Bonus 1:1 / Face Value Split From Rs.10/- To Re.1/-" is one event whose
    # combined effect is 20, not 2 and not 10. Reading only one half was the
    # first version's mistake here, and it under-adjusted by an order of
    # magnitude. So both are looked for and their factors multiplied.
    has_split = any(k in s for k in ("split", "sub-division", "subdivision", "sub division"))
    has_bonus = "bonus" in s
    factor, parts = 1.0, []

    if has_bonus:
        m = re.search(r"bonus\D{0,30}?(\d+)\s*[:/]\s*(\d+)", s)
        if not m or float(m.group(2)) <= 0:
            return None, "a bonus is mentioned but its ratio cannot be read"
        a, b = float(m.group(1)), float(m.group(2))
        factor *= (a + b) / b                      # b held become a+b
        parts.append(f"bonus {int(a)}:{int(b)}")

    if has_split:
        # "Rs", "Rs.", "Re.", "INR" all appear, and a face value of 1 is written
        # "Re.1/-". "From" is sometimes absent: "Face Value Split Rs.10/- To Rs.5/-".
        m = re.search(r"(?:from\s*)?(?:rs|re|inr)\.?\s*(\d+(?:\.\d+)?)\D{0,20}?"
                      r"to\s*(?:rs|re|inr)?\.?\s*(\d+(?:\.\d+)?)", s)
        if not m:
            # Refuse the whole subject rather than apply the bonus half alone.
            # Half a factor is worse than none, because it looks like it worked.
            return None, "a split is mentioned but its ratio cannot be read"
        a, b = float(m.group(1)), float(m.group(2))
        if not (b > 0 and a > b):
            return None, "a split is mentioned but its ratio cannot be read"
        factor *= a / b
        parts.append(f"face value {a:g} to {b:g}")

    if factor > 1.0:
        return factor, " + ".join(parts)
    return None, "not a price-affecting action"


def fetch_actions(sess, start: date, end: date):
    """{symbol: [(ex_date, factor)]} for the window, plus the ones refused."""
    url = ("https://www.nseindia.com/api/corporates-corporateActions"
           f"?index=equities&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}")
    data = []
    for attempt in range(4):
        try:
            r = sess.get(url, timeout=60)
            if r.status_code == 200:
                j = r.json()
                data = j.get("data", j) if isinstance(j, dict) else j
                break
        except Exception:
            pass
        _reset(sess); time.sleep(3 + 3 * attempt)
    out, refused = {}, []
    for row in data or []:
        sym = str(row.get("symbol") or "").strip()
        subj = row.get("subject") or ""
        ex = row.get("exDate") or row.get("ex_date") or ""
        d = pd.to_datetime(ex, dayfirst=True, errors="coerce")
        if not sym or pd.isna(d):
            continue
        fac, why = parse_action(subj)
        if fac is None:
            if "bonus" in str(subj).lower() or "split" in str(subj).lower():
                refused.append(f"{sym} {d.date()}: {why} — \"{subj}\"")
            continue
        out.setdefault(sym, []).append((d.normalize(), fac))
    return out, refused


# --------------------------------------------------------------------------
# bhavcopy
# --------------------------------------------------------------------------

def _reset(sess):
    sess.headers.update({"User-Agent": UA,
                         "Accept": "application/json, text/plain, */*",
                         "Accept-Language": "en-US,en;q=0.9",
                         "Referer": "https://www.nseindia.com/"})
    for u in ("https://www.nseindia.com/", "https://www.nseindia.com/all-reports"):
        try: sess.get(u, timeout=25)
        except Exception: pass
    return sess


def new_session():
    return _reset(requests.Session())


def urls_for(d: date):
    dd, mm, yy = f"{d.day:02d}", f"{d.month:02d}", str(d.year)
    mon = MON[d.month - 1]
    return [
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yy}{mm}{dd}_F_0000.csv.zip",
        f"https://archives.nseindia.com/content/historical/EQUITIES/{yy}/{mon}/cm{dd}{mon}{yy}bhav.csv.zip",
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{yy}/{mon}/cm{dd}{mon}{yy}bhav.csv.zip",
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dd}{mm}{yy}.csv",
    ]


def parse_bhav(raw: bytes, url: str, d: date):
    """One day's bhavcopy -> {symbol: (open, high, low, close, qty)}.

    NSE changed the file layout in 2024, so column names are matched by any of
    their known spellings rather than by position."""
    if url.endswith(".zip"):
        z = zipfile.ZipFile(io.BytesIO(raw))
        raw = z.read(z.namelist()[0])
    lines = [l for l in raw.decode("utf-8", "replace").splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    hdr = [h.strip().strip('"').upper() for h in lines[0].split(",")]
    def col(*names):
        for n in names:
            if n in hdr: return hdr.index(n)
        return None
    ix = dict(sym=col("SYMBOL", "TCKRSYMB"), ser=col("SERIES", "SCTYSRS"),
              o=col("OPEN", "OPEN_PRICE", "OPNPRIC"),
              h=col("HIGH", "HIGH_PRICE", "HGHPRIC"),
              l=col("LOW", "LOW_PRICE", "LWPRIC"),
              c=col("CLOSE", "CLOSE_PRICE", "CLSPRIC"),
              q=col("TOTTRDQTY", "TTL_TRD_QNTY", "TTLTRADGVOL"))
    if ix["sym"] is None or ix["c"] is None or ix["h"] is None:
        return None
    need = max(v for v in ix.values() if v is not None)
    out = {}
    for line in lines[1:]:
        f = [v.strip().strip('"') for v in line.split(",")]
        if len(f) <= need: continue
        if ix["ser"] is not None and f[ix["ser"]] not in SERIES_OK: continue
        def num(k):
            try: return float(f[ix[k]]) if ix[k] is not None else np.nan
            except Exception: return np.nan
        c = num("c")
        if not np.isfinite(c) or c <= 0: continue
        out[f[ix["sym"]]] = (num("o"), num("h"), num("l"), c, num("q"))
    return out or None


def fetch_day(sess, d: date):
    for attempt in range(3):
        for u in urls_for(d):
            try:
                r = sess.get(u, timeout=45)
            except Exception:
                time.sleep(1); continue
            if r.status_code == 200:
                got = parse_bhav(r.content, u, d)
                if got: return got
            elif r.status_code in (401, 403, 429):
                _reset(sess); time.sleep(3 + 3 * attempt); break
        time.sleep(0.5)
    return None            # holiday, or the day is genuinely not published


# --------------------------------------------------------------------------

def run(dry_run=False):
    files = sorted(f for f in os.listdir(OHLC_DIR) if f.endswith(".csv"))
    hist = {}
    for fn in files:
        d = pd.read_csv(os.path.join(OHLC_DIR, fn), parse_dates=["Date"])
        for c in COLS:
            if c not in d.columns: d[c] = np.nan
        hist[fn[:-4]] = d[COLS].sort_values("Date").reset_index(drop=True)

    ends = {s: d.Date.max() for s, d in hist.items() if not d.empty}
    if not ends:
        print("no usable price files"); return 1
    start = (min(ends.values()) - pd.Timedelta(days=OVERLAP_DAYS)).date()
    today = date.today()
    print(f"stored data ends between {min(ends.values()).date()} and "
          f"{max(ends.values()).date()}")
    print(f"fetching bhavcopy {start} -> {today}\n")

    sess = new_session()
    days, d = [], start
    while d <= today:
        if d.weekday() < 5: days.append(d)
        d += timedelta(days=1)

    frames = {}
    got_days = 0
    for dd in days:
        rows = fetch_day(sess, dd)
        if rows is None:
            continue                      # weekend, holiday, or not yet published
        got_days += 1
        for sym, (o, h, l, c, q) in rows.items():
            frames.setdefault(sym, []).append(
                dict(Date=pd.Timestamp(dd), Close=c, High=h, Low=l, Open=o, Volume=q))
    print(f"{got_days} trading day(s) downloaded, {len(frames)} symbols present\n")
    if got_days == 0:
        print("nothing downloaded — refusing to touch any file"); return 1

    actions, refused = fetch_actions(sess, start, today)
    if actions:
        print(f"corporate actions in the window: "
              f"{sum(len(v) for v in actions.values())} across {len(actions)} symbols")
    for r in refused:
        print(f"  refused: {r}")
    print()

    report, quarantined, updated = {}, [], 0
    for sym, h in hist.items():
        rows = frames.get(sym)
        if not rows:
            report[sym] = dict(status="no_new_data", notes=["not in any bhavcopy fetched"])
            continue
        new = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)[COLS]
        new = new[new.Date >= h.Date.max() - pd.Timedelta(days=OVERLAP_DAYS)]
        if not (new.Date > h.Date.max()).any():
            report[sym] = dict(status="up_to_date", notes=[])
            continue
        acts = [(d, f) for d, f in actions.get(sym, []) if d > h.Date.max() - pd.Timedelta(days=OVERLAP_DAYS)]
        frame, status, notes = update_one(sym, h, new, acts, h.Date.max())
        report[sym] = dict(status=status, notes=notes)
        if status == "quarantined":
            quarantined.append(sym)
            print(f"  QUARANTINED {sym}: {'; '.join(notes)}")
        elif status == "ok":
            if not dry_run:
                atomic_write(frame, os.path.join(OHLC_DIR, sym + ".csv"))
            updated += 1

    counts = {}
    for v in report.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print("\n" + "=" * 60)
    for k, v in sorted(counts.items()):
        print(f"  {k:15} {v}")
    if not dry_run:
        json.dump(report, open(REPORT, "w"), indent=1, default=str)
        open(QUARANTINE, "w").write("\n".join(quarantined))

    limit = max(5, int(0.02 * max(len(files), 1)))
    if quarantined:
        print(f"\n{len(quarantined)} quarantined and left untouched: "
              + ", ".join(quarantined[:30]))
    if len(quarantined) > limit:
        print(f"\nFAILING THE RUN: {len(quarantined)} quarantined, above the "
              f"tolerance of {limit}.")
        return 1
    return 0


# --------------------------------------------------------------------------

def selftest():
    ok = True
    def check(subject, want_factor, why=""):
        nonlocal ok
        f, note = parse_action(subject)
        good = (want_factor is None and f is None) or \
               (want_factor is not None and f is not None and abs(f - want_factor) < 1e-6)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {subject[:64]:66} -> "
              f"{f if f is not None else 'not applied':>12}  ({note})")

    print("corporate-action subjects, the three that caused the v3 repairs first:")
    check("Bonus Preference Shares 21:1", None)
    check("Bonus Debentures 6:1", None)
    check("Bonus issue of NCRPS in the ratio 4:1", None)
    check("Issue Of Bonus Non Convertible Redeemable Preference Shares 1:1", None)
    print()
    print("genuine equity actions:")
    check("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 5.0)
    check("Face Value Split From Rs.10/- To Re.1/- Per Share", 10.0)
    check("Bonus 1:1", 2.0)
    check("Bonus Issue 2:1", 3.0)
    check("Bonus 3:5", 1.6)
    print()
    print("things that must never move a price:")
    check("Interim Dividend - Rs 5 Per Share", None)
    check("Annual General Meeting", None)
    check("Buy Back of Shares", None)
    check("Rights Issue 1:4", None)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else run(dry_run=a.dry_run))
