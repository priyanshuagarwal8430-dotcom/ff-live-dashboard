"""
Phase A — what the real announcement dates change, shown BEFORE anything is written.

Reads results_dates.csv, builds a corrected copy of the flags file (a NEW file;
fund_flags_v3.csv is not touched), and runs the scan twice off one price load:
once on the 60-day assumption, once on the real dates. Then prints the difference.
Writes no ledger. Writes no dashboard.
"""
import pandas as pd, numpy as np, sys
import FF_Signal_Engine_v2 as E

QS = {"2026-03-31", "2026-06-30"}
MANUAL = {  # found by hand; the calendar records no results-purpose meeting for these
    ("NAUKRI","2026-03-31"):    ("2026-05-22","manual_verified"),  # matches an NSE board meeting the same day
    ("ABBOTINDIA","2026-03-31"):("2026-05-11","manual_web"),
    ("ALKYLAMINE","2026-03-31"):("2026-05-05","manual_web"),
    ("BAYERCROP","2026-03-31"): ("2026-05-26","manual_web"),
    ("MCX","2026-03-31"):       ("2026-05-08","manual_web"),
    ("MTARTECH","2026-03-31"):  ("2026-05-12","manual_web"),
    ("SAIL","2026-03-31"):      ("2026-05-15","manual_web"),
}

r = pd.read_csv("results_dates.csv")
r = r[~r.set_index(["symbol","quarter"]).index.isin(MANUAL.keys())]
r = pd.concat([r, pd.DataFrame([dict(symbol=k[0], quarter=k[1], announced_on=v[0], source=v[1])
                                for k, v in MANUAL.items()])], ignore_index=True)
r = r.sort_values(["quarter","symbol"]); r.to_csv("results_dates.csv", index=False)
print(f"results_dates.csv: {len(r)} rows  {r.source.value_counts().to_dict()}")

f = pd.read_csv("fund_flags_v3.csv", parse_dates=["report_date","avail_date"])
lut = {(x.symbol, x.quarter): x.announced_on for x in r.itertuples()}
key = list(zip(f.symbol, f.report_date.dt.strftime("%Y-%m-%d")))
new = [lut.get(k) for k in key]
mask = pd.Series([k[1] in QS and n is not None for k, n in zip(key, new)])
old_av = f.avail_date.copy()
f.loc[mask, "avail_date"] = pd.to_datetime(pd.Series(new)[mask])
moved = (f.avail_date != old_av)
print(f"avail_date corrected on {int(moved.sum())} rows "
      f"(of {int(pd.Series([k[1] in QS for k in key]).sum())} rows in those two quarters)")
shift = (old_av[moved] - f.avail_date[moved]).dt.days
print(f"  moved EARLIER by median {int(shift.median())} days, max {int(shift.max())}; "
      f"later on {int((shift<0).sum())} rows")
f.to_csv("fund_flags_v3_datefix.csv", index=False)

end = E.data_end(); print(f"\nprice data ends {end.date()}; loading prices once…", flush=True)
px = E.load_prices(end=end)
ident = pd.read_csv("ident.csv")
uni = sorted(set(ident[ident.market_type.isin(E.N500)].symbol) & set(px))

def run(path, label):
    fl = E.load_flags(path=path, tiers=E.N500)
    sigs = []
    for s in uni: sigs += E.triggers(s, px, fl)
    sigs.sort(key=lambda z: (z[1], z[0]))
    t = E.simulate(sigs, px, end)
    t = t[t.trigger_date >= pd.Timestamp("2026-06-01")].copy()
    t["dd"] = (100*(t.trigger_close/t.target_ath - 1)).abs()
    print(f"{label}: {len(t)} signals since 2026-06-01", flush=True)
    return t.set_index("symbol")

a = run("fund_flags_v3.csv",         "BEFORE (60-day assumption)")
b = run("fund_flags_v3_datefix.csv", "AFTER  (real announcement dates)")

print("\n" + "="*74)
print("GONE — triggered before 1 June once the real date is used, so out of the record")
print("="*74)
for s in sorted(set(a.index) - set(b.index)):
    print(f"  {s:12} was {a.loc[s,'trigger_date'].date()}  entry {a.loc[s,'entry_price']:>9.2f}  dd {a.loc[s,'dd']:.2f}%")
print("\n" + "="*74); print("NEW"); print("="*74)
for s in sorted(set(b.index) - set(a.index)):
    print(f"  {s:12} now {b.loc[s,'trigger_date'].date()}  entry {b.loc[s,'entry_price']:>9.2f}  dd {b.loc[s,'dd']:.2f}%")
print("\n" + "="*74); print("KEPT — and whether the date/price moved"); print("="*74)
for s in sorted(set(a.index) & set(b.index)):
    x, y = a.loc[s], b.loc[s]
    ch = "unchanged" if x.trigger_date == y.trigger_date else f"MOVED {x.trigger_date.date()} -> {y.trigger_date.date()}"
    print(f"  {s:12} {y.trigger_date.date()}  entry {y.entry_price:>9.2f}  dd {y.dd:5.2f}%   {ch}")
print("\n" + "="*74)
print(f"count   : {len(a)} -> {len(b)}")
print(f"median drawdown at trigger : {a.dd.median():.2f}%  ->  {b.dd.median():.2f}%   (rule fires at 40.00%)")
print(f"within 2pp of the rule     : {int((a.dd<=42).sum())}/{len(a)}  ->  {int((b.dd<=42).sum())}/{len(b)}")
