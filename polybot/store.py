"""
SQLite trade log + P&L ledger.

Every signal the bot acts on is recorded as an OPEN position. Later,
`resolve_open_positions()` checks Polymarket for the real outcome of each
market and settles the position — marking it WON or LOST and computing the
actual profit/loss. This is what lets you measure the bot's true win rate
and whether it has any edge, before risking real money.

P&L model (paper):
  - You stake `size_usd` dollars buying the chosen side at `market_prob`.
  - That buys  shares = size_usd / market_prob.
  - If that side WINS, each share pays $1  -> payout = shares * 1.0
                                            -> profit = payout - size_usd
  - If it LOSES, payout = 0                 -> profit = -size_usd
"""
import sqlite3
import datetime
from . import config
from .strategy import Signal


# Position lifecycle: OPEN -> (WON | LOST) once the market resolves.
STATUS_OPEN = "OPEN"
STATUS_WON = "WON"
STATUS_LOST = "LOST"
STATUS_VOID = "VOID"   # market cancelled/refunded (50-50): stake returned, pnl 0

# Honesty guard: headline/performance views must NEVER count simulated/backtest
# rows. Backfill rows are tagged condition_id LIKE 'bf-%' (and exec_status
# 'backfill'). This SQL fragment excludes them everywhere a real-money-style
# figure is shown — belt-and-suspenders so the public dashboard can never again
# display backtest numbers as if they were real bets, even if such rows reappear.
_REAL_ONLY = ("condition_id NOT LIKE 'bf-%' "
              "AND COALESCE(exec_status,'') != 'backfill'")


def _conn():
    return sqlite3.connect(config.DB_PATH)


def init_db():
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                mode TEXT,
                condition_id TEXT,
                question TEXT,
                side TEXT,
                fair_prob REAL,
                market_prob REAL,
                edge REAL,
                size_usd REAL,
                shares REAL,
                status TEXT,            -- OPEN | WON | LOST
                exec_status TEXT,       -- simulated / submitted / etc.
                order_id TEXT,
                resolved_ts TEXT,
                pnl_usd REAL,
                category TEXT,          -- soccer | esports | crypto | ...
                estimator TEXT,         -- heuristic | claude | openai
                hours_to_res REAL       -- hours to resolution at trade time
            )
            """
        )
        # Add columns to existing databases that predate this schema
        for col, typedef in [("category","TEXT"), ("estimator","TEXT"),
                              ("hours_to_res","REAL"), ("event_slug","TEXT")]:
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists


def record_trade(signal: Signal, result: dict):
    """Record a freshly-placed trade as an OPEN position.

    Use the ACTUAL fill price from the executor result (which includes paper
    slippage / the real LIVE matched price), not the quoted bid — so recorded
    shares and downstream P&L reflect realistic execution, not the best case.
    """
    fill_price = result.get("price") or signal.market_prob
    shares = round(signal.size_usd / fill_price, 4) if fill_price else 0.0
    estimator = getattr(signal, "estimator", "heuristic")
    category  = getattr(signal.market, "category", "other")
    hrs       = getattr(signal.market, "hours_to_resolution", None)
    slug      = getattr(signal.market, "event_slug", "") or ""
    with _conn() as c:
        # ensure the strategy tag column exists (multi-strategy tournament)
        try:
            c.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
        except Exception:
            pass
        strategy = result.get("strategy", "conservative_fade")
        c.execute(
            """INSERT INTO trades
               (ts, mode, condition_id, question, side, fair_prob,
                market_prob, edge, size_usd, shares, status, exec_status,
                order_id, resolved_ts, pnl_usd, category, estimator, hours_to_res,
                event_slug, strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.datetime.utcnow().isoformat(),
                result.get("mode"),
                signal.market.condition_id,
                signal.market.question,
                signal.side,
                signal.fair_prob,
                fill_price,            # actual fill (incl. paper slippage / real match)
                signal.edge,
                signal.size_usd,
                shares,
                STATUS_OPEN,
                result.get("status"),
                result.get("order_id"),
                None, None,
                category, estimator, hrs, slug, strategy,
            ),
        )


def already_open(condition_id: str, strategy: str = None) -> bool:
    """True if we currently hold an UNRESOLVED position in this market.

    strategy=None -> ANY book (the main-bankroll path: don't double-bet a market).
    strategy="x"  -> only THAT strategy's book. The tournament strategies each run
    on a SEPARATE bankroll and must bet independently, so they check their OWN
    open position, not the global one — otherwise once the main book bets a market,
    every tournament strategy is blocked from it (the bug that left the tournament
    books idle)."""
    with _conn() as c:
        if strategy is None:
            row = c.execute(
                "SELECT COUNT(*) FROM trades WHERE condition_id = ? AND status = ?",
                (condition_id, STATUS_OPEN),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) FROM trades WHERE condition_id = ? AND status = ? "
                "AND COALESCE(strategy,'') = ?",
                (condition_id, STATUS_OPEN, strategy),
            ).fetchone()
    return row[0] > 0


