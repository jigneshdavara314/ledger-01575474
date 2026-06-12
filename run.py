"""
Polymarket AI Trading Bot — main command-line entry point.

    python run.py scout         # scan short-term markets by category (no trades)
    python run.py export        # dump markets to estimates_todo.json for AI estimation
    python run.py short         # scan + trade short-term markets (paper or live)
    python run.py loop          # keep scanning + trading every SCAN_INTERVAL
    python run.py scan          # legacy: scan all markets (no category filter)
    python run.py trade         # legacy: trade all markets
    python run.py lagwatch      # resolution-lag arb: buy known winners still cheap
    python run.py resolve       # settle open positions against real outcomes
    python run.py history       # show recent trades from the database
    python run.py report        # win rate + P&L over all resolved trades
    python run.py status        # show current config / mode

Short-term strategy workflow:
    1. python run.py scout      # see what opportunities look like right now
    2. python run.py short      # paper-bet on them
    3. ...wait hours for markets to resolve (sports/esports resolve same day)...
    4. python run.py resolve    # settle them and book P&L
    5. python run.py report     # see actual win rate and category breakdown

All config lives in .env  (MODE, PROFILE, ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
"""
import sys
import time

from polybot import config, store
from polybot.market_data import fetch_markets, fetch_short_term_markets, fetch_resolution
from polybot.strategy import evaluate
from polybot.executor import Executor


import os as _os

USE_CLAUDE = bool(config.ANTHROPIC_API_KEY)
USE_OPENAI = bool(config.OPENAI_API_KEY)
# Manual mode is active whenever an estimates.json file exists (Claude-in-the-loop).
USE_MANUAL = _os.path.exists(config.ESTIMATES_PATH)
USE_AI     = USE_CLAUDE or USE_OPENAI or USE_MANUAL
AI_ENGINE  = "claude" if USE_CLAUDE else ("openai" if USE_OPENAI else "manual")


def _banner():
    if USE_CLAUDE:
        ai_tag = "claude-haiku (API)"
    elif USE_OPENAI:
        ai_tag = "gpt-4o-mini (API)"
    elif USE_MANUAL:
        ai_tag = "manual (estimates.json — Claude in the loop)"
    else:
        ai_tag = "heuristic (no AI — run 'export' to use Claude)"
    print("=" * 68)
    print(f"  Polymarket AI Bot   mode={config.MODE}   profile={config.STRATEGY.name}")
    print(f"  AI estimator : {ai_tag}")
    print(f"  Target cats  : {', '.join(config.TARGET_CATEGORIES)}")
    print(f"  Max hours    : {config.MAX_HOURS_TO_RESOLUTION}h   Daily target: ${config.DAILY_TARGET_USD:.0f}")
    print("=" * 68)


# ---------------------------------------------------------------------------
# SCOUT — show what's available without trading
# ---------------------------------------------------------------------------

def cmd_scout():
    """Show the best short-term opportunities by category. No trades placed."""
    _banner()
    store.init_db()

    markets = fetch_short_term_markets()
    if not markets:
        print("No short-term markets found in target categories right now.")
        print(f"  (looking for markets resolving within {config.MAX_HOURS_TO_RESOLUTION}h "
              f"in: {', '.join(config.TARGET_CATEGORIES)})")
        return

    # Group by category for a clean overview
    from collections import defaultdict
    by_cat = defaultdict(list)
    for m in markets:
        sig = evaluate(m, use_ai=False)  # heuristic only for scouting (fast)
        by_cat[m.category].append((m, sig))

    print(f"\nFound {len(markets)} markets across {len(by_cat)} categories.\n")
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        signals = [(m, s) for m, s in items if s is not None]
        print(f"  [{cat.upper():10}]  {len(items):3} markets  |  {len(signals):2} with edge")
        for m, s in sorted(signals, key=lambda x: -x[1].edge)[:3]:
            print(f"    {s.side:3} edge={s.edge:+.3f} fair={s.fair_prob:.2f} mkt={s.market_prob:.2f} "
                  f"hrs={m.hours_to_resolution:5.1f}  {m.question[:55]}")
        # Also show a few no-signal markets for transparency
        no_sig = [(m, s) for m, s in items if s is None][:2]
        for m, _ in no_sig:
            print(f"         (no edge)                                         "
                  f"hrs={m.hours_to_resolution:5.1f}  {m.question[:55]}")

    today_pnl = store.today_pnl()
    print(f"\n  Today's realised P&L: ${today_pnl:+.2f}   target: ${config.DAILY_TARGET_USD:.0f}/day")


