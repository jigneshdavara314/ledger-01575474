"""
Generate a self-contained HTML dashboard for the Polymarket paper bot.

    python dashboard.py            # writes dashboard.html and prints the path

Open dashboard.html in any browser. It shows:
  - headline stats (P&L, win rate, ROI, open positions, daily target progress)
  - all open positions (live, awaiting resolution)
  - resolved trade history (won/lost with P&L)
  - per-category performance breakdown

The page auto-refreshes every 60s. Re-run this script (or let the scheduled
job run it) to refresh the underlying data.
"""
import html
import datetime

from polybot import config, store


def _fmt_money(v, plus=True):
    if v is None:
        return "-"
    sign = "+" if (plus and v >= 0) else ""
    return f"{sign}${v:,.2f}"


def _pnl_class(v):
    if v is None:
        return ""
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def build_html() -> str:
    store.init_db()
    s = store.performance_summary()
    cats = store.category_summary()
    rows = store.recent_trades(200)
    today = store.today_pnl()
    target = config.DAILY_TARGET_USD

    open_rows = [r for r in rows if r[6] == "OPEN"]
    done_rows = [r for r in rows if r[6] in ("WON", "LOST")]

    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_pct = max(0, min(100, (today / target * 100) if target else 0))

    # ---- headline stat cards ----
    win_rate = s["win_rate"] * 100
    roi = s["roi"] * 100
    cards = f"""
      <div class="cards">
        <div class="card">
          <div class="label">Net P&amp;L (resolved)</div>
          <div class="value {_pnl_class(s['pnl_usd'])}">{_fmt_money(s['pnl_usd'])}</div>
          <div class="sub">over {s['resolved']} resolved trades</div>
        </div>
        <div class="card">
          <div class="label">Win rate</div>
          <div class="value">{win_rate:.1f}%</div>
          <div class="sub">{s['won']} won / {s['lost']} lost</div>
        </div>
        <div class="card">
          <div class="label">ROI on staked</div>
          <div class="value {_pnl_class(s['roi'])}">{roi:+.1f}%</div>
          <div class="sub">${s['staked_usd']:,.2f} staked</div>
        </div>
        <div class="card">
          <div class="label">Open positions</div>
          <div class="value">{s['open']}</div>
          <div class="sub">awaiting resolution</div>
        </div>
        <div class="card">
          <div class="label">Today's P&amp;L</div>
          <div class="value {_pnl_class(today)}">{_fmt_money(today)}</div>
          <div class="sub">target ${target:,.0f}/day</div>
        </div>
      </div>
      <div class="targetbar" title="progress to daily target">
        <div class="targetfill" style="width:{target_pct:.0f}%"></div>
        <span class="targetlabel">{target_pct:.0f}% of ${target:,.0f} daily target</span>
      </div>
    """

    # ---- open positions table ----
    open_html = ['<table><thead><tr>'
                 '<th>Time (UTC)</th><th>Side</th><th>Stake</th><th>Price</th>'
                 '<th>Edge</th><th>Category</th><th>Resolves in</th><th>Market</th>'
                 '</tr></thead><tbody>']
    if not open_rows:
        open_html.append('<tr><td colspan="8" class="empty">No open positions.</td></tr>')
    for ts, mode, side, size, price, edge, status, pnl, cat, hrs, q in open_rows:
        hrs_str = f"{hrs:.1f}h" if hrs else "?"
        open_html.append(
            f"<tr><td>{html.escape(ts[:16])}</td>"
            f"<td><span class='side {side.lower()}'>{side}</span></td>"
            f"<td>${size:,.2f}</td><td>{price:.3f}</td>"
            f"<td>{edge:+.3f}</td><td>{html.escape(cat or '')}</td>"
            f"<td>{hrs_str}</td><td class='q'>{html.escape(q[:70])}</td></tr>"
        )
    open_html.append('</tbody></table>')

    # ---- resolved history table ----
    done_html = ['<table><thead><tr>'
                 '<th>Time (UTC)</th><th>Side</th><th>Stake</th><th>Price</th>'
                 '<th>Result</th><th>P&amp;L</th><th>Category</th><th>Market</th>'
                 '</tr></thead><tbody>']
    if not done_rows:
        done_html.append('<tr><td colspan="8" class="empty">'
                         'No resolved trades yet — they settle after the games finish.</td></tr>')
    for ts, mode, side, size, price, edge, status, pnl, cat, hrs, q in done_rows:
        badge = "won" if status == "WON" else "lost"
        done_html.append(
            f"<tr><td>{html.escape(ts[:16])}</td>"
            f"<td><span class='side {side.lower()}'>{side}</span></td>"
            f"<td>${size:,.2f}</td><td>{price:.3f}</td>"
            f"<td><span class='badge {badge}'>{status}</span></td>"
            f"<td class='{_pnl_class(pnl)}'>{_fmt_money(pnl)}</td>"
            f"<td>{html.escape(cat or '')}</td>"
            f"<td class='q'>{html.escape(q[:70])}</td></tr>"
        )
    done_html.append('</tbody></table>')

    # ---- category breakdown ----
    cat_html = ['<table><thead><tr>'
                '<th>Category</th><th>Trades</th><th>Won</th><th>Lost</th>'
                '<th>Win%</th><th>P&amp;L</th><th>Staked</th></tr></thead><tbody>']
    if not cats:
        cat_html.append('<tr><td colspan="7" class="empty">No resolved trades to break down yet.</td></tr>')
    for cat, n, won, lost, pnl, staked in cats:
        wr = (won / n * 100) if n else 0
        cat_html.append(
            f"<tr><td>{html.escape(cat or 'other')}</td><td>{n}</td>"
            f"<td>{won}</td><td>{lost}</td><td>{wr:.0f}%</td>"
            f"<td class='{_pnl_class(pnl)}'>{_fmt_money(pnl)}</td>"
            f"<td>${staked:,.2f}</td></tr>"
        )
    cat_html.append('</tbody></table>')

    mode_badge = "LIVE" if config.MODE == "LIVE" else "PAPER"
    mode_class = "live" if config.MODE == "LIVE" else "paper"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Polymarket Bot Dashboard</title>
