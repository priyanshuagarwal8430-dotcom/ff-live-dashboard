"""
FF_market_probe.py  --  one-shot probe, not part of the daily run.

Purpose: settle which Yahoo symbols actually carry the ten market-strip
instruments, before any of them reaches the dashboard. This is the same
exercise that was done for ^CRSLDX and for the NSE endpoints: ask the
GitHub runner, because the container and the desktop are both blocked
from Yahoo by the proxy.

It answers three questions per candidate:
  1. does the symbol return data at all
  2. how fresh is it (a stale index is worse than a missing one)
  3. do the 1D / 1W / 1M / 6M / 1Y returns come out sane

Nothing is written. It only prints. Delete this file once the symbols
are chosen.
"""

import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
    import pandas as pd
except ImportError as e:
    print("MISSING DEPENDENCY:", e)
    sys.exit(0)

# Candidates. Several per instrument on purpose - the point of a probe is to
# find out which one is real, not to assume. Nifty 500 is already proven live
# by FF_index_update.py and is here only as a control: if ^CRSLDX fails, the
# probe itself is broken, not the symbol.
CANDIDATES = {
    "Nifty 50":        ["^NSEI"],
    "Nifty 500":       ["^CRSLDX"],
    "Nifty Midcap":    ["^NSEMDCP50", "NIFTY_MIDCAP_100.NS", "^CNXMIDCAP",
                        "NIFTYMIDCAP150.NS"],
    "Nifty Smallcap":  ["^CNXSC", "NIFTY_SMLCAP_100.NS", "NIFTY_SMLCAP_250.NS",
                        "NIFTYSMLCAP250.NS"],
    "Nifty Microcap":  ["NIFTY_MICROCAP250.NS", "NIFTYMICROCAP250.NS",
                        "^NSEMICRO"],
    "Gold USD":        ["GC=F", "XAUUSD=X"],
    "Gold INR proxy":  ["GOLDBEES.NS"],
    "Crude USD":       ["CL=F", "BZ=F"],
    "Bitcoin":         ["BTC-INR", "BTC-USD"],
    "USD / INR":       ["USDINR=X", "INR=X"],
    "Dollar index":    ["DX-Y.NYB", "DX=F"],
    # India 10Y is the one with no obvious Yahoo home. Probe it anyway - a
    # clean failure here is a result, and tells us to go to a different
    # source rather than guess a ticker.
    "India 10Y yield": ["^IN10Y", "IN10YT=RR", "INDIA10Y.BOND", "^TNX"],
}

# Trading-day offsets, not calendar days: a calendar month back can land on a
# holiday and silently return the wrong bar.
WINDOWS = [("1D", 1), ("1W", 5), ("1M", 21), ("6M", 126), ("1Y", 252)]


def probe(sym):
    """Return a dict describing what this symbol actually gives us."""
    try:
        df = yf.download(sym, period="2y", interval="1d",
                         progress=False, auto_adjust=False, threads=False)
    except Exception as e:
        return {"ok": False, "why": f"exception: {type(e).__name__}: {e}"[:90]}

    if df is None or len(df) == 0:
        return {"ok": False, "why": "no rows returned"}

    # yfinance sometimes hands back a MultiIndex column frame for a single
    # symbol. Flatten before touching Close, or the lookup silently misses.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    s = df["Close"].dropna()
    if len(s) < 2:
        return {"ok": False, "why": f"only {len(s)} usable closes"}

    last_dt = s.index[-1].date()
    stale = (datetime.now().date() - last_dt).days

    rets = {}
    for label, back in WINDOWS:
        if len(s) > back:
            prev = float(s.iloc[-1 - back])
            rets[label] = None if prev == 0 else (float(s.iloc[-1]) / prev - 1) * 100
        else:
            rets[label] = None

    return {"ok": True, "rows": len(s), "last_date": str(last_dt),
            "stale_days": stale, "last": float(s.iloc[-1]), "rets": rets}


def fmt(v, dp=2):
    return "     —" if v is None else f"{v:+7.2f}"


print("=" * 96)
print("FF market-strip probe")
print("run at", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
print("=" * 96)

winners = {}
for name, syms in CANDIDATES.items():
    print(f"\n{name}")
    print("-" * 96)
    for sym in syms:
        r = probe(sym)
        if not r["ok"]:
            print(f"  {sym:24s} FAIL   {r['why']}")
            continue
        rt = r["rets"]
        flag = "  STALE" if r["stale_days"] > 6 else ""
        print(f"  {sym:24s} OK     rows={r['rows']:5d}  last={r['last_date']}"
              f"  ({r['stale_days']}d old){flag}")
        print(f"  {'':24s}        last={r['last']:14,.2f}   "
              f"1D {fmt(rt['1D'])}  1W {fmt(rt['1W'])}  1M {fmt(rt['1M'])}  "
              f"6M {fmt(rt['6M'])}  1Y {fmt(rt['1Y'])}")
        if name not in winners and r["stale_days"] <= 6:
            winners[name] = (sym, r)

# Gold and crude were decided in INR (MCX basis). Yahoo has no clean MCX feed,
# so the honest options are a rupee ETF proxy or USD x USDINR. Compute the
# second here so the two can be compared against each other rather than
# assumed equivalent - they are not: the ETF carries import duty and a
# premium, the derived price does not.
print("\n" + "=" * 96)
print("INR conversion check (Gold and Crude were decided in rupees)")
print("-" * 96)
fx = winners.get("USD / INR")
if not fx:
    print("  no working USD/INR symbol, cannot derive rupee prices")
else:
    rate = fx[1]["last"]
    print(f"  USD/INR = {rate:.4f} (from {fx[0]})")
    for nm, oz in [("Gold USD", 1.0), ("Crude USD", 1.0)]:
        w = winners.get(nm)
        if w:
            print(f"  {nm:12s} {w[1]['last']:12,.2f} USD  ->  "
                  f"{w[1]['last'] * rate:14,.2f} INR   (per contract unit, "
                  f"NOT per 10g / per barrel MCX lot)")
    g = winners.get("Gold INR proxy")
    if g:
        print(f"  GOLDBEES.NS  {g[1]['last']:12,.2f} INR per unit "
              f"(~1/100 gram of gold, carries duty + fund premium)")

print("\n" + "=" * 96)
print("SUMMARY - first working, fresh symbol per instrument")
print("-" * 96)
for name in CANDIDATES:
    w = winners.get(name)
    print(f"  {name:20s} {w[0] if w else 'NONE FOUND - needs a different source'}")
print("=" * 96)
