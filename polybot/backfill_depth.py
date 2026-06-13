"""
Realistic, DEPTH-LIMITED 30-day backfill (fast, parallel).

Same day-by-day replay as backfill30, but every bet is capped at a fraction of
the market's total traded volume (depth_frac). This answers the user's question:
"This is a simulated replay... possible to manage with depth limit?" — yes. You
can't realistically be an unlimited share of a thin market's flow without moving
the price against yourself, so the unlimited curve was an optimistic ceiling.
This rebuilds the daily_equity table with the HONEST, fillable curve.

Speed: the slow part (one CLOB price-history call per market) is collected for
all days IN PARALLEL via a thread pool, cached to a JSON file, then the
compounding + depth chain is replayed instantly in pure Python.

Run:  python -u -m polybot.backfill_depth
"""
import datetime
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .backfill30 import collect_day_bets, replay_day

START = "2026-05-13"
END = "2026-06-13"
DEPOSIT = 500.0
DEPTH_FRAC = 0.03   # bet at most 3% of a market's total volume
CACHE = os.path.join(os.path.dirname(__file__), "backfill_bets_cache.json")


def _days(start, end):
    d0 = datetime.date.fromisoformat(start)
    d1 = datetime.date.fromisoformat(end)
    out, d = [], d0
    while d <= d1:
        out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def collect_all(days, use_cache=True):
    """Fetch every day's qualifying bets in parallel; cache the result."""
    if use_cache and os.path.exists(CACHE):
        with open(CACHE) as f:
            cached = json.load(f)
        if set(cached) == set(days):
            print(f"[cache] loaded {len(cached)} days from {CACHE}")
            return cached
    result = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(collect_day_bets, d): d for d in days}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                result[d] = fut.result()
            except Exception as e:
                print(f"[warn] {d} failed: {e}")
                result[d] = []
            print(f"[collected] {d}: {len(result[d])} qualifying bets")
    with open(CACHE, "w") as f:
        json.dump(result, f)
    return result


def run():
    days = _days(START, END)
    per_day = collect_all(days)

    bal = DEPOSIT
    rows = []
    for day in days:
        r = replay_day(per_day.get(day, []), daily_budget=bal,
                       max_bets=20, depth_frac=DEPTH_FRAC)
        bal = round(bal + r["profit"], 2)
        rows.append((day, r["won"] + r["lost"], r["won"], r["lost"],
                     r["profit"], bal, r["staked"], r["depth_capped"]))
        print(f"{day}  bets={r['bets']:>2} won={r['won']:>2} lost={r['lost']:>2} "
              f"staked=${r['staked']:>8.2f} profit=${r['profit']:>+9.2f} "
              f"capped={r['depth_capped']:>2}  balance=${bal:,.2f}")

    with sqlite3.connect(config.DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_equity (
                day TEXT PRIMARY KEY,
                bets_settled INTEGER, won INTEGER, lost INTEGER,
                day_profit REAL, balance_after REAL
            )""")
        c.execute("DELETE FROM daily_equity")
        for (day, settled, won, lost, profit, balance, _s, _c) in rows:
            c.execute(
                "INSERT INTO daily_equity "
                "(day, bets_settled, won, lost, day_profit, balance_after) "
                "VALUES (?,?,?,?,?,?)",
                (day, settled, won, lost, profit, balance))

    total_staked = sum(r[6] for r in rows)
    total_won = sum(r[2] for r in rows)
    total_lost = sum(r[3] for r in rows)
    resolved = max(1, total_won + total_lost)
    print("\n" + "=" * 60)
    print(f"DEPOSIT ${DEPOSIT:,.2f} on {START}")
    print(f"FINAL   ${bal:,.2f} on {END}")
    print(f"PROFIT  ${bal - DEPOSIT:,.2f}  ({(bal/DEPOSIT - 1)*100:+.1f}%)")
    print(f"bets won={total_won} lost={total_lost} win%={total_won/resolved*100:.1f}")
    print(f"total staked across month=${total_staked:,.2f} (depth {DEPTH_FRAC*100:.0f}%)")
    return bal


if __name__ == "__main__":
    run()
