"""
Per-strategy bankrolls for the multi-strategy tournament.

Kept SEPARATE from the original bankroll.py (which holds the real existing ledger
as the 'conservative_fade' book) so the proven single-book accounting is never
touched. The conservative strategy reads/writes the original bankroll; the other
4 strategies get their own fresh books in the strategy_books table here.

One table, keyed by strategy name. Every movement is logged (same discipline as
the main bankroll). Trades are tagged with `strategy` (a column added to trades)
so P&L can be attributed per book.
"""
import datetime
import sqlite3
from . import config
from .strategies import PER_STRATEGY_DEPOSIT, DEFAULT_STRATEGY


def _conn():
    return sqlite3.connect(config.DB_PATH)


def init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS strategy_books (
                strategy TEXT PRIMARY KEY,
                balance REAL,
                initial_deposit REAL,
                created_ts TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS strategy_book_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, strategy TEXT, kind TEXT, amount REAL,
                balance_after REAL, note TEXT
            )""")
        # tag column on trades so each bet is attributable to its strategy
        try:
            c.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
        except Exception:
            pass


def _seed(strategy):
    """Create a fresh book for a strategy if it doesn't exist."""
    with _conn() as c:
        row = c.execute("SELECT 1 FROM strategy_books WHERE strategy=?",
                        (strategy,)).fetchone()
        if row is None:
            ts = datetime.datetime.utcnow().isoformat()
            c.execute("INSERT INTO strategy_books VALUES (?,?,?,?)",
                      (strategy, PER_STRATEGY_DEPOSIT, PER_STRATEGY_DEPOSIT, ts))
            c.execute("INSERT INTO strategy_book_log (ts,strategy,kind,amount,balance_after,note) "
                      "VALUES (?,?,?,?,?,?)",
                      (ts, strategy, "deposit", PER_STRATEGY_DEPOSIT,
                       PER_STRATEGY_DEPOSIT, "initial deposit"))


def balance(strategy) -> float:
    """Current cash for a strategy. conservative_fade defers to the main bankroll."""
    if strategy == DEFAULT_STRATEGY:
        from .bankroll import balance as _b
        return _b()
    init(); _seed(strategy)
    with _conn() as c:
        return c.execute("SELECT balance FROM strategy_books WHERE strategy=?",
                         (strategy,)).fetchone()[0]


def can_afford(strategy, stake) -> bool:
    return balance(strategy) >= stake


def _move(strategy, kind, amount, note):
    init(); _seed(strategy)
    with _conn() as c:
        bal = c.execute("SELECT balance FROM strategy_books WHERE strategy=?",
                        (strategy,)).fetchone()[0]
        new = round(bal + amount, 4)
        ts = datetime.datetime.utcnow().isoformat()
        c.execute("UPDATE strategy_books SET balance=? WHERE strategy=?", (new, strategy))
        c.execute("INSERT INTO strategy_book_log (ts,strategy,kind,amount,balance_after,note) "
                  "VALUES (?,?,?,?,?,?)", (ts, strategy, kind, round(amount, 4), new, note))
    return new


def deduct_stake(strategy, stake, note=""):
    if strategy == DEFAULT_STRATEGY:
        from .bankroll import deduct_stake as _d
        return _d(stake, note)
    return _move(strategy, "stake", -abs(stake), note or "bet placed")


def credit_payout(strategy, payout, note=""):
    if strategy == DEFAULT_STRATEGY:
        from .bankroll import credit_payout as _c
        return _c(payout, note)
    if payout <= 0:
        return balance(strategy)
    return _move(strategy, "payout", abs(payout), note or "bet settled")


def summary(strategy) -> dict:
    """Per-strategy book summary (cash + open exposure + equity + P&L)."""
    if strategy == DEFAULT_STRATEGY:
        from .bankroll import summary as _s
        d = dict(_s())
        d["strategy"] = strategy        # main bankroll dict lacks the tag
        return d
    init(); _seed(strategy)
    with _conn() as c:
        bal, dep = c.execute(
            "SELECT balance, initial_deposit FROM strategy_books WHERE strategy=?",
            (strategy,)).fetchone()
        try:
            open_exp = c.execute(
                "SELECT COALESCE(SUM(size_usd),0) FROM trades "
                "WHERE status='OPEN' AND strategy=?", (strategy,)).fetchone()[0] or 0.0
        except Exception:
            open_exp = 0.0
    open_exp = round(open_exp, 2)
    equity = round(bal + open_exp, 2)
    profit = round(equity - dep, 2)
    return {"strategy": strategy, "balance": round(bal, 2), "initial_deposit": round(dep, 2),
            "open_exposure": open_exp, "total_equity": equity, "profit": profit,
            "return_pct": round((profit / dep * 100) if dep else 0, 1)}


def all_summaries() -> list:
    from .strategies import names
    return [summary(n) for n in names()]
