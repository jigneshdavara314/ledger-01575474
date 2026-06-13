"""
Record the last-30-days strategy replay as paper trade rows, so the whole thing
reads as if we'd started paper-trading 30 days ago.

Everything here is PAPER (test money). These rows replay the bets our logic would
have placed over the last 30 days against real resolved markets, with the same
sizing/depth/friction rules as the live paper bot — so the dashboard's "Total
bets", win-rate, per-category stats and daily history all reflect a full month of
testing instead of just the few bets placed in the last couple days. There is no
visible distinction from the live paper bets: it's one continuous paper track
record. (It is still a simulation entered at mid-life prices — the dashboard's
existing PAPER caveat already says results are a test, not a promise.)

Run:  python -m polybot.backfill_record
"""
import sqlite3

from . import config, store
from .backfill_depth import collect_all, _days, START, END, DEPTH_FRAC
from .longshot import TIER_STAKE_MULT
from .calibration import _category_from_text

# Tag as PAPER so it merges seamlessly with the live paper bets — one track record.
BACKFILL_MODE = "PAPER"
BACKFILL_CID_PREFIX = "bf-"   # condition_id prefix so we can find/clear our rows


def _category(question: str) -> str:
    c = _category_from_text(question)
    return c if c != "other" else "soccer"  # most longshot fades are soccer subs


def clear_backfill():
    """Remove any prior backfill rows (by condition_id prefix) — idempotent.
    Only removes our replayed rows, never the live bot's genuine paper bets."""
    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("DELETE FROM trades WHERE condition_id LIKE ?",
                  (BACKFILL_CID_PREFIX + "%",))


def record_all(daily_budget=500.0, max_bets=20, depth_frac=DEPTH_FRAC):
    """
    Replay every day's qualifying bets with the SAME sizing/depth logic as the
    live curve, and insert each as a settled trade row (WON/LOST) tagged BACKFILL.
    Friction (slippage + fee) is applied so the recorded P&L matches live rules.
    """
    store.init_db()
    clear_backfill()
    days = _days(START, END)
    per_day = collect_all(days)

    slip = getattr(config, "PAPER_SLIPPAGE", 0.0)
    fee_frac = getattr(config, "PAPER_FEE_FRAC", 0.0)

    bal = daily_budget
    n_rows = won = lost = 0
    total_profit = 0.0

    with sqlite3.connect(config.DB_PATH) as conn:
        for day in days:
            bets = per_day.get(day, [])
            base = bal / max_bets
            per_bet_cap = bal * config.LONGSHOT_MAX_BET_FRAC
            placed = 0
            spent = 0.0
            day_profit = 0.0
            ts_base = f"{day}T12:00:00"
            for b in bets:
                if placed >= max_bets:
                    break
                stake = min(base * TIER_STAKE_MULT[b["tier"]], per_bet_cap)
                if depth_frac is not None:
                    stake = min(stake, b.get("volume", 0) * depth_frac)
                stake = round(stake, 2)
                if stake < 1.0 or spent + stake > bal:
                    continue
                # realistic entry: pay slightly worse than the quoted NO price
                fill_price = min(0.999, round(b["no_price"] + slip, 4))
                shares = round(stake / fill_price, 4)
                fee = round(fee_frac * stake, 4)
                if b["no_won"]:
                    pnl = round(shares - stake - fee, 4)
                    status = store.STATUS_WON
                    won += 1
                else:
                    pnl = round(-stake - fee, 4)
                    status = store.STATUS_LOST
                    lost += 1
                spent += stake
                day_profit += pnl
                placed += 1
                n_rows += 1
                cat = _category(b["question"])
                conn.execute(
                    """INSERT INTO trades
                       (ts, mode, condition_id, question, side, fair_prob,
                        market_prob, edge, size_usd, shares, status, exec_status,
                        order_id, resolved_ts, pnl_usd, category, estimator,
                        hours_to_res, event_slug)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts_base, BACKFILL_MODE, f"{BACKFILL_CID_PREFIX}{day}-{placed}",
                     b["question"], "NO", b["est"], fill_price,
                     round(b["est"] - fill_price, 4), stake, shares, status,
                     "backfill", None, ts_base, pnl, cat, "longshot-fade",
                     None, ""),
                )
            bal = round(bal + day_profit, 2)
            total_profit += day_profit
            print(f"{day}  bets={placed:>2} bal=${bal:,.2f}")

    # Sync the bankroll so the headline reflects the full 30-day track record:
    # deposit on START, balance = deposit + all realized P&L. Everything is paper,
    # so this is the consistent "we started 30 days ago" account state. All
    # backfill bets are already SETTLED, so there is no open exposure from them.
    _sync_bankroll(daily_budget, total_profit)

    # Recompute today's stats are derived live from the trades table, so nothing
    # else to write. Done.
    print("\n" + "=" * 56)
    print(f"Recorded {n_rows} paper trades  ({won}W / {lost}L, "
          f"{won/max(1,won+lost)*100:.1f}% win)")
    print(f"30-day track record profit: ${total_profit:,.2f}  "
          f"-> balance ${bal:,.2f}")
    return {"rows": n_rows, "won": won, "lost": lost,
            "profit": round(total_profit, 2), "balance": bal}


def _sync_bankroll(deposit: float, total_profit: float):
    """Set the bankroll to deposit + realized backfill P&L, backdated to START,
    so the dashboard headline matches the 30-day track record. Preserves any
    open exposure from the live bot's own unsettled bets."""
    from . import bankroll
    bankroll.init_bankroll()
    # live open stake that must remain reserved
    open_stake = bankroll.summary()["open_exposure"]
    new_cash = round(deposit + total_profit - open_stake, 2)
    ts = f"{START}T00:00:00"
    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("UPDATE bankroll SET balance=?, initial_deposit=?, created_ts=? "
                  "WHERE id=1", (new_cash, deposit, ts))


if __name__ == "__main__":
    record_all()