def staked_today() -> float:
    """Total stake placed on trades opened today (UTC) — for the daily-spend cap."""
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(size_usd),0) FROM trades WHERE ts LIKE ?",
            (today + "%",),
        ).fetchone()
    return round(row[0] or 0.0, 2)


def open_count_for_event(event_slug: str) -> int:
    """How many OPEN bets we already hold on a given event (correlation cap)."""
    if not event_slug:
        return 0
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND event_slug=?",
                (event_slug,),
            ).fetchone()
        except Exception:
            return 0
    return row[0]


def open_count_for_event_like(group_key: str) -> int:
    """Open bets matching an event group — by exact event_slug OR by the match
    name parsed from the question (case-insensitive prefix before the colon).
    Used so the per-event correlation cap still works when event_slug is empty."""
    if not group_key:
        return 0
    gk = group_key.lower()
    with _conn() as c:
        try:
            row = c.execute(
                "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND ("
                "  LOWER(COALESCE(event_slug,'')) = ? "
                "  OR LOWER(question) LIKE ? )",
                (gk, gk + ":%"),
            ).fetchone()
        except Exception:
            return 0
    return row[0]


def open_position_count() -> int:
    """Count only currently-open (unresolved) positions."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status = ?", (STATUS_OPEN,)
        ).fetchone()
    return row[0]


def open_positions():
    """Return (id, condition_id, question, side, size_usd, market_prob, shares,
    strategy) for every open position, so the resolver can settle each to the book
    that funded it. The strategy tag is REQUIRED for correct accounting: tournament
    trades fund from their own strategy_books, so their payout/refund must go back
    there — not the main bankroll (the bug that overstated main equity)."""
    with _conn() as c:
        return c.execute(
            "SELECT id, condition_id, question, side, size_usd, market_prob, shares, "
            "COALESCE(strategy,'') FROM trades WHERE status = ?",
            (STATUS_OPEN,),
        ).fetchall()


def settle_position(trade_id: int, won: bool, size_usd: float, shares: float):
    """Mark a position WON or LOST and write its realised P&L, net of friction.

    A round-trip fee/gas drag (config.PAPER_FEE_FRAC of stake) is deducted so the
    paper P&L is not free-money optimistic — the same friction a live position
    would bear. Applied to wins and losses alike."""
    fee = round(getattr(config, "PAPER_FEE_FRAC", 0.0) * size_usd, 4)
    if won:
        pnl = round(shares * 1.0 - size_usd - fee, 4)   # each winning share pays $1
        status = STATUS_WON
    else:
        pnl = round(-size_usd - fee, 4)                 # losing side pays nothing
        status = STATUS_LOST
    with _conn() as c:
        c.execute(
            "UPDATE trades SET status=?, pnl_usd=?, resolved_ts=? WHERE id=?",
            (status, pnl, datetime.datetime.utcnow().isoformat(), trade_id),
        )
    # Return the fee too so the caller can charge it to the bankroll cash, keeping
    # the bankroll balance and the trade-ledger P&L reconciled (no drift).
    return pnl, fee


def _is_main_book(strategy) -> bool:
    """The main bankroll owns trades with no strategy tag OR the default strategy
    (conservative_fade defers to the main book). Everything else is a tournament
    book that must be credited via strategy_bankroll — crediting the main bankroll
    a payout it never funded was the $27.59 equity-overstatement bug."""
    try:
        from .strategies import DEFAULT_STRATEGY
    except Exception:
        DEFAULT_STRATEGY = "conservative_fade"
    return (strategy or "") in ("", DEFAULT_STRATEGY)


def settle_and_credit(trade_id: int, won: bool, size_usd: float, shares: float,
                      strategy: str = None):
    """ATOMIC settle (trade status + P&L) + route the cash to the OWNING book.

    Trade-status/pnl write is atomic here. The cash movement goes to the book that
    FUNDED the stake: the main bankroll for main/default trades, else the trade's
    strategy_book (tournament trades fund from their own book at open, so their
    payout/fee must return there — not the main bankroll). Returns (pnl, fee).
    """
    fee = round(getattr(config, "PAPER_FEE_FRAC", 0.0) * size_usd, 4)
    payout = round(shares * 1.0, 4) if won else 0.0
    if won:
        pnl = round(shares * 1.0 - size_usd - fee, 4)
        status = STATUS_WON
    else:
        pnl = round(-size_usd - fee, 4)
        status = STATUS_LOST
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute("UPDATE trades SET status=?, pnl_usd=?, resolved_ts=? WHERE id=?",
                  (status, pnl, now, trade_id))
        if _is_main_book(strategy):
            def _move(kind, amount, note):
                bal = c.execute("SELECT balance FROM bankroll WHERE id=1").fetchone()[0]
                new = round(bal + amount, 4)
                c.execute("UPDATE bankroll SET balance=? WHERE id=1", (new,))
                c.execute("INSERT INTO bankroll_log (ts,kind,amount,balance_after,note) "
                          "VALUES (?,?,?,?,?)", (now, kind, round(amount, 4), new, note))
            if payout > 0:
                _move("payout", payout, f"WON trade #{trade_id}")
            if fee > 0:
                _move("stake", -fee, f"fee trade #{trade_id}")
    # tournament book moves happen OUTSIDE the trades transaction (separate table)
    if not _is_main_book(strategy):
        from . import strategy_bankroll as sb
        if payout > 0:
            sb.credit_payout(strategy, payout, note=f"WON trade #{trade_id}")
        if fee > 0:
            sb.deduct_stake(strategy, fee, note=f"fee trade #{trade_id}")
    return pnl, fee


def settle_void(trade_id: int, size_usd: float, strategy: str = None):
    """ATOMIC void settlement: refund the full stake to the OWNING book, book 0 P&L.
    Routes the refund to the strategy book for tournament trades (not the main
    bankroll). Returns the refunded amount."""
    now = datetime.datetime.utcnow().isoformat()
    refund = round(size_usd, 4)
    with _conn() as c:
        c.execute("UPDATE trades SET status=?, pnl_usd=?, resolved_ts=? WHERE id=?",
                  (STATUS_VOID, 0.0, now, trade_id))
        if _is_main_book(strategy):
            bal = c.execute("SELECT balance FROM bankroll WHERE id=1").fetchone()[0]
            new = round(bal + refund, 4)
            c.execute("UPDATE bankroll SET balance=? WHERE id=1", (new,))
            c.execute("INSERT INTO bankroll_log (ts,kind,amount,balance_after,note) "
                      "VALUES (?,?,?,?,?)", (now, "refund", refund, new,
                                            f"VOID trade #{trade_id} (market cancelled)"))
    if not _is_main_book(strategy):
        from . import strategy_bankroll as sb
        sb.credit_payout(strategy, refund, note=f"VOID refund trade #{trade_id}")
    return refund


def live_spend_today() -> float:
    """Total REAL USDC staked today (UTC) on LIVE-mode fills — used to enforce the
    LIVE_MAX_DAILY_USD safety cap. Counts only genuinely-submitted live orders
    (mode LIVE / exec_status submitted-like), not paper rows."""
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(size_usd),0) FROM trades "
            "WHERE ts LIKE ? AND mode='LIVE' "
            "AND COALESCE(exec_status,'') NOT IN ('simulated','backfill')",
            (today + "%",),
        ).fetchone()
    return round(row[0] or 0.0, 2)


def today_pnl() -> float:
    """Sum of realised P&L on trades resolved today (UTC)."""
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) FROM trades "
            "WHERE status IN (?,?) AND resolved_ts LIKE ?",
            (STATUS_WON, STATUS_LOST, today + "%"),
        ).fetchone()
    return round(row[0], 2)


def category_summary() -> list:
    """Win rate and P&L broken down by category (for resolved trades only)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT category,
                      COUNT(*) as n,
                      SUM(status=?) as won,
                      SUM(status=?) as lost,
                      COALESCE(SUM(pnl_usd),0) as pnl,
                      COALESCE(SUM(size_usd),0) as staked
               FROM trades
               WHERE status IN (?,?) AND """ + _REAL_ONLY + """
               GROUP BY category
               ORDER BY pnl DESC""",
            (STATUS_WON, STATUS_LOST, STATUS_WON, STATUS_LOST),
        ).fetchall()
    return rows


def edge_confidence() -> list:
    """Per-EDGE (family) live track record + readiness, for the confidence report.

    Groups RESOLVED real trades by their canonical family (taxonomy.family_of on
    the question — the same classification we bet by), and for each computes:
      n, won, lost, win_rate, the WILSON LOWER BOUND of the live win-rate, net
      P&L, ROI, and whether the edge has enough live evidence to size up.

    This MEASURES the proof accruing per edge (it does not fabricate it): an edge
    is 'ready' to graduate only once it has its own settled sample whose Wilson
    lower bound still clears breakeven — i.e. the LIVE results, not a backtest,
    robustly support a bigger stake. Returns a list of dicts sorted by readiness.
    """
    from .taxonomy import family_of
    from .stats import wilson_lower_rate
    from collections import defaultdict
    agg = defaultdict(lambda: {"n": 0, "won": 0, "pnl": 0.0, "staked": 0.0,
                               "be_sum": 0.0})
    with _conn() as c:
        rows = c.execute(
            "SELECT question, status, COALESCE(pnl_usd,0), COALESCE(size_usd,0), "
            "COALESCE(market_prob,0) FROM trades "
            "WHERE status IN (?,?) AND " + _REAL_ONLY,
            (STATUS_WON, STATUS_LOST),
        ).fetchall()
    for q, status, pnl, size, price in rows:
        fam = family_of(q or "")
        a = agg[fam]
        a["n"] += 1
        a["won"] += 1 if status == STATUS_WON else 0
        a["pnl"] += pnl
        a["staked"] += size
        a["be_sum"] += price          # breakeven = avg entry price (need win >= price)
    out = []
    for fam, a in agg.items():
        n, won = a["n"], a["won"]
        wr = won / n if n else 0.0
        wlb = wilson_lower_rate(wr, n) if n else 0.0
        breakeven = (a["be_sum"] / n) if n else 0.0
        roi = (a["pnl"] / a["staked"]) if a["staked"] else 0.0
        # READY: enough live settlements AND the Wilson lower bound of the live
        # win-rate still beats the price we paid (robustly +EV on its OWN record).
        ready = n >= 15 and wlb > breakeven
        out.append({"family": fam, "n": n, "won": won, "lost": n - won,
                    "win_rate": round(wr, 3), "wilson_lower": round(wlb, 3),
                    "breakeven": round(breakeven, 3), "pnl": round(a["pnl"], 2),
                    "roi": round(roi, 3), "ready": ready})
    out.sort(key=lambda d: (-d["ready"], -d["n"]))
    return out


def init_snapshots():
    """Table recording a daily snapshot of bids placed + market capacity."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_snapshots (
                day TEXT PRIMARY KEY,
                ts TEXT,
                bets_placed INTEGER,
                staked_usd REAL,
                markets_available INTEGER,
                total_fillable_usd REAL,
                confirmed_fillable_usd REAL,
                exploratory_fillable_usd REAL,
                balance_usd REAL,
                equity_usd REAL
            )
        """)


def save_daily_snapshot(bets_placed, staked_usd, capacity, balance, equity):
    """Upsert today's snapshot (accumulates bets across the day's runs)."""
    import datetime
    init_snapshots()
    day = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    ts = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        existing = c.execute(
            "SELECT bets_placed, staked_usd FROM daily_snapshots WHERE day=?",
            (day,)).fetchone()
        prev_bets = existing[0] if existing else 0
        prev_staked = existing[1] if existing else 0.0
        c.execute("""
            INSERT INTO daily_snapshots
              (day, ts, bets_placed, staked_usd, markets_available,
               total_fillable_usd, confirmed_fillable_usd, exploratory_fillable_usd,
               balance_usd, equity_usd)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET
              ts=excluded.ts,
              bets_placed=daily_snapshots.bets_placed + ?,
              staked_usd=daily_snapshots.staked_usd + ?,
              markets_available=excluded.markets_available,
              total_fillable_usd=excluded.total_fillable_usd,
              confirmed_fillable_usd=excluded.confirmed_fillable_usd,
              exploratory_fillable_usd=excluded.exploratory_fillable_usd,
              balance_usd=excluded.balance_usd,
              equity_usd=excluded.equity_usd
        """, (day, ts, bets_placed, staked_usd,
              capacity.get("markets", 0), capacity.get("total_fillable_usd", 0),
              capacity.get("confirmed_fillable_usd", 0),
              capacity.get("exploratory_fillable_usd", 0),
              balance, equity, bets_placed, staked_usd))


def daily_snapshots(limit: int = 30):
    init_snapshots()
    with _conn() as c:
        return c.execute(
            "SELECT day, bets_placed, staked_usd, markets_available, "
            "total_fillable_usd, confirmed_fillable_usd, exploratory_fillable_usd, "
            "equity_usd FROM daily_snapshots ORDER BY day DESC LIMIT ?",
            (limit,)).fetchall()


def init_daily_equity():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_equity (
                day TEXT PRIMARY KEY,
                bets_settled INTEGER,
                won INTEGER,
                lost INTEGER,
                day_profit REAL,
                balance_after REAL
            )
        """)


