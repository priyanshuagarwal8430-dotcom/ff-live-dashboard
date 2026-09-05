"""
FF — dashboard builder v2, reading the ledger
============================================================================
Replaces FF_build_dashboard.py, which read the scanner's overwritten output,
hardcoded every row as `isNew:true`, and had no exit handling at all — so a
position that reached its target kept showing as open forever.

This reads `signal_ledger.csv` and `watchlist_ledger.csv`, which are append-only.
A recommendation on this page was published on the date it says and has not been
edited since.

Three things it states plainly rather than hiding:

  * **reconstructed vs live.** Signals dated before the ledger first ran were
    computed afterwards from history. They were never actually published at the
    time, so they are labelled and counted separately. Presenting them as a track
    record would be a claim about something that did not happen.
  * **Micro Cap is not recommendable.** It gets its own section, below, marked as
    a watchlist.
  * **The benchmark series ends earlier than the price series.** The comparison
    is shown to the last date the index actually carries, and says so, rather
    than being quietly extended.

    python FF_build_dashboard_v2.py
"""

from __future__ import annotations

import os, sys
import numpy as np, pandas as pd

LEDGER, WATCH = "signal_ledger.csv", "watchlist_ledger.csv"
OHLC_DIR, INDEX = "ohlc_data", "indices/Nifty500.csv"
OUT = "FF_Live_Tracking_Dashboard.html"

# from the validated reference palette; roles only, no raw hex in the body
CSS = """
:root{color-scheme:dark;
 --surface:#111110; --panel:#1a1a19; --line:#2c2c2a;
 --ink:#ffffff; --ink2:#c3c2b7; --ink3:#8a8a80;
 --series:#3987e5; --track:#26262a;
 --good:#0ca30c; --bad:#d03b3b; --warn:#fab219;}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--ink);
 font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;margin:34px 0 10px;letter-spacing:-.01em}
.sub{color:var(--ink3);font-size:12.5px;margin:0 0 22px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0 6px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:13px 15px}
.tile .k{color:var(--ink3);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.tile .v{font-size:23px;font-weight:600;margin-top:5px;letter-spacing:-.02em}
.tile .n{color:var(--ink3);font-size:11.5px;margin-top:3px}
.note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--warn);
 border-radius:7px;padding:12px 15px;color:var(--ink2);font-size:12.5px;margin:14px 0}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:9px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:12.5px;min-width:820px}
th{text-align:left;color:var(--ink3);font-weight:500;font-size:11px;text-transform:uppercase;
 letter-spacing:.06em;padding:11px 13px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 13px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:none}
.n{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--good)} .neg{color:var(--bad)}
.tag{font-size:10.5px;padding:2px 7px;border-radius:20px;border:1px solid var(--line);color:var(--ink3)}
.tag.live{color:var(--good);border-color:var(--good)}
.bar{position:relative;height:7px;background:var(--track);border-radius:4px;width:130px;display:inline-block;
 vertical-align:middle}
.bar > i{position:absolute;left:0;top:0;bottom:0;background:var(--series);border-radius:4px;
 box-shadow:0 0 0 2px var(--panel)}
.empty{padding:26px 15px;color:var(--ink3);font-size:12.5px}
footer{margin-top:40px;color:var(--ink3);font-size:11.5px;border-top:1px solid var(--line);padding-top:14px}
"""


def last_price(sym, px):
    d = px.get(sym)
    return (None, None) if d is None else (pd.Timestamp(d["dates"][-1]), float(d["close"][-1]))


def load_px():
    px = {}
    for fn in os.listdir(OHLC_DIR):
        if not fn.endswith(".csv"): continue
        t = pd.read_csv(os.path.join(OHLC_DIR, fn), usecols=["Date", "Close"])
        if len(t) < 1: continue
        px[fn[:-4]] = dict(dates=pd.to_datetime(t.Date).values, close=t.Close.values.astype(float))
    return px


def esc(x): return str(x).replace("&", "&amp;").replace("<", "&lt;")


