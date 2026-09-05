"""
FF — dashboard data builder (Phase B)
=====================================
Writes docs/data/*.json from the ledgers. Nothing about presentation lives here;
the page is an application that reads these files.

WHY THIS REPLACES A GENERATED PAGE
The old builder rewrote the whole HTML on every run, so the page changed in git
every day, collided on every push, and made interactivity awkward to add. Now the
data changes and the page does not.

TWO DERIVED FIELDS WORTH EXPLAINING

`below_ath_at_entry` — how far under its all-time-high close the stock was when
the signal fired. The rule fires at 40.00%, so a figure near 40 means the signal
was caught the moment it qualified. A deeper figure is not an error: it means the
price was already well below when the fundamentals turned.

`trigger_reason` — which of the two conditions was the last to become true. A
signal needs the price under the line AND the quarter public; the trigger lands on
whichever came second. Publishing it turns an unexplained deep entry into a
readable one: "the price was already 53% down; the signal came when the results
confirmed it." Computed, not guessed: the price-cross date comes from the price
series, the qualification date from the flags file, and the later one wins.
"""
import json, os
import pandas as pd, numpy as np

OUT   = "docs/data"
LED, WATCH = "signal_ledger.csv", "watchlist_ledger.csv"
IDX, FLAGS, OHLC = "indices/Nifty500.csv", "fund_flags_v3.csv", "ohlc_data"
THRESHOLD = 0.60
PERIODS = [("3M", 3), ("6M", 6), ("1Y", 12), ("1.5Y", 18),
           ("2Y", 24), ("2.5Y", 30), ("3Y", 36)]
SINCE = "2026-06-01"

_px = {}
def px(sym):
    if sym not in _px:
        p = os.path.join(OHLC, f"{sym}.csv")
        if not os.path.exists(p): _px[sym] = None
        else:
            d = pd.read_csv(p, usecols=["Date", "Close", "High"], parse_dates=["Date"])
            d = d.dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)
            d["ath"] = d.Close.cummax()
            _px[sym] = d
    return _px[sym]

def close_at(d, when):
    """Last close on or before `when` — the figure a reader would find on a chart."""
    m = d[d.Date <= pd.Timestamp(when)]
    return (round(float(m.Close.iloc[-1]), 2), str(m.Date.iloc[-1].date())) if len(m) else (None, None)

flags = pd.read_csv(FLAGS, parse_dates=["report_date", "avail_date"])
flags = flags[flags.all_hi_pos.astype(bool)]

def reason(sym, d, trig):
    """The later of the two conditions is the one that actually fired the signal."""
    trig = pd.Timestamp(trig)
    i = d.index[d.Date <= trig]
    if not len(i): return None, None, None
    i = i[-1]
    cond = d.Close <= THRESHOLD * d.ath
    j = i
    while j > 0 and bool(cond.iloc[j - 1]): j -= 1
    cross = d.Date.iloc[j]
    g = flags[(flags.symbol == sym) & (flags.avail_date <= trig)]
    qual = g.avail_date.max() if len(g) else pd.NaT
    if pd.isna(qual): return str(cross.date()), None, "Price crossed 40%"
    later = "Results published" if qual > cross else "Price crossed 40%"
    return str(cross.date()), str(qual.date()), later

def periodic(d, entry_date, entry_price, end, stop=None):
    """A cell is filled only once that period has actually elapsed — never zero,
    never a projection. For a closed trade the periods after the exit stay blank
    because there was no position to hold."""
    out = {}
    for label, months in PERIODS:
        when = pd.Timestamp(entry_date) + pd.DateOffset(months=months)
        if when > pd.Timestamp(end) or (stop is not None and when > pd.Timestamp(stop)):
            out[label] = None; continue
        c, _ = close_at(d, when)
        out[label] = None if c is None else round(100 * (c / entry_price - 1), 2)
    return out

