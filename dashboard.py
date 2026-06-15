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
    real_days = store.real_daily_equity(60)      # the 30-day paper track record

    daily_budget = config.daily_budget()
    deposit = bk["initial_deposit"]
    # ALL headline/stat numbers come from REAL paper trades (store.performance_summary),
    # NOT the simulated backfill curve. The backfill is shown separately and clearly
    # labelled as a simulation in the Overview chart — it is never the headline.
    value = bk["total_equity"]
    profit = bk["profit"]
    ret = bk["return_pct"]
    tot_won = s["won"]
    tot_lost = s["lost"]
    resolved = tot_won + tot_lost
    win_rate = (tot_won / resolved * 100) if resolved else 0

    # ---- headline: a plain investment statement ----
    #   Invested  →  Net profit/loss  →  On stake (in open bets)  →  Net balance
    # Net balance = free cash you can invest or withdraw right now.
    net_profit = bk["profit"]              # equity - deposit
    on_stake = bk["open_exposure"]         # locked in open bets
    free_balance = bk["balance"]           # available to invest or withdraw
    net_equity = bk["total_equity"]        # free + on stake (full account value)
    pcls = _cls(net_profit)
    headline = f"""
      <div class="invest-card">
        <div class="ic-row">
          <div class="ic-box">
            <div class="ic-lab">Invested</div>
            <div class="ic-val">${deposit:,.2f}</div>
            <div class="ic-sub">on {dep_date}</div>
          </div>
          <div class="ic-box clickable" onclick="showTab('history', true)" title="View day-by-day profit">
            <div class="ic-lab">Net profit / loss <span class="ic-link">view daily ›</span></div>
            <div class="ic-val {pcls}">{_money(net_profit)}</div>
            <div class="ic-sub">{(net_profit/deposit*100) if deposit else 0:+.1f}% after invested</div>
          </div>
          <div class="ic-box clickable" onclick="showTab('open', true)" title="View open bets">
            <div class="ic-lab">On stake <span class="ic-link">view bets ›</span></div>
            <div class="ic-val">${on_stake:,.2f}</div>
            <div class="ic-sub">live in open bets</div>
          </div>
          <div class="ic-box highlight">
            <div class="ic-lab">Net balance</div>
            <div class="ic-val accent">${free_balance:,.2f}</div>
            <div class="ic-sub">free to invest or withdraw</div>
          </div>
        </div>
        <div class="ic-foot">
          Total account value <b>${net_equity:,.2f}</b>
          &nbsp;=&nbsp; ${free_balance:,.2f} free &nbsp;+&nbsp; ${on_stake:,.2f} on stake
          &nbsp;·&nbsp; as bets settle, winnings move from stake into your net balance.
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="s-val pos">{tot_won}</div>
          <div class="s-lab">Total won</div></div>
        <div class="stat"><div class="s-val neg">{tot_lost}</div>
          <div class="s-lab">Total lost</div></div>
        <div class="stat"><div class="s-val">{win_rate:.0f}%</div>
          <div class="s-lab">Win rate</div></div>
        <div class="stat"><div class="s-val {_cls(profit)}">{_money(profit)}</div>
          <div class="s-lab">Total profit</div></div>
        <div class="stat"><div class="s-val">{resolved}</div>
          <div class="s-lab">Total bets</div></div>
        <div class="stat"><div class="s-val">{s['open']}</div>
          <div class="s-lab">Open now</div></div>
      </div>
      <div class="caveat">
        ⚠️ <b>Paper / test money — no real funds.</b> This is a 30-day track record
        of the strategy applied to real market outcomes (slippage + fees included).
        Being honest about limits: the edge is measured on past resolved markets
        and is <b>not yet proven on live open markets</b>, and the sample is still
        thin. Treat results as a work-in-progress test of the edge, not a promise.
      </div>
    """

    # ---- charts ----
    # The real trade-by-trade curve IS the 30-day record now (no separate sim).
    real_curve_points = [(d[0], d[5]) for d in reversed(real_days)]
    real_equity_svg = _equity_chart(real_curve_points)
    donut_svg = _donut(win_rate, "Win rate")
    cat_bars = _cat_bars(cats)

    # ---- daily history (real results, builds forward) ----
    day_html = []
    for day, n_set, won, lost, dprofit, bal in real_days:
        day_html.append(
            f"<tr><td>{html.escape(day)}</td><td>{n_set}</td>"
            f"<td>{won}–{lost}</td>"
            f"<td class='{_cls(dprofit)}'>{_money(dprofit)}</td>"
            f"<td>${bal:,.2f}</td></tr>")
    if not day_html:
        day_html = ['<tr><td colspan="5" class="empty">'
                    'No settled days yet — history fills in as bets resolve daily.</td></tr>']

    # ---- category summary table (resolved data, richest per-category view) ----
    # Count CURRENTLY-OPEN bets per category too, so each category row shows
    # both settled performance and live exposure.
    open_by_cat = {}
    for _ts, _m, _sd, _sz, _pr, _e, _st, _pn, ocat, _h, _q, _sl in open_rows:
        key = (ocat or "other")
        open_by_cat[key] = open_by_cat.get(key, 0) + 1

    # Show EVERY category we've bet in — settled OR currently open — so the
    # report reflects all activity, not just categories that happen to have
    # resolved bets yet. Categories with only open bets show "pending".
    settled_cats = {(r[0] or "other"): r for r in cats}
    all_cat_names = set(settled_cats) | set(open_by_cat)

    cat_rows = []
    for name in sorted(all_cat_names,
                       key=lambda c: -((settled_cats.get(c) or (0,0,0,0,0,0))[4] or 0)):
        row = settled_cats.get(name)
        opn = open_by_cat.get(name, 0)
        if row:
            _c, n, won, lost, pnl, staked = row
            wr = (won / n * 100) if n else 0
            roi = (pnl / staked * 100) if staked else 0
            wr_cls = "pos" if wr >= 55 else ("neg" if wr < 50 else "")
            cat_rows.append(
                f"<tr><td><b>{html.escape(name)}</b></td>"
                f"<td>{n}</td>"
                f"<td><span class='pos'>{won}</span>–<span class='neg'>{lost}</span></td>"
                f"<td class='{wr_cls}'>{wr:.0f}%</td>"
                f"<td>${staked:,.2f}</td>"
                f"<td class='{_cls(pnl)}'>{_money(pnl)}</td>"
                f"<td class='{_cls(roi)}'>{roi:+.0f}%</td>"
                f"<td>{opn}</td></tr>")
        else:
            # only open bets so far — no settled results to report
            cat_rows.append(
                f"<tr><td><b>{html.escape(name)}</b></td>"
                f"<td>0</td><td>—</td>"
                f"<td class='muted-cell'>pending</td>"
                f"<td>—</td><td>—</td><td>—</td>"
                f"<td>{opn}</td></tr>")
    if not cat_rows:
        cat_rows = ['<tr><td colspan="8" class="empty">'
                    'No bets placed yet — category breakdown fills in as the bot trades.</td></tr>']

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
  .invest-card {{ background:linear-gradient(135deg,#161b22,#1c2333);
                  border:1px solid var(--accent); border-radius:14px;
                  padding:18px 18px 14px; margin-bottom:16px; }}
  .ic-row {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
             gap:12px; }}
  .ic-box {{ background:#0d111799; border:1px solid var(--border);
             border-radius:11px; padding:14px 16px; }}
  .ic-box.highlight {{ border-color:var(--accent); background:#1f6feb18; }}
  .ic-lab {{ color:var(--muted); font-size:11px; text-transform:uppercase;
             letter-spacing:.05em; }}
  .ic-val {{ font-size:30px; font-weight:800; margin:4px 0 2px; line-height:1.1; }}
  .ic-sub {{ color:var(--muted); font-size:11.5px; }}
  .ic-foot {{ color:var(--muted); font-size:12px; margin-top:12px;
              border-top:1px solid var(--border); padding-top:10px; }}
  .ic-foot b {{ color:var(--text); }}
  .ic-box.clickable {{ cursor:pointer; transition:border-color .15s, transform .1s; }}
  .ic-box.clickable:hover {{ border-color:var(--accent); transform:translateY(-1px); }}
  .ic-link {{ color:var(--accent); font-size:10px; font-weight:700;
              text-transform:none; letter-spacing:0; float:right; opacity:.85; }}
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
  .accent {{ color:var(--accent); }}
  .live-account {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap;
                   background:var(--panel); border:1px solid var(--border);
                   border-radius:10px; padding:11px 14px; margin:0 0 14px;
                   font-size:14px; }}
  .la-title {{ color:var(--muted); font-size:11px; text-transform:uppercase;
               letter-spacing:.05em; margin-right:4px; }}
  .la-item b {{ font-size:17px; }}
  .la-net {{ font-size:14px; }}
  .la-net b {{ font-size:22px; }}
  .la-tag {{ background:#1f6feb22; color:var(--accent); border:1px solid #1f6feb55;
             border-radius:6px; padding:1px 7px; font-size:10px; font-weight:700;
             text-transform:uppercase; letter-spacing:.04em; margin-left:4px; }}
  .la-sep {{ color:var(--muted); }}
  .la-note {{ color:var(--muted); font-size:11px; flex-basis:100%; margin-top:2px; }}
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
  .tabs {{ display:flex; gap:4px; flex-wrap:wrap; border-bottom:1px solid var(--border);
           margin:18px 0 16px; }}
  .tab {{ background:transparent; color:var(--muted); border:none;
          border-bottom:2px solid transparent; padding:10px 14px; font-size:13.5px;
          font-weight:600; cursor:pointer; border-radius:7px 7px 0 0; }}
  .tab:hover {{ color:var(--text); background:var(--panel); }}
  .tab.active {{ color:var(--accent); border-bottom-color:var(--accent); }}
  .panel-tab {{ display:none; }}
  .panel-tab.active {{ display:block; animation:fade .2s ease; }}
  @keyframes fade {{ from {{ opacity:0; }} to {{ opacity:1; }} }}
  .panel-tab h2:first-child {{ margin-top:4px; }}
  .note {{ color:var(--muted); font-size:12px; margin:0 0 10px; }}
  .sim-tag {{ background:#8a6d3b22; color:#d8a23b; border:1px solid #8a6d3b;
              border-radius:6px; padding:1px 8px; font-size:10px; font-weight:700;
              text-transform:uppercase; letter-spacing:.04em; margin-left:6px;
              vertical-align:middle; }}
  .muted-cell {{ color:var(--muted); font-style:italic; }}
</style></head>
<body><div class="wrap">
  <h1>My Polymarket Portfolio</h1>
  <div class="gen">Updated {generated} · auto-refreshes every 60s · PAPER (no real money)</div>

  {headline}

  <div class="actions">
    <button class="btn primary" onclick="run('resolve')">Update results</button>
    <button class="btn" onclick="run('longshot')">Place today's bets</button>
    <button class="btn" onclick="location.reload()">Refresh</button>
    <button class="btn" onclick="connectGitHub()" title="One-time: paste a token so the buttons work from this public link">🔗 Connect GitHub</button>
    <span id="status"></span>
  </div>
  <div class="note" style="margin-top:6px">
    Note: the bot already runs automatically every ~15 min. These buttons are for
    triggering an extra run on demand. On the public link they need a one-time
    “Connect GitHub” token (stored only in your browser).
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

  <nav class="tabs">
    <button class="tab active" data-tab="overview" onclick="showTab('overview')">📈 Overview</button>
    <button class="tab" data-tab="category" onclick="showTab('category')">📊 By Category</button>
    <button class="tab" data-tab="history" onclick="showTab('history')">📅 Daily History</button>
    <button class="tab" data-tab="open" onclick="showTab('open')">⏳ Open Bets ({len(open_rows)})</button>
    <button class="tab" data-tab="settled" onclick="showTab('settled')">✅ Settled ({len(done_rows)})</button>
  </nav>

  <section class="panel-tab active" id="tab-overview">
    <h2>Balance over time <span class="sim-tag">paper / test</span></h2>
    <div class="note">Your paper track record since {dep_date}: ${deposit:,.0f} grown
      to ${net_equity:,.2f} by applying the strategy to real market outcomes.
      Paper money — a test of the edge, not a promise.</div>
    <div class="chart-box">{real_equity_svg}</div>
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
  </section>

  <section class="panel-tab" id="tab-category">
    <h2>Results by category</h2>
    <div class="note">Shows every category the bot has bet in. "pending" = bets
      placed but not settled yet (results appear once games finish).</div>
    <table><thead><tr><th>Category</th><th>Bets</th><th>W–L</th><th>Win %</th>
      <th>Staked</th><th>Profit</th><th>ROI</th><th>Open</th></tr></thead>
      <tbody>{''.join(cat_rows)}</tbody></table>
  </section>

  <section class="panel-tab" id="tab-history">
    <h2>Daily history (since {dep_date})</h2>
    <div class="note">Day-by-day, built from every settled bet, chaining from the
      ${deposit:,.0f} starting balance.</div>
    <table><thead><tr><th>Day</th><th>Settled</th><th>W–L</th><th>Day profit</th>
      <th>Balance</th></tr></thead>
      <tbody>{''.join(day_html)}</tbody></table>
  </section>

  <section class="panel-tab" id="tab-open">
    <h2>Open bets ({len(open_rows)})</h2>
    <table><thead><tr><th>Side</th><th>Stake</th><th>Price</th><th>Category</th>
      <th>Resolves</th><th>Market</th></tr></thead>
      <tbody>{''.join(open_html)}</tbody></table>
  </section>

  <section class="panel-tab" id="tab-settled">
  <h2>Settled bets ({len(done_rows)})</h2>
  <table><thead><tr><th>Date</th><th>Side</th><th>Stake</th><th>Result</th>
    <th>Profit</th><th>Market</th></tr></thead>
    <tbody>{''.join(done_html)}</tbody></table>
  </section>
</div>
<script>
function showTab(name, scroll) {{
  document.querySelectorAll('.panel-tab').forEach(p =>
    p.classList.toggle('active', p.id === 'tab-' + name));
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.getAttribute('data-tab') === name));
  try {{ localStorage.setItem('activeTab', name); }} catch(e) {{}}
  if (scroll) {{
    var nav = document.querySelector('.tabs');
    if (nav) nav.scrollIntoView({{behavior:'smooth', block:'start'}});
  }}
}}
// Restore the last-viewed tab across the 60s auto-refresh so it doesn't reset
// (no scroll on restore, so the page doesn't jump while you're reading the top).
(function() {{
  try {{
    var t = localStorage.getItem('activeTab');
    if (t && document.getElementById('tab-' + t)) showTab(t, false);
  }} catch(e) {{}}
}})();
// On the LOCAL server we POST to /run/. On the public GitHub Pages link there is
// no server, so we trigger the GitHub Actions workflow directly. The token is
// NEVER stored in this page — it lives only in YOUR browser (localStorage), set
// once via the "Connect" button, so a public viewer never sees it.
var GH_REPO = "jigneshdavara314/ledger-01575474";
function isLocal() {{ return location.hostname === "localhost" || location.hostname === "127.0.0.1"; }}

