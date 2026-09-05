"""Rebuild both ledgers from the corrected flags into NEW files.
Prices are loaded once and cached: FF_Ledger's main() plus two build() calls
would otherwise read 751 files three times and risk the call timing out."""
import FF_Signal_Engine_v2 as E
_cache = {}
_orig = E.load_prices
def cached(d=E.OHLC_DIR, end=None):
    k = (d, str(end))
    if k not in _cache: _cache[k] = _orig(d, end)
    return _cache[k]
E.load_prices = cached           # patched BEFORE FF_Ledger imports the name
import FF_Ledger as L
L.load_prices = cached
L.LEDGER = "signal_ledger_new.csv"
L.WATCH  = "watchlist_ledger_new.csv"
raise SystemExit(L.main(False))
