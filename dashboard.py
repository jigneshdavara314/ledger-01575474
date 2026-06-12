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


def _market_link(question: str, slug: str) -> str:
    """Render the market question as a clickable Polymarket link (new tab)."""
    label = html.escape(question[:70])
    if slug:
        url = f"https://polymarket.com/event/{html.escape(slug)}"
        return (f'<a href="{url}" target="_blank" rel="noopener" '
                f'class="mlink">{label} ↗</a>')
    # no slug stored (older trades) -> link to Polymarket search as fallback
    return (f'<a href="https://polymarket.com/markets" target="_blank" '
            f'rel="noopener" class="mlink">{label}</a>')


def _load_backtest():
    """Load the saved strategy-backtest result, if present."""
    import os, json
    path = os.path.join(os.path.dirname(__file__), "backtest_result.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _backtest_section(bt) -> str:
    """Render the strategy-backtest validation as a matrix table."""
    if not bt or not bt.get("overall"):
        return ('<h2>Strategy backtest (validation)</h2>'
                '<div class="note">No backtest yet. Run '
                '<code>python run_strategy_backtest.py 30</code> to validate the '
                'strategy on the last 30 days of resolved markets.</div>')
    o = bt["overall"]
    gen = (bt.get("generated", "") or "")[:16]

    def verdict_class(v):
        if "CONFIRMED" in v:
            return "won"
        if "NEGATIVE" in v:
            return "lost"
        return ""

    rows = []
    # overall row first, then sub-types
    ordered = [("OVERALL", o)]
    ordered += sorted(bt.get("by_subtype", {}).items(),
                      key=lambda x: -((x[1] or {}).get("n", 0)))
    for name, s in ordered:
        if not s:
            continue
        beat = s["win_rate"] >= s["predicted_win"]
        beat_mark = "✓" if beat else "✗"
        rows.append(
            f"<tr><td><b>{html.escape(name)}</b></td>"
            f"<td>{s['n']}</td>"
            f"<td>{s['win_rate']*100:.0f}%</td>"
            f"<td>{s['predicted_win']*100:.0f}%</td>"
            f"<td class='{'pos' if beat else 'neg'}'>{beat_mark}</td>"
            f"<td>[{s['win_ci'][0]*100:.0f}–{s['win_ci'][1]*100:.0f}%]</td>"
            f"<td class='{_pnl_class(s['pnl'])}'>{_fmt_money(s['pnl'])}</td>"
            f"<td class='{_pnl_class(s['roi'])}'>{s['roi']*100:+.0f}%</td>"
            f"<td><span class='badge {verdict_class(s['verdict'])}'>"
            f"{html.escape(s['verdict'].split(' (')[0])}</span></td></tr>"
        )

    return f"""
      <h2>Strategy backtest — last {bt['days']} days (validation, generated {gen})</h2>
      <table>
        <thead><tr>
          <th>Segment</th><th>Bets</th><th>Actual win%</th><th>Predicted%</th>
          <th>Beat?</th><th>95% CI</th><th>P&amp;L</th><th>ROI</th><th>Verdict</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <div class="note">
        <b>This is the confidence check:</b> we replayed the exact betting logic on
        markets that already resolved. <b>Beat? ✓</b> means actual win rate met or
        exceeded what our model predicted (good — not over-optimistic). A verdict of
        <b>EDGE CONFIRMED</b> means the win-rate CI is above 50% with positive ROI.
        Past results don't guarantee future ones, but this is real out-of-sample evidence.
      </div>
    """


def _bankroll_section():
    """Compounding-bankroll equity banner + movement history."""
    try:
        from polybot import bankroll
        bk = bankroll.summary()
        moves = bankroll.history(20)
    except Exception:
        return ""
    rows = []
    for ts, kind, amount, bal_after, note in moves:
        rows.append(
            f"<tr><td>{html.escape(ts[:16])}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td class='{_pnl_class(amount)}'>{_fmt_money(amount)}</td>"
            f"<td>${bal_after:,.2f}</td>"
            f"<td class='q'>{html.escape((note or '')[:46])}</td></tr>"
        )
    if not rows:
        rows = ['<tr><td colspan="5" class="empty">No movements yet.</td></tr>']
    return f"""
      <div class="bankroll">
        <div class="bk-item"><div class="label">Deposit (total invested)</div>
          <div class="value">${bk['initial_deposit']:,.2f}</div></div>
        <div class="bk-item"><div class="label">Cash available</div>
          <div class="value">${bk['balance']:,.2f}</div></div>
        <div class="bk-item"><div class="label">In open bets</div>
          <div class="value">${bk['open_exposure']:,.2f}</div></div>
        <div class="bk-item"><div class="label">Total equity</div>
          <div class="value {_pnl_class(bk['profit'])}">${bk['total_equity']:,.2f}</div></div>
        <div class="bk-item"><div class="label">Profit / return</div>
          <div class="value {_pnl_class(bk['profit'])}">{_fmt_money(bk['profit'])}
            ({bk['return_pct']:+.1f}%)</div></div>
      </div>
      <h2>Bankroll history (compounding — ${bk['initial_deposit']:.0f} deposited once,
          winnings reinvested)</h2>
      <table><thead><tr><th>Time (UTC)</th><th>Type</th><th>Amount</th>
        <th>Balance after</th><th>Note</th></tr></thead>
        <tbody>{''.join(rows)}</tbody></table>
    """


def _daily_backtest_section():
    """Day-by-day $500-float profit table."""
    import os, json
    path = os.path.join(os.path.dirname(__file__), "daily_backtest.json")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return ""
    rows = []
    for r in d.get("daily", []):
        rows.append(
            f"<tr><td>{html.escape(r['day'])}</td><td>{r['bets']}</td>"
            f"<td>${r['staked']:,.2f}</td><td>${r['end_balance']:,.2f}</td>"
            f"<td class='{_pnl_class(r['profit'])}'>{_fmt_money(r['profit'])}</td></tr>"
        )
    if not rows:
        rows = ['<tr><td colspan="5" class="empty">No daily data.</td></tr>']
    return f"""
      <h2>Daily profit — {html.escape(d.get('model',''))}</h2>
      <table><thead><tr><th>Day</th><th>Bets</th><th>Staked</th>
        <th>End balance</th><th>Profit skimmed</th></tr></thead>
        <tbody>{''.join(rows)}</tbody></table>
      <div class="note">
        Each day starts at the $500 float; profit above $500 is swept out.
        <b>Total profit ${d.get('total_profit',0):+,.2f}</b> across
        {d.get('days_with_action',0)} active day(s), win rate
        {d.get('win_rate',0)*100:.0f}%. <b>One strong day is not the average</b> —
        many days have few or no qualifying markets. Real fills may be smaller
        (no historical depth data). Treat as a budget-scaled estimate.
      </div>
    """


def build_html() -> str:
    store.init_db()
    s = store.performance_summary()
    cats = store.category_summary()
    rows = store.recent_trades(200)
    today = store.today_pnl()
    target = config.DAILY_TARGET_USD
    bt = _load_backtest()

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
    for ts, mode, side, size, price, edge, status, pnl, cat, hrs, q, slug in open_rows:
        hrs_str = f"{hrs:.1f}h" if hrs else "?"
        open_html.append(
            f"<tr><td>{html.escape(ts[:16])}</td>"
            f"<td><span class='side {side.lower()}'>{side}</span></td>"
            f"<td>${size:,.2f}</td><td>{price:.3f}</td>"
            f"<td>{edge:+.3f}</td><td>{html.escape(cat or '')}</td>"
            f"<td>{hrs_str}</td><td class='q'>{_market_link(q, slug)}</td></tr>"
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
    for ts, mode, side, size, price, edge, status, pnl, cat, hrs, q, slug in done_rows:
        badge = "won" if status == "WON" else "lost"
        done_html.append(
            f"<tr><td>{html.escape(ts[:16])}</td>"
            f"<td><span class='side {side.lower()}'>{side}</span></td>"
            f"<td>${size:,.2f}</td><td>{price:.3f}</td>"
            f"<td><span class='badge {badge}'>{status}</span></td>"
            f"<td class='{_pnl_class(pnl)}'>{_fmt_money(pnl)}</td>"
            f"<td>{html.escape(cat or '')}</td>"
            f"<td class='q'>{_market_link(q, slug)}</td></tr>"
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
  .bankroll {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
               gap:12px; margin:6px 0 14px; padding:16px; border-radius:12px;
               background:linear-gradient(135deg,#161b22,#1c2333);
               border:1px solid var(--accent); }}
  .bk-item .label {{ color:var(--muted); font-size:11px; text-transform:uppercase; }}
  .bk-item .value {{ font-size:20px; font-weight:700; margin-top:4px; }}
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
  a.mlink {{ color:var(--accent); text-decoration:none; }}
  a.mlink:hover {{ text-decoration:underline; }}
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
  .actions {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center;
              margin-bottom:18px; }}
  .btn {{ background:var(--panel); color:var(--text); border:1px solid var(--border);
          padding:9px 14px; border-radius:8px; font-size:13px; font-weight:600;
          cursor:pointer; transition:.15s; }}
  .btn:hover {{ border-color:var(--accent); }}
  .btn.primary {{ background:#1f6feb; border-color:#1f6feb; }}
  .btn.primary:hover {{ background:#2a7bff; }}
  .btn.ghost {{ background:transparent; color:var(--muted); }}
  .btn:disabled {{ opacity:.5; cursor:wait; }}
  .status {{ font-size:13px; color:var(--muted); }}
  .output {{ background:#0b0e13; border:1px solid var(--border); border-radius:8px;
             padding:12px; font-size:12px; color:#cdd9e5; max-height:260px;
             overflow:auto; white-space:pre-wrap; margin-bottom:18px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Polymarket Bot Dashboard</h1>
    <span class="badge-mode {mode_class}">{mode_badge}</span>
    <span class="badge-mode paper">profile: {config.STRATEGY.name}</span>
  </header>
  <div class="gen">Generated {generated} &middot; auto-refreshes every 60s</div>

  <div class="actions">
    <button class="btn primary" onclick="run('resolve')">🔄 Resolve (settle finished games)</button>
    <button class="btn" onclick="run('longshot')">🎯 Run longshot fades</button>
    <button class="btn" onclick="run('lagwatch')">⚡ Check lag arb</button>
    <button class="btn" onclick="run('backtest')">📊 Run 30-day backtest</button>
    <button class="btn ghost" onclick="location.reload()">↻ Refresh page</button>
    <span id="status" class="status"></span>
  </div>
  <pre id="output" class="output" style="display:none"></pre>

  {cards}

  {_bankroll_section()}

  {_daily_backtest_section()}

  {_backtest_section(bt)}

  <h2>Open positions ({len(open_rows)})</h2>
  {''.join(open_html)}

  <h2>Resolved history ({len(done_rows)})</h2>
  {''.join(done_html)}

  <h2>Performance by category</h2>
  {''.join(cat_html)}

  <div class="note">
    <b>Buttons:</b> work only on the local server version
    (<code>http://localhost:8755</code>, started by <code>python serve.py</code>).
    They run the real bot commands on your machine. The <b>file://</b> and
    <b>GitHub Pages</b> versions are view-only.<br>
    <b>Reading this:</b> Win rate alone is misleading — a 95% win rate on bets
    priced at 0.95 makes ~5&cent; per win but loses the full stake on a miss, so
    net P&amp;L (not win%) is what matters. Watch the <b>Net P&amp;L</b> and
    <b>ROI</b> cards. This is PAPER mode — no real money. Going LIVE requires a
    funded wallet and is never done automatically.
  </div>
</div>
<script>
async function run(action) {{
  const btns = document.querySelectorAll('.btn');
  const status = document.getElementById('status');
  const output = document.getElementById('output');
  btns.forEach(b => b.disabled = true);
  status.textContent = 'Running ' + action + '... (this can take 30-60s)';
  output.style.display = 'block';
  output.textContent = 'Working...';
  try {{
    const res = await fetch('/run/' + action, {{ method: 'POST' }});
    const text = await res.text();
    output.textContent = text;
    status.textContent = 'Done. Reloading data...';
    setTimeout(() => location.reload(), 1500);
  }} catch (e) {{
    output.textContent = 'Error: ' + e + '\\n\\n(Is serve.py still running? Buttons only work via http://localhost:8755, not the file:// or GitHub Pages version.)';
    status.textContent = 'Failed.';
    btns.forEach(b => b.disabled = false);
  }}
}}
</script>
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