async function run(action) {{
  var btns = document.querySelectorAll('.btn'), st = document.getElementById('status'),
      out = document.getElementById('output');
  btns.forEach(b => b.disabled = true);
  st.textContent = 'Running ' + action + '…';
  out.style.display = 'block'; out.textContent = 'Working…';
  try {{
    if (isLocal()) {{
      var r = await fetch('/run/' + action, {{method:'POST'}});
      out.textContent = await r.text();
      st.textContent = 'Done. Reloading…';
      setTimeout(() => location.reload(), 1500);
    }} else {{
      // public link -> trigger the GitHub workflow on demand
      var tok = localStorage.getItem('ghToken');
      if (!tok) {{
        out.textContent = 'One-time setup: click "Connect GitHub" and paste a ' +
          'fine-grained token (Actions: read+write on this repo only). It is saved ' +
          'in THIS browser only, never on the page.';
        st.textContent = 'Token needed.'; btns.forEach(b => b.disabled = false); return;
      }}
      var resp = await fetch('https://api.github.com/repos/' + GH_REPO +
        '/actions/workflows/bot.yml/dispatches', {{
        method: 'POST',
        headers: {{ 'Authorization': 'token ' + tok, 'Accept': 'application/vnd.github+json' }},
        body: JSON.stringify({{ ref: 'master' }})
      }});
      if (resp.status === 204) {{
        out.textContent = 'Triggered the cloud bot run (' + action + '). It runs the ' +
          'full cycle on GitHub; refresh in ~1-2 min to see results.';
        st.textContent = 'Triggered ✓';
      }} else {{
        out.textContent = 'GitHub returned ' + resp.status + '. Token may be wrong/expired.';
        st.textContent = 'Failed.';
      }}
      btns.forEach(b => b.disabled = false);
    }}
  }} catch (e) {{
    out.textContent = 'Error: ' + e;
    st.textContent = 'Failed.'; btns.forEach(b => b.disabled = false);
  }}
}}

function connectGitHub() {{
  var cur = localStorage.getItem('ghToken') ? '(a token is already saved)' : '';
  var tok = prompt('Paste a GitHub fine-grained token with "Actions: Read and write" ' +
    'on the ledger-01575474 repo ONLY. Saved in this browser only. ' + cur);
  if (tok) {{ localStorage.setItem('ghToken', tok.trim());
    document.getElementById('status').textContent = 'GitHub connected ✓ (this browser)'; }}
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