def rows(path, end, kind):
    led = pd.read_csv(path, parse_dates=["trigger_date", "entry_date", "exit_date"])
    out = []
    for r in led.itertuples():
        d = px(r.symbol)
        if d is None: continue
        last, last_dt = close_at(d, end)
        upto = d[d.Date <= r.trigger_date]
        k = upto.Close.idxmax()
        ath_date = str(d.Date.loc[k].date())
        closed = str(r.status) == "CLOSED"
        exit_px = float(r.exit_price) if closed and pd.notna(r.exit_price) else None
        cur = exit_px if closed else last
        # Both tables measure from the entry price - the next trading day's open.
        # Measuring the watchlist from the trigger close instead was tried and
        # rejected: it would have put the two lists on different bases, so a
        # watchlist return could not be read against a position return.
        ep, tgt = float(r.entry_price), float(r.target_ath)
        cross, qual, why = reason(r.symbol, d, r.trigger_date)
        rec = dict(
            symbol=r.symbol, market_type=r.market_type,
            trigger_date=str(r.trigger_date.date()), entry_date=str(r.entry_date.date()),
            entry_price=round(ep, 2), target=round(tgt, 2), ath_date=ath_date,
            last=cur, last_date=str(r.exit_date.date()) if closed else last_dt,
            ret_pct=round(100 * (cur / ep - 1), 2) if cur else None,
            progress_pct=round(100 * (cur - ep) / (tgt - ep), 1) if cur and tgt > ep else None,
            below_ath_at_entry=round(abs(float(r.drawdown_pct)), 2),
            price_crossed_on=cross, qualified_on=qual, trigger_reason=why,
            status=r.status,
        )
        if kind == "position":
            stop = r.exit_date if closed else None
            rec["periods"] = periodic(d, r.entry_date, ep, end, stop)
            if closed:
                rec.update(exit_date=str(r.exit_date.date()), exit_price=round(exit_px, 2),
                           exit_reason=r.exit_reason,
                           days_held=int((r.exit_date - r.entry_date).days))
        out.append(rec)
    return out

def main():
    os.makedirs(OUT, exist_ok=True)
    end = max(px(s).Date.max() for s in
              pd.read_csv(LED).symbol.tolist()[:5] + ["RELIANCE"] if px(s) is not None)
    all_pos = rows(LED, end, "position")
    # Micro Cap is NOT published. Its stored flags were computed on a FOUR-quarter
    # window, not the strategy's eight, so seven of its thirteen entries do not
    # qualify under the rule the rest of this dashboard runs on - OPTIEMUS among
    # them, whose Net Sales is a 4-quarter high and an 8-quarter low. Showing a
    # looser screen beside the real signals would read as the same rule on a
    # different universe, which it is not. FF_Ledger.py keeps writing
    # watchlist_ledger.csv so the record accumulates; it gets its own dashboard
    # once its flags are rebuilt on an eight-quarter window.
    op = [r for r in all_pos if r["status"] != "CLOSED"]
    cl = [r for r in all_pos if r["status"] == "CLOSED"]
    op.sort(key=lambda r: -(r["ret_pct"] or 0))
    cl.sort(key=lambda r: r["exit_date"], reverse=True)


    ix = pd.read_csv(IDX, parse_dates=["Date"]).dropna(subset=["Close"]).sort_values("Date")
    yr = ix[ix.Date >= ix.Date.max() - pd.Timedelta(days=370)]
    step = max(1, len(yr) // 130)
    index = dict(name="Nifty 500",
                 last=round(float(yr.Close.iloc[-1]), 2),
                 prev=round(float(yr.Close.iloc[-2]), 2),
                 lo=int(yr.Close.min()), hi=int(yr.Close.max()),
                 as_of=str(yr.Date.iloc[-1].date()),
                 points=[round(float(v)) for v in yr.Close.tolist()[::step]])

    meta = dict(as_of=str(pd.Timestamp(end).date()), tracking_since=SINCE,
                open=len(op), closed=len(cl),
                in_profit=sum(1 for r in op if (r["ret_pct"] or 0) > 0),
                avg_return=round(float(np.mean([r["ret_pct"] for r in op if r["ret_pct"] is not None])), 2) if op else None,
                index_as_of=index["as_of"], periods=[p[0] for p in PERIODS])

    for name, obj in (("positions", op), ("closed", cl),
                      ("index", index), ("meta", meta)):
        with open(f"{OUT}/{name}.json", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    if not os.path.exists(f"{OUT}/news.json"):
        with open(f"{OUT}/news.json", "w", encoding="utf-8") as f:
            json.dump({"portfolio": [], "macro": [], "as_of": None}, f)

    print(f"as of {meta['as_of']} | index to {meta['index_as_of']}")
    print(f"  open {meta['open']}  closed {meta['closed']}"
          f"  in profit {meta['in_profit']}  avg {meta['avg_return']:+.2f}%")
    print("  Micro Cap not published - its flags use a 4-quarter window, not 8")
    n = sum(1 for r in op if r["trigger_reason"] == "Results published")
    print(f"  trigger reason: {n} results-bound, {len(op)-n} price-bound")
    print(f"  3M column filled for {sum(1 for r in op if r['periods']['3M'] is not None)} of {len(op)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
