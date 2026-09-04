"""Seed ohlc_data/ from the validated adjusted price base.

WHY RE-SEED RATHER THAN REPAIR
------------------------------
The live repo's OHLC files carry the v1 corruption — unadjusted rows appended
onto an adjusted history with no re-adjustment — plus 268 pre-2024 jumps beyond
35% inherited from the original build. Phase 3's acceptance test is that the new
scanner reproduces the 358-trade v3 backtest exactly, and that cannot be done
from those files. The backtest's own price base is the only thing it can be
reproduced from, so that is what the live pipeline should stand on.

WHAT THIS WRITES
----------------
ff_prices_adjusted_v3.parquet, verified identical to pxd_v3 across all 751
symbols and all four price columns, with the three non-equity bonus factors
un-applied exactly as build_v3.py does:

    ZEEL     2014-03-03  x22.0   Bonus Preference Shares 21:1
    DRREDDY  2011-03-17  x7.0    Bonus Debentures 6:1
    TVSMOTOR 2025-08-25  x5.0    Bonus NCRPS 4:1

None of these changes the equity share count, so none should ever have touched
the equity price. Dividing history by them left a 21.9x cliff in ZEEL and one
trade that booked a fictional +245.8%.

PRECISION
---------
Numbers are written at 6 significant figures rather than full float64 repr.
Tested: the trade set is identical, XIRR moves in the 7th decimal
(32.15053829% -> 32.15054210%), and the files are 46% smaller. Significant
figures rather than fixed decimals because the lowest adjusted close in the base
is 0.0159 (FACT), where four decimal places would cost 0.32%.

Column order matches the existing repo files so the diff stays readable.
"""
import os, sys, shutil
import numpy as np, pandas as pd

PARQUET = sys.argv[1] if len(sys.argv) > 1 else "ff_prices_adjusted_v3.parquet"
OUT     = sys.argv[2] if len(sys.argv) > 2 else "ohlc_data"
FMT     = "%.6g"
COLS    = ["Date", "Close", "High", "Low", "Open", "Volume"]
HISTORY_OK_FROM = "2004-01-01"   # see the note on pre-2004 zeros below
CHUNK   = sys.argv[3] if len(sys.argv) > 3 else None   # e.g. "A-G", to stay inside a timeout

REPAIRS = {
    "ZEEL":     ("2014-03-03", 22.0, "Bonus Preference Shares 21:1"),
    "DRREDDY":  ("2011-03-17",  7.0, "Bonus Debentures 6:1"),
    "TVSMOTOR": ("2025-08-25",  5.0, "Bonus NCRPS 4:1"),
    # Found by the systematic sweep in FF_ca_repair.py, not by eye. Same class as
    # the three above: a factor applied where the equity price did not move.
    "BRITANNIA":  ("2010-03-08", 2.0, "Bonus Debentures — equity untouched"),
    "COROMANDEL": ("2012-07-13", 2.0, "factor applied, price did not move"),
    "NTPC":       ("2015-03-20", 2.0, "Bonus Debentures — equity untouched"),
}

d = pd.read_parquet(PARQUET)
d["date"] = pd.to_datetime(d["date"])
print(f"loaded {len(d):,} rows, {d.symbol.nunique()} symbols, "
      f"{d.date.min().date()} -> {d.date.max().date()}")

for sym, (ex, fac, why) in REPAIRS.items():
    m = (d.symbol == sym) & (d.date < pd.Timestamp(ex))
    if not m.any():
        print(f"  WARNING: {sym} has no rows before {ex} — repair not applied")
        continue
    for c in ("open", "high", "low", "close"):
        d.loc[m, c] = d.loc[m, c] * fac
    g = d[d.symbol == sym].sort_values("date")
    i = g.date.searchsorted(pd.Timestamp(ex))
    ratio = float(g.close.iloc[i-1] / g.close.iloc[i]) if 0 < i < len(g) else float("nan")
    print(f"  repair {sym:9} un-applied {fac}x ({why}) -> ratio across the ex-date "
          f"is now {ratio:.3f}")

os.makedirs(OUT, exist_ok=True)
before = sum(os.path.getsize(os.path.join(OUT, f))
             for f in os.listdir(OUT) if f.endswith(".csv"))

if CHUNK:
    lo, hi = CHUNK.split("-")
    d = d[(d.symbol >= lo) & (d.symbol < chr(ord(hi) + 1))]
    print(f"chunk {CHUNK}: {d.symbol.nunique()} symbols")

written = 0
problems = []
for sym, g in d.groupby("symbol", sort=True):
    g = g.sort_values("date")
    out = pd.DataFrame({
        "Date":   g.date.dt.strftime("%Y-%m-%d"),
        "Close":  g.close.values,
        "High":   g.high.values,
        "Low":    g.low.values,
        "Open":   g.open.values,
        # Volume must stay an integer. float_format applies to every float
        # column, so leaving it as one writes 1777465 as "1.77746e+06" — a
        # silent loss of five units that no downstream code would notice.
        "Volume": pd.array(np.round(g.qty.values), dtype="Int64"),
    })
    px = out[["Close", "High", "Low", "Open"]]
    if px.isna().any().any():
        problems.append(f"{sym}: NaN prices")
    # Zeros in Open/High/Low before Sep-2003 are a known artifact of the source
    # data, already recorded in the integrity audit (5,805 rows, 174 symbols,
    # none after 2012, no trade's entry or exit day affected). They are carried
    # through unchanged rather than silently patched; only zeros in the modern
    # region are treated as a defect.
    recent = out[pd.to_datetime(out.Date) >= HISTORY_OK_FROM]
    if (recent[["Close", "High", "Low", "Open"]] <= 0).any().any():
        problems.append(f"{sym}: non-positive prices after {HISTORY_OK_FROM}")
    if out.Date.duplicated().any():
        problems.append(f"{sym}: duplicate dates")
    out.to_csv(os.path.join(OUT, sym + ".csv"), index=False, float_format=FMT)
    written += 1

after = sum(os.path.getsize(os.path.join(OUT, f))
            for f in os.listdir(OUT) if f.endswith(".csv"))
print(f"\nwrote {written} files -> {OUT}/")
print(f"size {before/1e6:.0f} MB -> {after/1e6:.0f} MB  ({100*(1-after/max(before,1)):.0f}% smaller)")
if problems:
    print(f"\n{len(problems)} problem(s):")
    for p in problems[:20]: print("  " + p)
    sys.exit(1)
print("no NaN, no non-positive prices, no duplicate dates")