# ---------------------------------------------------------------------------
# EXPORT — dump markets for manual (Claude-in-the-loop) estimation
# ---------------------------------------------------------------------------

def cmd_export():
    """
    Write all current short-term markets to estimates_todo.json so an AI
    (e.g. Claude in this session) can fill in fair-probability estimates.

    Workflow:
      1. python run.py export          -> writes estimates_todo.json
      2. AI reads it, writes estimates.json mapping condition_id -> prob
      3. python run.py short           -> uses those estimates to trade
    """
    import json
    _banner()
    markets = fetch_short_term_markets()
    if not markets:
        print("No short-term markets found to export.")
        return

    todo = []
    for m in markets:
        todo.append({
            "condition_id": m.condition_id,
            "question": m.question,
            "category": m.category,
            "event": m.event_title,
            "market_price_yes": round(m.price_yes, 4),
            "hours_to_resolution": m.hours_to_resolution,
            "liquidity": round(m.liquidity, 0),
            # AI fills this in -> our estimated TRUE probability of YES
            "fair_prob": None,
        })

    out_path = "estimates_todo.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(todo, f, indent=2, ensure_ascii=False)

    print(f"\nExported {len(todo)} markets to {out_path}")
    print("Next: have the AI fill in 'fair_prob' for each, save as estimates.json,")
    print("then run:  python run.py short")


# ---------------------------------------------------------------------------
# SHORT — scan short-term markets + trade
# ---------------------------------------------------------------------------

def cmd_short_trade(scout_only: bool = False):
    """Scan short-term markets and optionally place trades."""
    _banner()
    store.init_db()
    executor = Executor() if not scout_only else None

    markets = fetch_short_term_markets()
    if not markets:
        print("No short-term markets found in target categories right now.")
        return

    print(f"Found {len(markets)} markets. Analysing with {AI_ENGINE} estimator...\n")

    signals = []
    for m in markets:
        sig = evaluate(m, use_ai=USE_AI, ai_engine=AI_ENGINE)
        if sig:
            signals.append(sig)

    if not signals:
        print("No tradable signals this scan.")
        _print_filter_hint(markets)
        return

    signals.sort(key=lambda s: s.edge, reverse=True)
    print(f"{'side':4} {'$':>6} {'edge':>7} {'fair':>5} {'mkt':>5} {'cat':>10} {'hrs':>5}  question")
    print("-" * 100)

    for s in signals:
        hrs_tag = f"{s.market.hours_to_resolution:5.1f}h"
        print(f"{s.side:4} ${s.size_usd:>5.2f} {s.edge:>+7.3f} "
              f"{s.fair_prob:>5.2f} {s.market_prob:>5.2f} "
              f"{s.market.category:>10} {hrs_tag}  {s.market.question[:45]}")

        if not scout_only:
            if store.already_open(s.market.condition_id):
                print("     -> skipped (already open)")
                continue
            if store.open_position_count() >= config.STRATEGY.max_open_positions:
                print("     -> skipped (max open positions reached)")
                continue
            result = executor.execute(s)
            print(f"     -> {result['mode']} {result['status']} "
                  f"@ {result.get('price')}  id={result.get('order_id','-')}")

    today_pnl = store.today_pnl()
    print(f"\n  Today's realised P&L: ${today_pnl:+.2f}   target: ${config.DAILY_TARGET_USD:.0f}/day")


def _print_filter_hint(markets):
    """Show why no signals fired to help with tuning."""
    cfg = config.STRATEGY
    low_liq = sum(1 for m in markets if m.liquidity < cfg.min_liquidity_usd)
    wide_spread = sum(1 for m in markets if m.spread and m.spread > cfg.max_spread)
    price_extreme = sum(1 for m in markets
                        if not (cfg.min_price <= m.price_yes <= cfg.max_price))
    print(f"  Filter breakdown: {low_liq} low-liquidity  "
          f"{wide_spread} wide-spread  {price_extreme} price out of range")
    print("  Tip: lower MIN_EDGE or try more categories in .env CATEGORIES=")


# ---------------------------------------------------------------------------
# LEGACY scan / trade (all markets, no category filter)
# ---------------------------------------------------------------------------

