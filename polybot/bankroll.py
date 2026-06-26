"""
Compounding bankroll — a single $200 deposit that grows by reinvesting winnings.

The user's model: deposit $200 ONCE on day 1. Every bet is sized from the CURRENT
balance (not a fresh $200/day). When a bet is placed, its stake is deducted from
the balance; when it resolves, the payout (stake + profit on a win, or 0 on a loss)
is credited back. So the balance compounds over the month, and total invested stays
$200.

State lives in two SQLite tables (same DB as trades):
  bankroll      — current balance + the initial deposit
  bankroll_log  — every movement (deposit / stake / payout) for full history

This gives a proper equity curve: you can see exactly how $200 evolved.
"""
import datetime
import sqlite3
from . import config


INITIAL_DEPOSIT = float(__import__("os").getenv("INITIAL_DEPOSIT", "500"))


def _conn():
    return sqlite3.connect(config.DB_PATH)


def init_bankroll():
    """Create tables and seed the initial deposit if not already present."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                balance REAL,
                initial_deposit REAL,
                created_ts TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS bankroll_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                kind TEXT,          -- deposit | stake | payout
                amount REAL,        -- signed: deposit/payout positive, stake negative
                balance_after REAL,
                note TEXT
            )
        """)
        row = c.execute("SELECT balance FROM bankroll WHERE id=1").fetchone()
        if row is None:
            ts = datetime.datetime.utcnow().isoformat()
            # Account for any bets already placed before the bankroll existed:
            # subtract their stakes from the opening balance so it reflects reality.
            try:
                already = c.execute(
                    "SELECT COALESCE(SUM(size_usd),0) FROM trades WHERE status='OPEN'"
                ).fetchone()[0] or 0.0
            except Exception:
                already = 0.0
            opening = round(INITIAL_DEPOSIT - already, 4)
            c.execute("INSERT INTO bankroll (id, balance, initial_deposit, created_ts) "
                      "VALUES (1, ?, ?, ?)", (opening, INITIAL_DEPOSIT, ts))
            c.execute("INSERT INTO bankroll_log (ts, kind, amount, balance_after, note) "
                      "VALUES (?,?,?,?,?)",
                      (ts, "deposit", INITIAL_DEPOSIT, INITIAL_DEPOSIT,
                       f"initial deposit ${INITIAL_DEPOSIT:.2f}"))
            if already > 0:
                n_open = c.execute(
                    "SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
                c.execute("INSERT INTO bankroll_log (ts, kind, amount, balance_after, note) "
                          "VALUES (?,?,?,?,?)",
                          (ts, "stake", -already, opening,
                           f"{n_open} pre-existing open bets"))


def balance() -> float:
    init_bankroll()
    with _conn() as c:
        return c.execute("SELECT balance FROM bankroll WHERE id=1").fetchone()[0]


def _move(kind: str, amount: float, note: str) -> float:
    """Apply a signed movement to the balance and log it. Returns new balance."""
    with _conn() as c:
        bal = c.execute("SELECT balance FROM bankroll WHERE id=1").fetchone()[0]
        new = round(bal + amount, 4)
        ts = datetime.datetime.utcnow().isoformat()
        c.execute("UPDATE bankroll SET balance=? WHERE id=1", (new,))
        c.execute("INSERT INTO bankroll_log (ts, kind, amount, balance_after, note) "
                  "VALUES (?,?,?,?,?)", (ts, kind, round(amount, 4), new, note))
    return new


def can_afford(stake: float) -> bool:
    return balance() >= stake


def peak_equity() -> float:
    """Highest equity reached so far, from the real daily curve + current equity.
    Used to ratchet the drawdown halt to the PEAK (protecting compounded gains),
    not just the initial deposit."""
    cur = summary()["total_equity"]
    peak = cur
    try:
        from . import store
        for row in store.real_daily_equity(365):
            peak = max(peak, row[5])
    except Exception:
        pass
    return max(peak, summary()["initial_deposit"])


def drawdown_halted() -> bool:
    """
    Ruin guard: True if total equity has fallen below DRAWDOWN_HALT_FRAC of the
    PEAK equity reached so far (ratcheted), not just the initial deposit — so a
    big run-up that then craters is also protected. When True, callers must stop
    opening NEW positions (existing ones still settle normally).
    """
    s = summary()
    floor = config.DRAWDOWN_HALT_FRAC * peak_equity()
    return s["total_equity"] < floor


def exposure_ok(new_stake: float) -> bool:
    """Aggregate ceiling: total open exposure (current + this new bet) must not
    exceed AGG_EXPOSURE_FRAC of total equity, capping correlated tail clusters."""
    s = summary()
    cap = config.AGG_EXPOSURE_FRAC * s["total_equity"]
    return (s["open_exposure"] + new_stake) <= cap


def deduct_stake(stake: float, note: str = "") -> float:
    """Remove a stake from the balance when a bet is placed."""
    init_bankroll()
    return _move("stake", -abs(stake), note or "bet placed")


def credit_payout(payout: float, note: str = "") -> float:
    """Add a payout back to the balance when a bet resolves (stake+profit, or 0)."""
    init_bankroll()
    if payout <= 0:
        return balance()
    return _move("payout", abs(payout), note or "bet settled")


def deposit_date() -> str:
    """The date the initial $500 was deposited (YYYY-MM-DD)."""
    init_bankroll()
    with _conn() as c:
        row = c.execute("SELECT created_ts FROM bankroll WHERE id=1").fetchone()
    return (row[0] or "")[:10] if row else ""


def summary() -> dict:
    """Balance, profit vs initial deposit, and total return %.

    Open exposure is the EXACT sum of stakes on currently-OPEN trades — taken
    straight from the trades table. Total equity = free cash + that open stake
    (open bets held at COST, never marked up — honest). All from real paper trades.

    HONESTY: the HEADLINE profit/return is the REALIZED P&L only (settled WON/LOST
    bets) — money that has actually been won or lost. The stake sitting in OPEN bets
    is NOT profit (it's undecided), so it must not inflate the headline. Previously
    `profit = equity - deposit` counted open stakes as gains (e.g. reported +10.3%
    when realized was +5.3%). We now report realized_profit/return_pct as the
    trustworthy number, and keep total_equity (incl. open-at-cost) separately.
    """
    init_bankroll()
    with _conn() as c:
        bal, dep = c.execute(
            "SELECT balance, initial_deposit FROM bankroll WHERE id=1").fetchone()
        # True money tied up right now = stakes on OPEN positions.
        try:
            open_exposure = c.execute(
                "SELECT COALESCE(SUM(size_usd),0) FROM trades WHERE status='OPEN'"
            ).fetchone()[0] or 0.0
        except Exception:
            open_exposure = 0.0
        # REALIZED P&L = sum of pnl on SETTLED real trades (the honest headline).
        try:
            realized = c.execute(
                "SELECT COALESCE(SUM(pnl_usd),0) FROM trades "
                "WHERE status IN ('WON','LOST') AND condition_id NOT LIKE 'bf-%' "
                "AND COALESCE(exec_status,'') != 'backfill'"
            ).fetchone()[0] or 0.0
        except Exception:
            realized = 0.0
    open_exposure = round(open_exposure, 2)
    equity = round(bal + open_exposure, 2)
    realized = round(realized, 2)
    return {
        "balance": round(bal, 2),
        "initial_deposit": round(dep, 2),
        "open_exposure": open_exposure,
        "total_equity": equity,            # cash + open bets at cost
        "realized_profit": realized,       # HEADLINE: actually won/lost (settled)
        "return_pct": round((realized / dep * 100) if dep else 0, 1),  # realized %
        # `profit` kept as an alias of realized_profit for back-compat with callers,
        # so nothing reports the open-stake-inflated figure as profit anymore.
        "profit": realized,
    }


def history(limit: int = 100) -> list:
    init_bankroll()
    with _conn() as c:
        return c.execute(
            "SELECT ts, kind, amount, balance_after, note FROM bankroll_log "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def equity_points(limit: int = 500) -> list:
    """Chronological (ts, balance_after) for plotting an equity curve."""
    init_bankroll()
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, balance_after FROM bankroll_log ORDER BY id ASC LIMIT ?",
            (limit,)).fetchall()
    return rows