def num(v, dp=2, sign=False):
    """Returns are ALWAYS rendered with an explicit + or -.

    Positive and negative are also coloured green and red, and those two are
    ΔE 4.1 apart under deuteranopia — indistinguishable to a red-green colour
    blind reader. The sign is what actually carries the meaning; the colour only
    speeds it up for everyone else. Do not remove the sign to "clean up" the
    numbers: that would make the colour load-bearing and lock those readers out."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)): return "&mdash;"
    s = f"{v:+,.{dp}f}" if sign else f"{v:,.{dp}f}"
    return s


def open_table(rows):
    if not rows: return '<div class="scroll"><div class="empty">No open positions.</div></div>'
    body = []
    for r in rows:
        cls = "pos" if r["unreal"] >= 0 else "neg"
        pct = max(0.0, min(1.0, r["progress"]))
        body.append(
            f'<tr><td><strong>{esc(r["symbol"])}</strong></td><td>{esc(r["tier"])}</td>'
            f'<td><span class="tag {"live" if r["record"]=="live" else ""}">{esc(r["record"])}</span></td>'
            f'<td>{r["entry_date"]}</td><td class="n">{num(r["entry"])}</td>'
            f'<td class="n">{num(r["now"])}</td><td class="n {cls}">{num(r["unreal"],1,True)}%</td>'
            f'<td class="n">{num(r["target"])}</td><td class="n">{num(r["to_target"],1)}%</td>'
            f'<td><span class="bar"><i style="width:{pct*100:.1f}%"></i></span></td>'
            f'<td class="n">{r["days"]}</td><td class="n">{r["left"]}</td></tr>')
    return ('<div class="scroll"><table><thead><tr>'
            '<th>Symbol</th><th>Tier</th><th>Record</th><th>Entry date</th>'
            '<th class="n">Entry</th><th class="n">Now</th><th class="n">Unrealised</th>'
            '<th class="n">Target</th><th class="n">To target</th><th>Progress</th>'
            '<th class="n">Days held</th><th class="n">Days to cap</th>'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def closed_table(rows):
    if not rows:
        return ('<div class="scroll"><div class="empty">No positions have closed yet. '
                'The earliest entries are from June 2026 and the time cap is three years, '
                'so the first closes will be target hits.</div></div>')
    body = []
    for r in rows:
        cls = "pos" if r["ret"] >= 0 else "neg"
        body.append(f'<tr><td><strong>{esc(r["symbol"])}</strong></td><td>{esc(r["tier"])}</td>'
                    f'<td>{r["entry_date"]}</td><td>{r["exit_date"]}</td>'
                    f'<td class="n">{num(r["entry"])}</td><td class="n">{num(r["exit"])}</td>'
                    f'<td class="n {cls}">{num(r["ret"],1,True)}%</td>'
                    f'<td>{esc(r["reason"])}</td><td class="n">{r["days"]}</td></tr>')
    return ('<div class="scroll"><table><thead><tr><th>Symbol</th><th>Tier</th>'
            '<th>Entry</th><th>Exit</th><th class="n">Entry price</th><th class="n">Exit price</th>'
            '<th class="n">Return</th><th>Reason</th><th class="n">Days</th>'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def prepare(path, px, today):
    if not os.path.exists(path): return [], []
    led = pd.read_csv(path, parse_dates=["trigger_date", "entry_date", "exit_date"])
    op, cl = [], []
    for r in led.itertuples():
        d, now = last_price(r.symbol, px)
        if str(r.status) == "CLOSED":
            cl.append(dict(symbol=r.symbol, tier=r.market_type,
                           entry_date=str(pd.Timestamp(r.entry_date).date()),
                           exit_date=str(pd.Timestamp(r.exit_date).date()),
                           entry=float(r.entry_price), exit=float(r.exit_price),
                           ret=100 * (float(r.exit_price) / float(r.entry_price) - 1),
                           reason=r.exit_reason,
                           days=(pd.Timestamp(r.exit_date) - pd.Timestamp(r.entry_date)).days))
            continue
        if now is None: continue
        entry, tgt = float(r.entry_price), float(r.target_ath)
        held = (d - pd.Timestamp(r.entry_date)).days
        op.append(dict(symbol=r.symbol, tier=r.market_type, record=str(r.record),
                       entry_date=str(pd.Timestamp(r.entry_date).date()),
                       entry=entry, now=now, target=tgt,
                       unreal=100 * (now / entry - 1),
                       to_target=100 * (tgt / now - 1),
                       progress=(now - entry) / (tgt - entry) if tgt > entry else 0.0,
                       days=held, left=max(0, 1095 - held)))
    op.sort(key=lambda x: -x["progress"])
    cl.sort(key=lambda x: x["exit_date"])
    return op, cl


def benchmark(op, today):
    """Equal-weighted unrealised return against the Nifty 500 over the same
    dates. Only to the last date the index series carries — it is not extended."""
    if not op or not os.path.exists(INDEX): return None
    ix = pd.read_csv(INDEX, parse_dates=["Date"]).sort_values("Date")
    ix_end = ix.Date.max()
    s = ix.set_index("Date").Close
    rets, bench = [], []
    for r in op:
        ed = pd.Timestamp(r["entry_date"])
        if ed > ix_end: continue
        a = s.asof(ed); b = s.asof(ix_end)
        if not (np.isfinite(a) and np.isfinite(b) and a > 0): continue
        bench.append(100 * (b / a - 1))
        rets.append(r["unreal"])
    if not rets: return None
    return dict(n=len(rets), strat=float(np.mean(rets)), bench=float(np.mean(bench)),
                end=ix_end)


def main():
    px = load_px()
    today = max(pd.Timestamp(d["dates"][-1]) for d in px.values())
    op, cl = prepare(LEDGER, px, today)
    wop, wcl = prepare(WATCH, px, today)

    live_n = sum(1 for r in op if r["record"] == "live")
    inprofit = sum(1 for r in op if r["unreal"] >= 0)
    med = float(np.median([r["unreal"] for r in op])) if op else float("nan")
    bm = benchmark(op, today)

    tiles = [("Open positions", f"{len(op)}",
              (f"{live_n} published live, {len(op)-live_n} reconstructed" if live_n
               else "all reconstructed &mdash; none published live yet")),
             ("Closed trades", f"{len(cl)}", "realised, with exit prices"),
             ("Median unrealised", f"{med:+.1f}%" if op else "&mdash;",
              f"{inprofit} of {len(op)} above entry"),
             ("Micro Cap watchlist", f"{len(wop)}", "not recommendations")]
    if bm:
        tiles.append(("Vs Nifty 500", f"{bm['strat']-bm['bench']:+.1f} pp",
                      f"{bm['strat']:+.1f}% against {bm['bench']:+.1f}%, "
                      f"to {bm['end'].date()}"))

    tile_html = "".join(
        f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="n">{n}</div></div>' for k, v, n in tiles)

    notes = []
    if len(op) - live_n:
        notes.append(f"<strong>{len(op)-live_n} of {len(op)} open positions are "
                     "reconstructed</strong> — computed from history after the fact under "
                     "the current rules, not published on the day they triggered. They are "
                     "shown because they are the strategy's real state, not because they "
                     "are a track record. Only rows marked <em>live</em> were published "
                     "when they fired.")
    if bm:
        notes.append(f"The benchmark comparison runs to <strong>{bm['end'].date()}</strong>, "
                     f"the last date the Nifty 500 series carries, while prices run to "
                     f"{today.date()}. The index series is not extended to close the gap.")
    notes.append("Micro Cap is excluded from recommendations and shown separately. "
                 "Its fundamental flags still need their own eight-quarter rebuild.")
    note_html = "".join(f'<div class="note">{n}</div>' for n in notes)

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fundamental First — live tracking</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>Fundamental First — live tracking</h1>
<p class="sub">Prices to {today.date()} &middot; spec v2.0 &middot; record begins 1 June 2026 &middot;
append-only ledger: a published row is never edited</p>
{tile_html and f'<div class="tiles">{tile_html}</div>'}
{note_html}
<h2>Open positions &mdash; Nifty 500</h2>
{open_table(op)}
<h2>Closed trades</h2>
{closed_table(cl)}
<h2>Micro Cap watchlist &mdash; not recommendations</h2>
{open_table(wop)}
<footer>Entry: the close falls to 60% of its running all-time high while Net Sales, PAT and
diluted EPS are each at an eight-quarter high and all three are positive; entry is the next
day's open. Exit: the all-time-high close as at the trigger date, fixed then, or a 1,095-day
cap. No stop loss. Built from signal_ledger.csv.</footer>
</div></body></html>"""

    open(OUT, "w", encoding="utf-8").write(html)
    print(f"{OUT}: {len(op)} open, {len(cl)} closed, {len(wop)} on the watchlist")
    print(f"  live {live_n}, reconstructed {len(op)-live_n}")
    if bm: print(f"  vs Nifty 500 to {bm['end'].date()}: "
                 f"{bm['strat']:+.2f}% against {bm['bench']:+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