def _cmd_scan_all(trade: bool = False):
    _banner()
    store.init_db()
    executor = Executor() if trade else None
    markets = fetch_markets(limit=config.MARKET_LIMIT)
    print(f"Fetched {len(markets)} markets. Analysing...\n")

    signals = []
    for m in markets:
        sig = evaluate(m, use_ai=USE_AI, ai_engine=AI_ENGINE)
        if sig:
            signals.append(sig)

    if not signals:
        print("No tradable signals this scan.")
        return

    signals.sort(key=lambda s: s.edge, reverse=True)
    for s in signals:
        print(f"[{s.side:3}] ${s.size_usd:>6.2f}  edge={s.edge:+.3f}  "
              f"fair={s.fair_prob:.2f} mkt={s.market_prob:.2f}  | {s.market.question[:60]}")
        if trade:
            if store.already_open(s.market.condition_id):
                print("       -> skipped (already have a position in this market)")
                continue
            if store.open_position_count() >= config.STRATEGY.max_open_positions:
                print("       -> skipped (max open positions reached)")
                continue
            result = executor.execute(s)
            print(f"       -> {result['mode']} {result['status']} "
                  f"@ {result.get('price')}  id={result.get('order_id','-')}")


# ---------------------------------------------------------------------------
# LOOP — continuous short-term scanning
# ---------------------------------------------------------------------------

def cmd_loop():
    print(f"Short-term loop every {config.SCAN_INTERVAL}s. Ctrl+C to stop.\n")
    try:
        while True:
            cmd_short_trade(scout_only=False)
            print(f"\n--- sleeping {config.SCAN_INTERVAL}s ---\n")
            time.sleep(config.SCAN_INTERVAL)
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# LONGSHOT — fade overpriced exact-score / spread longshots (buy NO)
# ---------------------------------------------------------------------------

def cmd_longshot(trade: bool = True):
    """
    Find overpriced longshot markets (exact-score, spreads) and buy NO on each,
    spread across many small bets. Based on the calibration study finding that
    these are systematically overpriced (favorite-longshot bias).
    """
    from polybot.longshot import find_longshot_fades
    from polybot.strategy import Signal

    _banner()
    store.init_db()
    executor = Executor() if trade else None

    print("Scanning for overpriced longshots to fade (buy NO)...\n")
    fades = find_longshot_fades()
    if not fades:
        print("No longshot-fade opportunities right now.")
        print("  (No open exact-score/spread markets in the fade price band.)")
        return

    print(f"Found {len(fades)} longshot fade(s). Bidding NO below ask, "
          f"sized to real book depth:\n")
    print(f"{'tier':11} {'stake':>6} {'want':>6} {'fillable':>9} {'bid':>6} "
          f"{'estWin':>7} {'hrs':>5}  market")
    print("-" * 104)

    placed = 0
    skipped_nofill = 0
    for f in fades:
        print(f"{f.tier:11} ${f.size_usd:>5.2f} ${f.desired_usd:>5.2f} "
              f"${f.fillable_usd:>8.2f} {f.bid_price:>6.3f} "
              f"{f.est_win_prob:>7.2f} {f.market.hours_to_resolution:>4.1f}h  "
              f"{f.market.question[:38]}")
        if not trade:
            continue
        if store.already_open(f.market.condition_id):
            print("     -> skipped (already open)")
            continue
        if store.open_position_count() >= 50:
            print("     -> skipped (position cap)")
            continue

        # HONEST FILL MODEL: a limit order below the ask may not fill. In paper
        # mode we simulate this — only count the trade as filled with prob
        # f.fill_prob. Use a deterministic hash so runs are reproducible and we
        # don't depend on Math.random/time (keeps behavior stable).
        fill_roll = (hash(f.market.condition_id) % 1000) / 1000.0
        if fill_roll > f.fill_prob:
            print(f"     -> limit NOT filled at {f.bid_price:.3f} "
                  f"(would retry next scan or pay ask)")
            skipped_nofill += 1
            continue

        sig = Signal(
            market=f.market, side="NO",
            fair_prob=f.est_win_prob, market_prob=f.bid_price,
            edge=round(f.est_win_prob - f.bid_price, 4), size_usd=f.size_usd,
            reason=f.reason, estimator="longshot-fade",
        )
        result = executor.execute(sig)
        placed += 1

    if trade:
        staked = placed * config.LONGSHOT_STAKE_USD
        print(f"\nFilled {placed} NO bets (~${staked:.2f} staked); "
              f"{skipped_nofill} limit orders didn't fill at the mid price.")
        print("Bidding below ask = better entry when it fills = more profit per win.")
        print("Run 'resolve' then 'report' after the games settle.")


# ---------------------------------------------------------------------------
# LAGWATCH — resolution-lag arbitrage
# ---------------------------------------------------------------------------