def record_daily_equity():
    """
    DEPRECATED / no-op for the real curve.

    This used to chain real daily P&L onto the SIMULATED 30-day backfill base
    stored in daily_equity, producing a self-inconsistent hybrid table (an audit
    flagged the 5x figure). The dashboard now builds the REAL curve on the fly
    from actual trades via real_daily_equity(), so we no longer write real
    results into the simulated table at all. The simulated backfill rows in
    daily_equity are left untouched for the clearly-labelled simulation chart.

    Kept as a no-op so existing callers (the resolve job) don't break.
    """
    return


def real_daily_equity(limit: int = 60, start_balance: float = None):
    """
    The REAL day-by-day equity curve, built ONLY from actual placed-and-settled
    trades — no simulated backfill base. Starts at the initial deposit and chains
    each day's realized P&L. This is what the dashboard's headline daily history
    shows; the simulated 30-day replay (daily_equity) is a SEPARATE, labelled curve.

    Returns rows oldest-first... actually newest-first to match daily_equity():
    (day, bets_settled, won, lost, day_profit, balance_after).
    """
    if start_balance is None:
        # Source from the real initial deposit, not a hardcoded constant.
        try:
            from .bankroll import summary as _bk_summary
            start_balance = _bk_summary()["initial_deposit"]
        except Exception:
            start_balance = 500.0
    with _conn() as c:
        rows = c.execute(
            "SELECT substr(resolved_ts,1,10) AS day, "
            "       SUM(status='WON') AS won, SUM(status='LOST') AS lost, "
            "       COALESCE(SUM(pnl_usd),0) AS profit "
            "FROM trades WHERE status IN ('WON','LOST') AND resolved_ts IS NOT NULL "
            f"      AND {_REAL_ONLY} "
            "GROUP BY day ORDER BY day ASC"
        ).fetchall()
    out = []
    bal = start_balance
    for day, won, lost, profit in rows:
        bal = round(bal + (profit or 0), 2)
        out.append((day, (won or 0) + (lost or 0), won or 0, lost or 0,
                    round(profit or 0, 2), bal))
    out.reverse()  # newest-first
    return out[:limit]


