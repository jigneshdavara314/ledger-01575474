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
    """Record a freshly-placed trade as an OPEN position."""
    shares = round(signal.size_usd / signal.market_prob, 4) if signal.market_prob else 0.0
    estimator = getattr(signal, "estimator", "heuristic")
    category  = getattr(signal.market, "category", "other")
    hrs       = getattr(signal.market, "hours_to_resolution", None)
    slug      = getattr(signal.market, "event_slug", "") or ""
    with _conn() as c:
        c.execute(
            """INSERT INTO trades
               (ts, mode, condition_id, question, side, fair_prob,
                market_prob, edge, size_usd, shares, status, exec_status,
                order_id, resolved_ts, pnl_usd, category, estimator, hours_to_res,
                event_slug)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.datetime.utcnow().isoformat(),
                result.get("mode"),
                signal.market.condition_id,
                signal.market.question,
                signal.side,
                signal.fair_prob,
                signal.market_prob,
                signal.edge,
                signal.size_usd,
                shares,
                STATUS_OPEN,
                result.get("status"),
                result.get("order_id"),
                None, None,
                category, estimator, hrs, slug,
            ),
        )


def already_open(condition_id: str) -> bool:
    """True only if we currently hold an UNRESOLVED position in this market."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM trades WHERE condition_id = ? AND status = ?",
            (condition_id, STATUS_OPEN),
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


def open_position_count() -> int:
    """Count only currently-open (unresolved) positions."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status = ?", (STATUS_OPEN,)
        ).fetchone()
    return row[0]


def open_positions():
    """Return (id, condition_id, question, side, size_usd, market_prob, shares)
    for every open position, so the resolver can settle them."""
    with _conn() as c:
        return c.execute(
            "SELECT id, condition_id, question, side, size_usd, market_prob, shares "
            "FROM trades WHERE status = ?",
            (STATUS_OPEN,),
        ).fetchall()


def settle_position(trade_id: int, won: bool, size_usd: float, shares: float):
    """Mark a position WON or LOST and write its realised P&L."""
    if won:
        pnl = round(shares * 1.0 - size_usd, 4)   # each winning share pays $1
        status = STATUS_WON
    else:
        pnl = round(-size_usd, 4)                  # losing side pays nothing
        status = STATUS_LOST
    with _conn() as c:
        c.execute(
            "UPDATE trades SET status=?, pnl_usd=?, resolved_ts=? WHERE id=?",
            (status, pnl, datetime.datetime.utcnow().isoformat(), trade_id),
        )
    return pnl


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
               WHERE status IN (?,?)
               GROUP BY category
               ORDER BY pnl DESC""",
            (STATUS_WON, STATUS_LOST, STATUS_WON, STATUS_LOST),
        ).fetchall()
    return rows


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
    Recompute TODAY's row from actual resolved-today trades and CHAIN it onto the
    running equity curve: balance_after = previous day's balance + today's profit.

    This preserves the historical backfill (it does NOT reset to the live bankroll
    balance, which would clobber the curve). Idempotent: re-running today just
    recomputes today's profit and re-chains from the prior day.
    """
    import datetime
    init_daily_equity()
    day = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    with _conn() as c:
        won, lost, profit = c.execute(
            "SELECT COALESCE(SUM(status='WON'),0), COALESCE(SUM(status='LOST'),0), "
            "COALESCE(SUM(pnl_usd),0) FROM trades "
            "WHERE status IN ('WON','LOST') AND resolved_ts LIKE ?",
            (day + "%",),
        ).fetchone()
        # previous day's running balance (the most recent row BEFORE today)
        prev = c.execute(
            "SELECT balance_after FROM daily_equity WHERE day < ? "
            "ORDER BY day DESC LIMIT 1", (day,)).fetchone()
        prev_bal = prev[0] if prev else 500.0
        new_bal = round(prev_bal + (profit or 0), 2)
        c.execute("""
            INSERT INTO daily_equity (day, bets_settled, won, lost, day_profit, balance_after)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET
              bets_settled=excluded.bets_settled, won=excluded.won,
              lost=excluded.lost, day_profit=excluded.day_profit,
              balance_after=excluded.balance_after
        """, (day, (won or 0) + (lost or 0), won or 0, lost or 0,
              round(profit or 0, 2), new_bal))


def real_daily_equity(limit: int = 60, start_balance: float = 500.0):
    """
    The REAL day-by-day equity curve, built ONLY from actual placed-and-settled
    trades — no simulated backfill base. Starts at the initial deposit and chains
    each day's realized P&L. This is what the dashboard's headline daily history
    shows; the simulated 30-day replay (daily_equity) is a SEPARATE, labelled curve.

    Returns rows oldest-first... actually newest-first to match daily_equity():
    (day, bets_settled, won, lost, day_profit, balance_after).
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT substr(resolved_ts,1,10) AS day, "
            "       SUM(status='WON') AS won, SUM(status='LOST') AS lost, "
            "       COALESCE(SUM(pnl_usd),0) AS profit "
            "FROM trades WHERE status IN ('WON','LOST') AND resolved_ts IS NOT NULL "
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
        # event_slug may not exist on very old DBs; COALESCE guards it
        rows = c.execute(
            "SELECT ts, mode, side, size_usd, market_prob, edge, status, pnl_usd, "
            "category, hours_to_res, question, COALESCE(event_slug,'') "
            "FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows


def performance_summary() -> dict:
    """Aggregate win rate, P&L and ROI over all RESOLVED positions."""
    with _conn() as c:
        total, open_n = c.execute(
            "SELECT COUNT(*), SUM(status=?) FROM trades", (STATUS_OPEN,)
        ).fetchone()
        won = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status=?", (STATUS_WON,)
        ).fetchone()[0]
        lost = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status=?", (STATUS_LOST,)
        ).fetchone()[0]
        staked = c.execute(
            "SELECT COALESCE(SUM(size_usd),0) FROM trades WHERE status IN (?,?)",
            (STATUS_WON, STATUS_LOST),
        ).fetchone()[0]
        pnl = c.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) FROM trades WHERE status IN (?,?)",
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