def cmd_lagwatch(trade: bool = True):
    """
    Find sports games that just finished (known winner via ESPN) where the
    Polymarket market still prices the winner below 1.00, and paper-buy them.
    """
    from polybot.lagwatch import find_lag_opportunities
    from polybot.market_data import Market, limit_bid_price
    from polybot.strategy import Signal

    _banner()
    store.init_db()
    executor = Executor() if trade else None

    print("Checking ESPN for finished games vs open Polymarket markets...\n")
    opps = find_lag_opportunities(min_profit=0.02, max_price=0.98)

    if not opps:
        print("No resolution-lag opportunities right now.")
        print("  (Markets caught up, games already resolved on-chain, or no")
        print("   finished game matches a currently-open single-match market.)")
        return

    print(f"Found {len(opps)} lag opportunit(ies):\n")
    placed = 0
    nofill = 0
    for o in opps:
        # MID-PRICE BIDDING: the side already won, so bid below the ask to pay
        # less and capture more profit. We bid a bit more aggressively here than
        # for longshots (aggression 0.6) because we want this near-certain win
        # to actually fill, not miss over a fraction of a cent.
        quote = limit_bid_price(o.token_id, aggression=0.6)
        if quote:
            bid_price = quote["price"]
            ask_price = quote["ask"]
            fill_prob = quote["fill_prob_estimate"]
        else:
            bid_price = o.market_price
            ask_price = o.market_price
            fill_prob = 1.0
        profit = round(1.0 - bid_price, 4)

        print(f"  BUY {o.side} bid@{bid_price:.3f} (ask {ask_price:.3f}) "
              f"-> profit +{profit*100:.1f}% if it pays $1   liq=${o.liquidity:,.0f}")
        print(f"    {o.game.winner} won {o.game.home_score}-{o.game.away_score}  "
              f"| {o.question[:55]}")

        if not trade:
            continue
        if store.already_open(o.condition_id):
            print("    -> skipped (already open)\n")
            continue

        # Honest fill model for the limit order (deterministic, reproducible).
        fill_roll = (hash(o.condition_id) % 1000) / 1000.0
        if fill_roll > fill_prob:
            print(f"    -> limit NOT filled at {bid_price:.3f} (retry next scan)\n")
            nofill += 1
            continue

        size = config.STRATEGY.max_position_usd
        mkt = Market(
            condition_id=o.condition_id, question=o.question,
            token_id_yes=o.token_id if o.side == "YES" else "",
            token_id_no=o.token_id if o.side == "NO" else "",
            price_yes=bid_price if o.side == "YES" else round(1 - bid_price, 4),
            liquidity=o.liquidity, volume=0, volume_24h=0, spread=0,
            end_date="", hours_to_resolution=0,
            category="lag-" + o.game.league, event_title=o.game.winner,
        )
        sig = Signal(
            market=mkt, side=o.side,
            fair_prob=0.99, market_prob=bid_price,
            edge=profit, size_usd=size,
            reason=f"lag-arb: {o.game.winner} already won {o.game.home_score}-{o.game.away_score}",
            estimator="lagwatch",
        )
        result = executor.execute(sig)
        placed += 1
        print(f"    -> {result['mode']} {result['status']} @ {result.get('price')}\n")

    if trade:
        print(f"Filled {placed} lag bets; {nofill} limit orders didn't fill.")
    print("Run 'python run.py resolve' after the oracle settles these markets.")


# ---------------------------------------------------------------------------
# RESOLVE — settle open positions
# ---------------------------------------------------------------------------

def cmd_resolve():
    store.init_db()
    positions = store.open_positions()
    if not positions:
        print("No open positions to resolve.")
        return

    print(f"Checking {len(positions)} open positions for resolution...\n")
    settled = 0
    for trade_id, cond_id, question, side, size_usd, mkt_prob, shares in positions:
        winner = fetch_resolution(cond_id)
        if winner is None:
            print(f"  [pending] {question[:60]}")
            continue
        won = (winner == side)
        pnl = store.settle_position(trade_id, won, size_usd, shares)
        tag = "WON " if won else "LOST"
        print(f"  [{tag}]   {question[:48]}  (bet {side}, {winner} won)  pnl={pnl:+.2f}")
        settled += 1

    print(f"\nSettled {settled} position(s).")
    today_pnl = store.today_pnl()
    print(f"Today's realised P&L: ${today_pnl:+.2f}   target: ${config.DAILY_TARGET_USD:.0f}/day")


# ---------------------------------------------------------------------------
# HISTORY
# ---------------------------------------------------------------------------