def daily_equity(limit: int = 60):
    init_daily_equity()
    with _conn() as c:
        return c.execute(
            "SELECT day, bets_settled, won, lost, day_profit, balance_after "
            "FROM daily_equity ORDER BY day DESC LIMIT ?", (limit,)).fetchall()


def recent_trades(limit: int = 30):
    with _conn() as c:
        # event_slug may not exist on very old DBs; COALESCE guards it.
        # Real bets only — never display simulated/backfill rows.
        rows = c.execute(
            "SELECT ts, mode, side, size_usd, market_prob, edge, status, pnl_usd, "
            "category, hours_to_res, question, COALESCE(event_slug,'') "
            "FROM trades WHERE " + _REAL_ONLY + " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows


def performance_summary() -> dict:
    """Aggregate win rate, P&L and ROI over all RESOLVED positions (REAL only —
    simulated/backfill rows are excluded so the headline never overstates)."""
    with _conn() as c:
        total, open_n = c.execute(
            "SELECT COUNT(*), SUM(status=?) FROM trades WHERE " + _REAL_ONLY,
            (STATUS_OPEN,)
        ).fetchone()
        won = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status=? AND " + _REAL_ONLY,
            (STATUS_WON,)
        ).fetchone()[0]
        lost = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status=? AND " + _REAL_ONLY,
            (STATUS_LOST,)
        ).fetchone()[0]
        staked = c.execute(
            "SELECT COALESCE(SUM(size_usd),0) FROM trades WHERE status IN (?,?) AND " + _REAL_ONLY,
            (STATUS_WON, STATUS_LOST),
        ).fetchone()[0]
        pnl = c.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) FROM trades WHERE status IN (?,?) AND " + _REAL_ONLY,
            (STATUS_WON, STATUS_LOST),
        ).fetchone()[0]

    resolved = won + lost
    win_rate = (won / resolved) if resolved else 0.0
    roi = (pnl / staked) if staked else 0.0
    return {
        "total_trades": total or 0,
        "open": open_n or 0,
        "resolved": resolved,
        "won": won,
        "lost": lost,
        "win_rate": win_rate,
        "staked_usd": round(staked, 2),
        "pnl_usd": round(pnl, 2),
        "roi": roi,
    }