<style>
  :root {{
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
    --muted:#8b949e; --pos:#3fb950; --neg:#f85149; --accent:#58a6ff;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1200px; margin:0 auto; padding:24px; }}
  header {{ display:flex; align-items:center; gap:14px; margin-bottom:6px; flex-wrap:wrap; }}
  h1 {{ font-size:22px; margin:0; }}
  .badge-mode {{ padding:3px 10px; border-radius:20px; font-size:12px; font-weight:700; }}
  .badge-mode.paper {{ background:#1f6feb33; color:var(--accent); border:1px solid #1f6feb55; }}
  .badge-mode.live {{ background:#f8514922; color:var(--neg); border:1px solid #f8514955; }}
  .gen {{ color:var(--muted); font-size:12px; margin-bottom:20px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
            gap:14px; margin-bottom:14px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px;
           padding:16px; }}
  .card .label {{ color:var(--muted); font-size:12px; text-transform:uppercase;
                  letter-spacing:.04em; }}
  .card .value {{ font-size:28px; font-weight:700; margin:6px 0 2px; }}
  .card .sub {{ color:var(--muted); font-size:12px; }}
  .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
  .targetbar {{ position:relative; height:26px; background:var(--panel);
                border:1px solid var(--border); border-radius:8px; overflow:hidden;
                margin-bottom:28px; }}
  .targetfill {{ height:100%; background:linear-gradient(90deg,#1f6feb,#3fb950);
                 transition:width .4s; }}
  .targetlabel {{ position:absolute; inset:0; display:flex; align-items:center;
                  justify-content:center; font-size:12px; color:var(--text); }}
  h2 {{ font-size:15px; margin:26px 0 10px; color:var(--text);
        border-left:3px solid var(--accent); padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel);
           border:1px solid var(--border); border-radius:10px; overflow:hidden;
           font-size:13px; }}
  th,td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--border); }}
  th {{ color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase;
        letter-spacing:.03em; background:#1c2129; }}
  tr:last-child td {{ border-bottom:none; }}
  td.q {{ color:var(--muted); }}
  .empty {{ text-align:center; color:var(--muted); padding:18px; }}
  .side {{ padding:1px 8px; border-radius:6px; font-weight:700; font-size:11px; }}
  .side.yes {{ background:#3fb95022; color:var(--pos); }}
  .side.no {{ background:#f8514922; color:var(--neg); }}
  .badge {{ padding:1px 8px; border-radius:6px; font-weight:700; font-size:11px; }}
  .badge.won {{ background:#3fb95022; color:var(--pos); }}
  .badge.lost {{ background:#f8514922; color:var(--neg); }}
  .note {{ background:#1c2129; border:1px solid var(--border); border-radius:10px;
           padding:14px 16px; color:var(--muted); font-size:13px; line-height:1.5;
           margin-top:24px; }}
  .note b {{ color:var(--text); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Polymarket Bot Dashboard</h1>
    <span class="badge-mode {mode_class}">{mode_badge}</span>
    <span class="badge-mode paper">profile: {config.STRATEGY.name}</span>
  </header>
  <div class="gen">Generated {generated} &middot; auto-refreshes every 60s &middot;
       re-run <code>python dashboard.py</code> to update data</div>

  {cards}

  <h2>Open positions ({len(open_rows)})</h2>
  {''.join(open_html)}

  <h2>Resolved history ({len(done_rows)})</h2>
  {''.join(done_html)}

  <h2>Performance by category</h2>
  {''.join(cat_html)}

  <div class="note">
    <b>Reading this:</b> Win rate alone is misleading — a 95% win rate on bets
    priced at 0.95 makes ~5&cent; per win but loses the full stake on a miss, so
    net P&amp;L (not win%) is what matters. Watch the <b>Net P&amp;L</b> and
    <b>ROI</b> cards. This is PAPER mode — no real money. Going LIVE requires a
    funded wallet and is never done automatically.
  </div>
</div>
</body>
</html>"""


def main():
    import os
    out = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html())
    print(f"Dashboard written to: {out}")
    print(f"Open it in your browser:  file:///{out.replace(chr(92), '/')}")


if __name__ == "__main__":
    main()
