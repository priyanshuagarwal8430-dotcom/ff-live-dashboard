import pandas as pd, time, sys
import FF_Signal_Engine_v2 as E
t0=time.time()
end = E.data_end(); print(f"end {end.date()}  [{time.time()-t0:.0f}s]", flush=True)
px = E.load_prices(end=end); print(f"prices loaded: {len(px)}  [{time.time()-t0:.0f}s]", flush=True)
ident = pd.read_csv("ident.csv")
uni = sorted(set(ident[ident.market_type.isin(E.N500)].symbol) & set(px))
fl = E.load_flags(path="fund_flags_v3_datefix.csv", tiers=E.N500)
print(f"flags loaded  [{time.time()-t0:.0f}s]", flush=True)
sigs=[]
for s in uni: sigs += E.triggers(s, px, fl)
sigs.sort(key=lambda z:(z[1],z[0]))
print(f"triggers done: {len(sigs)} over all history  [{time.time()-t0:.0f}s]", flush=True)
t = E.simulate(sigs, px, end)
t = t[t.trigger_date >= pd.Timestamp("2026-06-01")].copy()
t["dd"] = (100*(t.trigger_close/t.target_ath - 1)).abs().round(2)
t.to_csv("datefix_signals.csv", index=False)
print(f"WROTE datefix_signals.csv : {len(t)} signals since 2026-06-01  [{time.time()-t0:.0f}s]", flush=True)