def cmd_history():
    store.init_db()
    rows = store.recent_trades(30)
    if not rows:
        print("No trades recorded yet.")
        return
    print(f"{'time':20} {'mode':5} {'side':4} {'$':>6} {'price':>6} {'edge':>7} "
          f"{'status':7} {'pnl':>8} {'cat':>9} {'hrs':>5}  question")
    print("-" * 120)
    for ts, mode, side, size, price, edge, status, pnl, cat, hrs, q in rows:
        pnl_str = f"{pnl:>+8.2f}" if pnl is not None else f"{'':>8}"
        hrs_str = f"{hrs:5.1f}" if hrs else "    ?"
        cat_str = (cat or "other")[:9]
        print(f"{ts[:19]:20} {mode:5} {side:4} {size:>6.2f} {price:>6.2f} "
              f"{edge:>+7.3f} {status:7} {pnl_str} {cat_str:>9} {hrs_str}  {q[:40]}")


# ---------------------------------------------------------------------------
# REPORT — performance summary + category breakdown
# ---------------------------------------------------------------------------

def cmd_report():
    store.init_db()
    s = store.performance_summary()
    print("=" * 60)
    print("  PERFORMANCE REPORT  (resolved trades only)")
    print("=" * 60)
    print(f"  total trades placed : {s['total_trades']}")
    print(f"  still open          : {s['open']}")
    print(f"  resolved            : {s['resolved']}  (won {s['won']}, lost {s['lost']})")
    if s["resolved"] == 0:
        print("\n  No trades have resolved yet -- place trades, wait, then 'resolve'.")
        return
    print(f"  win rate            : {s['win_rate']*100:.1f}%")
    print(f"  total staked        : ${s['staked_usd']:.2f}")
    print(f"  net P&L             : ${s['pnl_usd']:+.2f}")
    print(f"  ROI on staked       : {s['roi']*100:+.1f}%")

    # Category breakdown
    cat_rows = store.category_summary()
    if cat_rows:
        print(f"\n  {'category':12} {'n':>4} {'won':>4} {'lost':>4} {'win%':>6} {'pnl':>8} {'staked':>8}")
        print(f"  {'-'*12} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*8} {'-'*8}")
        for cat, n, won, lost, pnl, staked in cat_rows:
            wr = won/n*100 if n else 0
            print(f"  {(cat or 'other'):12} {n:>4} {won:>4} {lost:>4} {wr:>5.0f}% "
                  f"${pnl:>+7.2f} ${staked:>7.2f}")

    today_pnl = store.today_pnl()
    print(f"\n  Today's P&L: ${today_pnl:+.2f}   daily target: ${config.DAILY_TARGET_USD:.0f}")
    print("-" * 60)
    if s["resolved"] < 20:
        print("  Need 20+ resolved trades for statistically meaningful results.")
    elif s["roi"] > 0:
        print("  Positive ROI. Keep building sample size (aim for 50-100 resolved).")
    else:
        print("  Negative ROI. Do NOT go live. Tune strategy or keep as research.")


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

def cmd_status():
    _banner()
    cfg = config.STRATEGY
    print(f"  min_edge            : {cfg.min_edge}")
    print(f"  min_confidence      : {cfg.min_confidence}")
    print(f"  kelly_fraction      : {cfg.kelly_fraction}")
    print(f"  bankroll_usd        : {cfg.bankroll_usd}")
    print(f"  max_position_usd    : {cfg.max_position_usd}")
    print(f"  max_open_positions  : {cfg.max_open_positions}")
    print(f"  min_liquidity_usd   : {cfg.min_liquidity_usd}")
    print(f"  scan_interval       : {config.SCAN_INTERVAL}s")
    wallet = "set" if config.POLYGON_WALLET_PRIVATE_KEY else "MISSING"
    print(f"  wallet key          : {wallet}")
    print(f"  anthropic key       : {'set' if config.ANTHROPIC_API_KEY else 'MISSING'}")
    print(f"  openai key          : {'set' if config.OPENAI_API_KEY else 'MISSING'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMANDS = {
    "scout":   cmd_scout,
    "export":  cmd_export,
    "short":   lambda: cmd_short_trade(scout_only=False),
    "loop":    cmd_loop,
    "scan":    lambda: _cmd_scan_all(trade=False),
    "trade":   lambda: _cmd_scan_all(trade=True),
    "lagwatch": cmd_lagwatch,
    "longshot": cmd_longshot,
    "resolve": cmd_resolve,
    "history": cmd_history,
    "report":  cmd_report,
    "status":  cmd_status,
}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = COMMANDS.get(cmd)
    if not fn:
        print(__doc__)
        print(f"Unknown command: {cmd}")
        sys.exit(1)
    fn()


if __name__ == "__main__":
    main()
