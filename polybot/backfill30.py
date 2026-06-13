"""
True 30-day backfill — reconstruct the day-by-day equity curve as if we'd been
running the longshot-fade strategy since 2026-05-13.

Uses the Gamma date-range filter (end_date_min / end_date_max) which DOES reach
back a full month (unlike the plain closed=true list which only serves ~2 days).
For each day we fetch that day's resolved longshot markets, replay our exact
betting logic against their real mid-life prices and real outcomes, and book the
day's P&L. The result is a genuine historical equity curve — real prices, real
outcomes, our real rules.
"""
import time
import requests

from . import config
from .calibration import price_before_close, _parse
from .longshot import _longshot_tier, FADE_MIN_YES, FADE_MAX_YES, TIER_STAKE_MULT
from .calib_table import measured_no_win


def fetch_day_markets(day: str):
    """Resolved longshot markets whose end date falls on `day` (YYYY-MM-DD)."""
    out = []
    offset = 0
    for _ in range(20):
        r = requests.get(
            f"{config.GAMMA_HOST}/markets",
            params={"closed": "true", "limit": 100, "offset": offset,
                    "end_date_min": f"{day}T00:00:00Z",
                    "end_date_max": f"{day}T23:59:59Z"},
            timeout=30,
        )
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        for m in batch:
            if _longshot_tier(m.get("question", "")) is None:
                continue
            prices = _parse(m.get("outcomePrices"))
            toks = _parse(m.get("clobTokenIds"))
            if len(prices) < 2 or len(toks) < 2:
                continue
            if prices not in (["1", "0"], ["0", "1"]):
                continue
            out.append({"question": m.get("question", ""),
                        "token_yes": str(toks[0]),
                        "yes_won": prices[0] == "1"})
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.03)
    return out


def backfill_day(day: str, daily_budget: float = 500.0, max_bets: int = 20):
    """
    Replay the longshot-fade strategy for one day. Returns the day's summary:
    bets placed, won, lost, profit. Sizing mirrors the live model (budget/maxbets,
    tier-scaled, per-bet cap). No order-book depth (historical) — uses implied NO
    price, which is conservative.
    """
    markets = fetch_day_markets(day)
    base = daily_budget / max_bets
    per_bet_cap = daily_budget * config.LONGSHOT_MAX_BET_FRAC

    bets = won = lost = 0
    profit = 0.0
    spent = 0.0
    for m in markets:
        if bets >= max_bets:
            break
        tier = _longshot_tier(m["question"])
        yes_price = price_before_close(m["token_yes"])
        if yes_price is None or not (FADE_MIN_YES <= yes_price <= FADE_MAX_YES):
            continue
        no_price = round(1 - yes_price, 4)
        est = measured_no_win(m["question"], yes_price, no_price)["est"]
        if est - no_price < config.LONGSHOT_MIN_EDGE:
            continue
        stake = round(min(base * TIER_STAKE_MULT[tier], per_bet_cap), 2)
        if spent + stake > daily_budget:
            break
        spent += stake
        bets += 1
        shares = stake / no_price
        if not m["yes_won"]:        # NO wins (longshot missed)
            won += 1
            profit += shares - stake
        else:
            lost += 1
            profit += -stake
    return {"day": day, "bets": bets, "won": won, "lost": lost,
            "profit": round(profit, 2), "staked": round(spent, 2)}
