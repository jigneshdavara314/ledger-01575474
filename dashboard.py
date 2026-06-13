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


def _equity_chart(points, w=860, h=240, pad=34):
    """Inline SVG line chart of balance over time. points = [(day, balance), ...]
    oldest-first."""
    if len(points) < 2:
        return '<div class="note">Not enough days yet to chart.</div>'
    days = [p[0] for p in points]
    vals = [p[1] for p in points]
    vmin, vmax = min(vals), max(vals)
    if vmax == vmin:
        vmax = vmin + 1
    span = vmax - vmin
    n = len(vals)

    def x(i):
        return pad + (w - 2 * pad) * i / (n - 1)

    def y(v):
        return h - pad - (h - 2 * pad) * (v - vmin) / span

    # line + area path
    line_pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = (f"M {x(0):.1f},{h-pad:.1f} L " +
            " L ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals)) +
            f" L {x(n-1):.1f},{h-pad:.1f} Z")
    # gridlines + y labels (4 levels)
    grid = ""
    for k in range(5):
        gv = vmin + span * k / 4
        gy = y(gv)
        grid += (f'<line x1="{pad}" y1="{gy:.1f}" x2="{w-pad}" y2="{gy:.1f}" '
                 f'class="grid"/>'
                 f'<text x="6" y="{gy+4:.1f}" class="axlab">${gv:,.0f}</text>')
    # x labels: first, middle, last
    xlabs = ""
    for i in (0, n // 2, n - 1):
        xlabs += f'<text x="{x(i):.1f}" y="{h-8}" class="axlab" text-anchor="middle">{days[i][5:]}</text>'
    up = vals[-1] >= vals[0]
    color = "#3fb950" if up else "#f85149"
    return f"""
      <svg viewBox="0 0 {w} {h}" class="chart" preserveAspectRatio="xMidYMid meet">
        <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="{color}" stop-opacity="0.35"/>
          <stop offset="1" stop-color="{color}" stop-opacity="0"/>
        </linearGradient></defs>
        {grid}
        <path d="{area}" fill="url(#g)"/>
        <polyline points="{line_pts}" fill="none" stroke="{color}" stroke-width="2.5"/>
        {xlabs}
      </svg>
    """


def _donut(pct, label, w=130):
    """Win-rate donut: pct 0-100."""
    import math
    r, cx, cy, sw = 48, w/2, w/2, 12
    circ = 2 * math.pi * r
    filled = circ * (pct / 100)
    color = "#3fb950" if pct >= 55 else ("#d8a23b" if pct >= 50 else "#f85149")
    return f"""
      <svg viewBox="0 0 {w} {w}" class="donut">
        <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#30363d" stroke-width="{sw}"/>
        <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{sw}"
          stroke-dasharray="{filled:.1f} {circ:.1f}" stroke-linecap="round"
          transform="rotate(-90 {cx} {cy})"/>
        <text x="{cx}" y="{cy-2}" text-anchor="middle" class="donut-pct">{pct:.0f}%</text>
        <text x="{cx}" y="{cy+16}" text-anchor="middle" class="donut-lab">{label}</text>
      </svg>
    """


def _cat_bars(cats):
    """Horizontal win% bars per category."""
    bars = []
    for cat, n, won, lost, pnl, staked in cats:
        wr = (won / n * 100) if n else 0
        color = "#3fb950" if wr >= 55 else ("#d8a23b" if wr >= 50 else "#f85149")
        bars.append(f"""
          <div class="bar-row">
            <div class="bar-name">{html.escape(cat or 'other')}</div>
            <div class="bar-track"><div class="bar-fill" style="width:{wr:.0f}%;background:{color}"></div></div>
            <div class="bar-val">{wr:.0f}% <span class="bar-sub">({won}–{lost})</span></div>
          </div>""")
    if not bars:
        return '<div class="note">No settled bets yet.</div>'
    return f'<div class="bars">{"".join(bars)}</div>'


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
    equity_days = store.daily_equity(60)  # newest first

    daily_budget = config.daily_budget()
    deposit = bk["initial_deposit"]
    # Headline value = the backfilled equity curve's latest balance (the real
    # "what would $500 be now" answer). Falls back to live equity if no curve.
    if equity_days:
        value = equity_days[0][5]            # balance_after of most recent day
        tot_won = sum(d[2] for d in equity_days)
        tot_lost = sum(d[3] for d in equity_days)
    else:
        value = bk["total_equity"]
        tot_won, tot_lost = s["won"], s["lost"]
    profit = round(value - deposit, 2)
    ret = round(profit / deposit * 100, 1) if deposit else 0
    resolved = tot_won + tot_lost
    win_rate = (tot_won / resolved * 100) if resolved else 0

    # ---- headline ----
    headline = f"""
      <div class="hero">
        <div class="hero-label">Invested ${deposit:,.0f} on {dep_date}</div>
        <div class="hero-value {_cls(profit)}">${value:,.2f}</div>
        <div class="hero-sub">
          <span class="{_cls(profit)}">{_money(profit)} ({ret:+.1f}%)</span>
          &nbsp;·&nbsp; over {len(equity_days)} days
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="s-val pos">{tot_won}</div>
          <div class="s-lab">Total won</div></div>
        <div class="stat"><div class="s-val neg">{tot_lost}</div>
          <div class="s-lab">Total lost</div></div>
        <div class="stat"><div class="s-val">{resolved}</div>
          <div class="s-lab">Total bets</div></div>
        <div class="stat"><div class="s-val">{win_rate:.0f}%</div>
          <div class="s-lab">Win rate</div></div>
        <div class="stat"><div class="s-val {_cls(profit)}">{_money(profit)}</div>
          <div class="s-lab">Total profit</div></div>
        <div class="stat"><div class="s-val">{s['open']}</div>
          <div class="s-lab">Open now</div></div>
      </div>
      <div class="caveat">
        ⚠️ This is a <b>simulated replay</b> of the strategy on real resolved
        markets since {dep_date} — real prices, real outcomes, our real rules.
        The win rate (~{win_rate:.0f}%) is genuine. Every bet is now
        <b>depth-limited</b> (capped at 3% of each market's actual traded volume),
        so it no longer assumes unlimited size in thin markets — this is roughly
        half the un-capped ceiling. One optimism remains: it assumes you got
        <b>filled at the mid-life price</b> on each bet; real fills slip. Paper
        only — not a promise.
      </div>
    """

    # ---- charts ----
    curve_points = [(d[0], d[5]) for d in reversed(equity_days)]  # oldest-first
    equity_svg = _equity_chart(curve_points)
    donut_svg = _donut(win_rate, "Win rate")
    cat_bars = _cat_bars(cats)

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
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
            gap:10px; margin-bottom:18px; }}
  .stat {{ background:var(--panel); border:1px solid var(--border);
           border-radius:10px; padding:14px; text-align:center; }}
  .s-val {{ font-size:24px; font-weight:700; }}
  .s-lab {{ color:var(--muted); font-size:11px; text-transform:uppercase; margin-top:2px; }}
  .caveat {{ background:#2a1f0e; border:1px solid #8a6d3b; border-radius:10px;
             padding:12px 14px; color:#d8c08a; font-size:12.5px; line-height:1.5;
             margin-bottom:22px; }}
  .caveat b {{ color:#f0d99a; }}
  .chart-box {{ background:var(--panel); border:1px solid var(--border);
                border-radius:12px; padding:14px; margin-bottom:14px; }}
  .chart {{ width:100%; height:auto; display:block; }}
  .grid {{ stroke:#21262d; stroke-width:1; }}
  .axlab {{ fill:var(--muted); font-size:10px; }}
  .chart-grid {{ display:grid; grid-template-columns:180px 1fr; gap:14px; margin-bottom:14px; }}
  @media(max-width:600px) {{ .chart-grid {{ grid-template-columns:1fr; }} }}
  .chart-card {{ background:var(--panel); border:1px solid var(--border);
                 border-radius:12px; padding:14px; }}
  .chart-title {{ color:var(--muted); font-size:11px; text-transform:uppercase;
                  letter-spacing:.04em; margin-bottom:8px; }}
  .donut {{ width:130px; height:130px; display:block; margin:0 auto; }}
  .donut-pct {{ fill:var(--text); font-size:24px; font-weight:800; }}
  .donut-lab {{ fill:var(--muted); font-size:10px; }}
  .bars {{ display:flex; flex-direction:column; gap:8px; }}
  .bar-row {{ display:grid; grid-template-columns:130px 1fr 90px; align-items:center; gap:10px; }}
  .bar-name {{ font-size:12px; }}
  .bar-track {{ background:#21262d; border-radius:6px; height:16px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:6px; transition:width .4s; }}
  .bar-val {{ font-size:12px; font-weight:600; text-align:right; }}
  .bar-sub {{ color:var(--muted); font-weight:400; font-size:11px; }}
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
  .budget-box {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap;
                 background:var(--panel); border:1px solid var(--border);
                 border-radius:10px; padding:11px 14px; margin:4px 0 8px; }}
  .budget-lab {{ color:var(--muted); font-size:12px; text-transform:uppercase;
                 letter-spacing:.04em; }}
  .budget-val {{ font-size:20px; font-weight:800; color:var(--accent); }}
  .budget-edit {{ display:flex; gap:6px; align-items:center; }}
  .budget-edit input {{ width:90px; background:#0b0e13; color:var(--text);
                        border:1px solid var(--border); border-radius:7px;
                        padding:6px 8px; font-size:13px; }}
  .budget-note {{ color:var(--muted); font-size:11.5px; }}
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
  <div class="budget-box">
    <span class="budget-lab">Daily investment budget</span>
    <span class="budget-val">${daily_budget:,.0f}</span>
    <span class="budget-edit">
      <input id="budgetInput" type="number" min="1" step="50" value="{daily_budget:.0f}">
      <button class="btn" onclick="setBudget()">Save</button>
    </span>
    <span class="budget-note">/ day — raise this anytime to invest more. Compounds from your balance.</span>
  </div>
  <pre id="output"></pre>

  <h2>Balance over time</h2>
  <div class="chart-box">{equity_svg}</div>

  <div class="chart-grid">
    <div class="chart-card">
      <div class="chart-title">Overall win rate</div>
      {donut_svg}
    </div>
    <div class="chart-card wide">
      <div class="chart-title">Win % by category</div>
      {cat_bars}
    </div>
  </div>

  <h2>Daily history (since {dep_date})</h2>
  <table><thead><tr><th>Day</th><th>Settled</th><th>W–L</th><th>Day profit</th>
    <th>Balance</th></tr></thead>
    <tbody>{''.join(day_html)}</tbody></table>

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
async function setBudget() {{
  var v = document.getElementById('budgetInput').value,
      st = document.getElementById('status');
  st.textContent = 'Saving budget…';
  try {{
    var r = await fetch('/set-budget/' + encodeURIComponent(v), {{method:'POST'}});
    st.textContent = await r.text() + ' Reloading…';
    setTimeout(() => location.reload(), 1200);
  }} catch (e) {{
    st.textContent = 'Budget save works only on the local server.';
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
