"""Re-seed ohlc_data/ from the validated base, then carry the recent rows across.

The validated price base ends 24 August 2026; the repo's files run to 3
September, eight trading days further, produced by the old updater. Seeding
alone would throw those days away, so they are carried across — but through the
new updater's own logic rather than by trusting them.

Every symbol's 25-Aug-onward rows are offered to update_one() exactly as if they
had just been fetched. They are accepted only if the overlap agrees with the
clean base and the join has no cliff in it. A symbol that fails is left at 24
August and reported, for the next scheduled run to fill from Yahoo.

The two bases were checked to agree before this was attempted: on 24 August all
751 symbols match to machine precision, so appending across the seam is sound.

    python FF_reseed_gapfill.py <parquet> <ohlc_dir> [CHUNK]
"""
import os, sys
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from FF_OHLC_Updater_v2 import update_one, atomic_write, COLS, FLOAT_FMT

PARQUET = sys.argv[1]
OUT     = sys.argv[2]
CHUNK   = sys.argv[3] if len(sys.argv) > 3 else None
OVERLAP = 10

REPAIRS = {
    "ZEEL":     ("2014-03-03", 22.0, "Bonus Preference Shares 21:1"),
    "DRREDDY":  ("2011-03-17",  7.0, "Bonus Debentures 6:1"),
    "TVSMOTOR": ("2025-08-25",  5.0, "Bonus NCRPS 4:1"),
}

d = pd.read_parquet(PARQUET)
d["date"] = pd.to_datetime(d["date"])
for sym, (ex, fac, why) in REPAIRS.items():
    m = (d.symbol == sym) & (d.date < pd.Timestamp(ex))
    for c in ("open", "high", "low", "close"):
        d.loc[m, c] = d.loc[m, c] * fac

if CHUNK:
    lo, hi = CHUNK.split("-")
    d = d[(d.symbol >= lo) & (d.symbol < chr(ord(hi) + 1))]

seeded = filled = quarantined = 0
notes_out = []
for sym, g in d.groupby("symbol", sort=True):
    g = g.sort_values("date")
    hist = pd.DataFrame({
        "Date":   g.date.values,
        "Close":  g.close.values, "High": g.high.values,
        "Low":    g.low.values,   "Open": g.open.values,
        "Volume": g.qty.values,
    })[COLS]

    path = os.path.join(OUT, sym + ".csv")
    new = None
    if os.path.exists(path):
        cur = pd.read_csv(path, parse_dates=["Date"])
        if not cur.empty:
            new = cur[cur.Date >= hist.Date.max() - pd.Timedelta(days=OVERLAP)].copy()
            for c in COLS:
                if c not in new.columns: new[c] = np.nan
            new = new[COLS]

    frame, status = hist, "seed only"
    if new is not None and (new.Date > hist.Date.max()).any():
        f, st, notes = update_one(sym, hist, new, [], hist.Date.max())
        if st == "ok":
            frame, status = f, "gap filled"; filled += 1
        else:
            quarantined += 1
            notes_out.append(f"{sym}: {'; '.join(notes)}")
    atomic_write(frame, path)
    seeded += 1

print(f"symbols written        : {seeded}")
print(f"  recent rows carried  : {filled}")
print(f"  left at the base end : {seeded - filled - quarantined}")
print(f"  QUARANTINED          : {quarantined}")
for n in notes_out[:25]:
    print("   " + n)
