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
        for col, typedef in [("category","TEXT"), ("estimator","TEXT"), ("hours_to_res","REAL")]:
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
    with _conn() as c:
        c.execute(
            """INSERT INTO trades
               (ts, mode, condition_id, question, side, fair_prob,
                market_prob, edge, size_usd, shares, status, exec_status,
                order_id, resolved_ts, pnl_usd, category, estimator, hours_to_res)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                category, estimator, hrs,
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


def recent_trades(limit: int = 30):
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, mode, side, size_usd, market_prob, edge, status, pnl_usd, "
            "category, hours_to_res, question "
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
