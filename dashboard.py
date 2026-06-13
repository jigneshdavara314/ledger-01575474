"""
Clean investor dashboard for the Polymarket paper bot.

    python dashboard.py            # writes dashboard.html

Simple, focused view: "You invested $500 on 13-05-2026 — here's how it's doing."
Headline value, profit, win rate, a small per-category win% summary, and two
tables (open bets, settled bets). Nothing else — no backtest/capacity clutter.

Auto-refreshes every 60s. Served interactively by serve.py (with action buttons).
"""
import html
import datetime

from polybot import config, store, bankroll


def _money(v, plus=True):
    if v is None:
        return "-"
    sign = "+" if (plus and v >= 0) else ""
    return f"{sign}${v:,.2f}"


def _cls(v):
    if v is None:
        return ""
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _link(question, slug):
    label = html.escape((question or "")[:72])
    if slug:
        return (f'<a href="https://polymarket.com/event/{html.escape(slug)}" '
                f'target="_blank" rel="noopener" class="mlink">{label} ↗</a>')
    return f'<a href="https://polymarket.com/markets" target="_blank" class="mlink">{label}</a>'


def build_html() -> str:
    store.init_db()
    bk = bankroll.summary()
    dep_date = bankroll.deposit_date()
    s = store.performance_summary()
    cats = store.category_summary()
    rows = store.recent_trades(200)
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    open_rows = [r for r in rows if r[6] == "OPEN"]
    done_rows = [r for r in rows if r[6] in ("WON", "LOST")]
    equity_days = store.daily_equity(60)

    value = bk["total_equity"]
    profit = bk["profit"]
    ret = bk["return_pct"]
    win_rate = s["win_rate"] * 100

    # ---- headline ----
    headline = f"""
      <div class="hero">
        <div class="hero-label">Invested ${bk['initial_deposit']:,.0f} on {dep_date}</div>
        <div class="hero-value {_cls(profit)}">${value:,.2f}</div>
        <div class="hero-sub">
          <span class="{_cls(profit)}">{_money(profit)} ({ret:+.1f}%)</span>
          &nbsp;·&nbsp; ${bk['balance']:,.2f} cash + ${bk['open_exposure']:,.2f} in play
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="s-val">{s['won']}–{s['lost']}</div>
          <div class="s-lab">Won – Lost</div></div>
        <div class="stat"><div class="s-val">{win_rate:.0f}%</div>
          <div class="s-lab">Win rate</div></div>
        <div class="stat"><div class="s-val">{s['open']}</div>
          <div class="s-lab">Open bets</div></div>
        <div class="stat"><div class="s-val">{s['resolved']}</div>
          <div class="s-lab">Settled</div></div>
      </div>
    """

    # ---- daily history (real results, builds forward) ----
    day_html = []
    for day, n_set, won, lost, dprofit, bal in equity_days:
        day_html.append(
            f"<tr><td>{html.escape(day)}</td><td>{n_set}</td>"
            f"<td>{won}–{lost}</td>"
            f"<td class='{_cls(dprofit)}'>{_money(dprofit)}</td>"
            f"<td>${bal:,.2f}</td></tr>")
    if not day_html:
        day_html = ['<tr><td colspan="5" class="empty">'
                    'No settled days yet — history fills in as bets resolve daily.</td></tr>']

    # ---- category summary (simple, only resolved data) ----
    cat_rows = []
    for cat, n, won, lost, pnl, staked in cats:
        wr = (won / n * 100) if n else 0
        cat_rows.append(
            f"<tr><td>{html.escape(cat or 'other')}</td>"
            f"<td>{won}–{lost}</td><td>{wr:.0f}%</td>"
            f"<td class='{_cls(pnl)}'>{_money(pnl)}</td></tr>")
    if not cat_rows:
        cat_rows = ['<tr><td colspan="4" class="empty">No settled bets yet.</td></tr>']

    # ---- open bets ----
    open_html = []
    for ts, mode, side, size, price, edge, status, pnl, cat, hrs, q, slug in open_rows:
        hrs_str = f"{hrs:.0f}h" if hrs else "—"
        open_html.append(
            f"<tr><td><span class='side {side.lower()}'>{side}</span></td>"
            f"<td>${size:,.2f}</td><td>{price:.2f}</td>"
            f"<td>{html.escape(cat or '')}</td><td>{hrs_str}</td>"
            f"<td class='q'>{_link(q, slug)}</td></tr>")
    if not open_html:
        open_html = ['<tr><td colspan="6" class="empty">No open bets right now.</td></tr>']

    # ---- settled bets ----
    done_html = []
    for ts, mode, side, size, price, edge, status, pnl, cat, hrs, q, slug in done_rows:
        badge = "won" if status == "WON" else "lost"
        done_html.append(
            f"<tr><td>{html.escape(ts[:10])}</td>"
            f"<td><span class='side {side.lower()}'>{side}</span></td>"
            f"<td>${size:,.2f}</td>"
            f"<td><span class='badge {badge}'>{status}</span></td>"
            f"<td class='{_cls(pnl)}'>{_money(pnl)}</td>"
            f"<td class='q'>{_link(q, slug)}</td></tr>")
    if not done_html:
        done_html = ['<tr><td colspan="6" class="empty">Nothing settled yet — '
                     'bets settle after the games finish.</td></tr>']

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>My Polymarket Portfolio</title>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
           --muted:#8b949e; --pos:#3fb950; --neg:#f85149; --accent:#58a6ff; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
          font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; }}
  .wrap {{ max-width:920px; margin:0 auto; padding:24px; }}
  h1 {{ font-size:18px; margin:0 0 2px; }}
  .gen {{ color:var(--muted); font-size:12px; margin-bottom:18px; }}
  .hero {{ background:linear-gradient(135deg,#161b22,#1c2333);
           border:1px solid var(--accent); border-radius:14px; padding:24px;
           text-align:center; margin-bottom:14px; }}
  .hero-label {{ color:var(--muted); font-size:13px; text-transform:uppercase;
                 letter-spacing:.05em; }}
  .hero-value {{ font-size:46px; font-weight:800; margin:6px 0; }}
  .hero-sub {{ font-size:15px; color:var(--muted); }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px;
            margin-bottom:24px; }}
  .stat {{ background:var(--panel); border:1px solid var(--border);
           border-radius:10px; padding:14px; text-align:center; }}
  .s-val {{ font-size:24px; font-weight:700; }}
  .s-lab {{ color:var(--muted); font-size:11px; text-transform:uppercase; margin-top:2px; }}
  .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
  h2 {{ font-size:14px; margin:22px 0 8px; border-left:3px solid var(--accent);
        padding-left:9px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel);
           border:1px solid var(--border); border-radius:10px; overflow:hidden;
           font-size:13px; }}
  th,td {{ text-align:left; padding:9px 11px; border-bottom:1px solid var(--border); }}
  th {{ color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase;
        background:#1c2129; }}
  tr:last-child td {{ border-bottom:none; }}
  td.q {{ color:var(--muted); }}
  a.mlink {{ color:var(--accent); text-decoration:none; }}
  a.mlink:hover {{ text-decoration:underline; }}
  .empty {{ text-align:center; color:var(--muted); padding:16px; }}
  .side {{ padding:1px 7px; border-radius:5px; font-weight:700; font-size:11px; }}
  .side.yes {{ background:#3fb95022; color:var(--pos); }}
  .side.no {{ background:#f8514922; color:var(--neg); }}
  .badge {{ padding:1px 7px; border-radius:5px; font-weight:700; font-size:11px; }}
  .badge.won {{ background:#3fb95022; color:var(--pos); }}
  .badge.lost {{ background:#f8514922; color:var(--neg); }}
  .actions {{ display:flex; gap:8px; flex-wrap:wrap; margin:8px 0 4px; }}
  .btn {{ background:var(--panel); color:var(--text); border:1px solid var(--border);
          padding:8px 13px; border-radius:8px; font-size:13px; font-weight:600;
          cursor:pointer; }}
  .btn:hover {{ border-color:var(--accent); }}
  .btn.primary {{ background:#1f6feb; border-color:#1f6feb; }}
  .btn:disabled {{ opacity:.5; cursor:wait; }}
  #status {{ font-size:12px; color:var(--muted); align-self:center; }}
  #output {{ background:#0b0e13; border:1px solid var(--border); border-radius:8px;
             padding:10px; font-size:12px; color:#cdd9e5; max-height:200px;
             overflow:auto; white-space:pre-wrap; margin:8px 0; display:none; }}
</style></head>
<body><div class="wrap">
  <h1>My Polymarket Portfolio</h1>
  <div class="gen">Updated {generated} · auto-refreshes every 60s · PAPER (no real money)</div>

  {headline}

  <div class="actions">
    <button class="btn primary" onclick="run('resolve')">Update results</button>
    <button class="btn" onclick="run('longshot')">Place today's bets</button>
    <button class="btn" onclick="location.reload()">Refresh</button>
    <span id="status"></span>
  </div>
  <pre id="output"></pre>

  <h2>Daily history (since {dep_date})</h2>
  <table><thead><tr><th>Day</th><th>Settled</th><th>W–L</th><th>Day profit</th>
    <th>Balance</th></tr></thead>
    <tbody>{''.join(day_html)}</tbody></table>

  <h2>By category</h2>
  <table><thead><tr><th>Category</th><th>W–L</th><th>Win%</th><th>Profit</th></tr></thead>
    <tbody>{''.join(cat_rows)}</tbody></table>

  <h2>Open bets ({len(open_rows)})</h2>
  <table><thead><tr><th>Side</th><th>Stake</th><th>Price</th><th>Category</th>
    <th>Resolves</th><th>Market</th></tr></thead>
    <tbody>{''.join(open_html)}</tbody></table>

  <h2>Settled bets ({len(done_rows)})</h2>
  <table><thead><tr><th>Date</th><th>Side</th><th>Stake</th><th>Result</th>
    <th>Profit</th><th>Market</th></tr></thead>
    <tbody>{''.join(done_html)}</tbody></table>
</div>
<script>
async function run(action) {{
  var btns = document.querySelectorAll('.btn'), st = document.getElementById('status'),
      out = document.getElementById('output');
  btns.forEach(b => b.disabled = true);
  st.textContent = 'Running ' + action + '…';
  out.style.display = 'block'; out.textContent = 'Working…';
  try {{
    var r = await fetch('/run/' + action, {{method:'POST'}});
    out.textContent = await r.text();
    st.textContent = 'Done. Reloading…';
    setTimeout(() => location.reload(), 1500);
  }} catch (e) {{
    out.textContent = 'Error: ' + e + ' (buttons work only on the local server)';
    st.textContent = 'Failed.'; btns.forEach(b => b.disabled = false);
  }}
}}
</script>
</body></html>"""


def main():
    import os
    out = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html())
    print(f"Dashboard written to: {out}")


if __name__ == "__main__":
    main()
