"""
FF — Nifty 500 index series, kept current
=========================================
WHAT WAS WRONG
indices/Nifty500.csv was seeded once from the backtest's benchmark series, which
ends on the reference date 2026-07-27, and nothing has appended to it since. The
daily updater refreshes the 751 symbols in ident.csv; the index is not one of
them, so it simply stopped. A dashboard showing that number as today's market
reference would have been two months stale and said so nowhere.

The index is a REFERENCE, not a comparison. Nothing on the dashboard measures the
strategy against it. That is precisely why it has to be current: a reference that
is wrong is worse than no reference.

THE ONE CHECK THAT MATTERS
An index has no corporate actions, so none of the OHLC updater's split machinery
applies here. What can go wrong instead is a SCALE mismatch: ^CRSLDX is a price
index, and pulling a total-return variant by mistake would append a series that
drifts away from the stored one without ever looking broken on any single day.
So the fetched block must agree with the stored tail where they overlap. If it
does not, nothing is written and the run says why.

Failure is never fatal. A stale index is a flaw on one strip; a failed run stops
the prices, the signals and the ledger. So this exits 0 whatever happens and
reports what it did.

    python FF_index_update.py --selftest
    python FF_index_update.py
"""
from __future__ import annotations
import sys, io, os
import pandas as pd, numpy as np

PATH      = "indices/Nifty500.csv"
SYMBOL    = "^CRSLDX"                 # Nifty 500, price index
COLS      = ["Date", "Close", "High", "Low", "Open", "Volume"]
OVERLAP_TOL = 0.005                   # 0.5% — an index does not gap on definition
MIN_OVERLAP = 5                       # fewer shared days than this proves nothing


def merge(hist: pd.DataFrame, new: pd.DataFrame):
    """Append only dates the stored series does not have, after proving the two
    are the same series. Returns (frame, note); frame is None when refusing."""
    if new is None or new.empty:
        return None, "feed returned nothing"
    hist = hist.copy(); new = new.copy()
    hist["Date"] = pd.to_datetime(hist.Date); new["Date"] = pd.to_datetime(new.Date)
    j = hist.merge(new, on="Date", suffixes=("_h", "_n"))
    if len(j) < MIN_OVERLAP:
        return None, f"only {len(j)} overlapping days — not enough to confirm the scale"
    r = (j.Close_n / j.Close_h).replace([np.inf, -np.inf], np.nan).dropna()
    med, spread = float(r.median()), float(r.max() - r.min())
    if abs(med - 1.0) > OVERLAP_TOL or spread > OVERLAP_TOL:
        return None, (f"overlap disagrees: median ratio {med:.4f}, spread {spread:.4f} "
                      f"over {len(r)} days — this is a different series, not new data")
    add = new[~new.Date.isin(set(hist.Date))]
    if add.empty:
        return hist, "already current"
    out = (pd.concat([hist, add], ignore_index=True)
             .drop_duplicates("Date", keep="last")
             .sort_values("Date").reset_index(drop=True))
    return out, f"appended {len(add)} rows to {add.Date.max().date()}"


def fetch(since):
    import yfinance as yf
    d = yf.download(SYMBOL, start=str(pd.Timestamp(since).date()),
                    progress=False, auto_adjust=False, threads=False)
    if d is None or d.empty: return None
    if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
    d = d.reset_index()[["Date", "Close", "High", "Low", "Open", "Volume"]]
    d["Date"] = pd.to_datetime(d.Date).dt.tz_localize(None).dt.normalize()
    return d.dropna(subset=["Close"])


def selftest():
    base = pd.DataFrame({"Date": pd.bdate_range("2026-06-01", periods=40),
                         "Close": np.linspace(22000, 23000, 40)})
    for c in ("High", "Low", "Open"): base[c] = base.Close
    base["Volume"] = 0
    ok = True
    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"); ok &= bool(cond)

    new = base.iloc[-10:].copy()
    ext = pd.DataFrame({"Date": pd.bdate_range(base.Date.max() + pd.Timedelta(days=1), periods=6)})
    for c in ("Close", "High", "Low", "Open"): ext[c] = 23100.0
    ext["Volume"] = 0
    f, why = merge(base, pd.concat([new, ext], ignore_index=True))
    check("appends only the new dates", f is not None and len(f) == 46 and "appended 6" in why)

    f, why = merge(base, base.iloc[-10:].copy())
    check("nothing new is not an error", f is not None and why == "already current")

    scaled = base.iloc[-10:].copy(); scaled["Close"] *= 1.4
    f, why = merge(base, scaled)
    check("a rescaled series is refused", f is None and "different series" in why)

    f, why = merge(base, base.iloc[-2:].copy())
    check("too little overlap is refused", f is None and "not enough" in why)

    f, why = merge(base, None)
    check("an empty feed is refused, not crashed", f is None)

    drift = base.iloc[-10:].copy()
    drift.loc[drift.index[-1], "Close"] *= 1.02          # one day 2% off
    f, why = merge(base, drift)
    check("a single bad day is caught by the spread", f is None)
    return 0 if ok else 1


def main():
    if not os.path.exists(PATH):
        print(f"{PATH} not found — nothing to update"); return 0
    hist = pd.read_csv(PATH)
    last = pd.to_datetime(hist.Date).max()
    print(f"{PATH}: {len(hist)} rows to {last.date()}")
    try:
        new = fetch(last - pd.Timedelta(days=30))
    except Exception as e:
        print(f"  fetch failed: {type(e).__name__}: {str(e)[:120]}")
        print("  index left as it was; this never fails the run"); return 0
    out, why = merge(hist, new)
    print(f"  {why}")
    if out is None:
        print("  index left as it was; this never fails the run"); return 0
    if why != "already current":
        out[COLS].to_csv(PATH, index=False, float_format="%.6g")
        print(f"  -> {PATH}, now to {pd.to_datetime(out.Date).max().date()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
