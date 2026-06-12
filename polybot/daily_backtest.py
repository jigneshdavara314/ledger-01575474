"""
Day-by-day backtest of the "$500 daily float, skim profit" model.

The user's model:
  - Each DAY starts with $500 of working capital.
  - The bot places longshot-fade NO bets that day, up to the $500 cap.
  - Bets resolve, winnings recycle within the day.
  - At day end, anything ABOVE $500 is swept out as PROFIT (pocketed).
  - The next day resets to $500. Capital never compounds past $500; we harvest.

This backtest replays that over the last N days of real resolved markets,
grouping bets by their resolution day, and books each day's profit/loss so you
see a 30-row daily P&L table and the total skimmed profit.

Honest caveats:
  - We size each bet from the $500 cap (same logic as live), capped per-bet.
  - We can't fetch historical order-book depth, so depth-capping isn't applied
    (real fills may be smaller). Treat daily profit as a budget-scaled estimate.
  - A "day" here = the market's resolution date (UTC). Within a day we don't
    model intraday recycling precisely; we sum that day's bet outcomes and skim
    the net above $500. This is the standard, honest approximation.
"""
import time
import datetime
import requests
from collections import defaultdict

from . import config
from .calibration import price_before_close, _parse, _category_from_text
from .calib_table import measured_no_win
from .longshot import _longshot_tier, FADE_MIN_YES, FADE_MAX_YES, TIER_STAKE_MULT


DAILY_FLOAT = float(__import__("os").getenv("DAILY_FLOAT", "500"))


def _fetch_resolved_window(days: int, max_pages: int = 60) -> list:
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    out, offset = [], 0
    for _ in range(max_pages):
        r = requests.get(
            f"{config.GAMMA_HOST}/markets",
            params={"closed": "true", "limit": 100, "offset": offset,
                    "order": "closedTime", "ascending": "false"},
            timeout=30,
        )
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        stop = False
        for m in batch:
            ct = m.get("closedTime") or m.get("endDate") or ""
            day = ct[:10]
            try:
                cdt = datetime.datetime.fromisoformat(ct.replace("Z", "")[:19])
                if cdt < cutoff:
                    stop = True
                    continue
            except Exception:
                continue
            ql = m.get("question", "").lower()
            if not ("exact score" in ql or "spread:" in ql or "handicap" in ql):
                continue
            prices = _parse(m.get("outcomePrices"))
            toks = _parse(m.get("clobTokenIds"))
            if len(prices) < 2 or len(toks) < 2:
                continue
            if prices not in (["1", "0"], ["0", "1"]):
                continue
            out.append({
                "day": day,
                "question": m.get("question", ""),
                "token_yes": str(toks[0]),
                "yes_won": prices[0] == "1",
            })
        offset += 100
        if stop:
            break
        time.sleep(0.04)
    return out


def run_daily_backtest(days: int = 30, sample_cap: int = 600) -> dict:
    markets = _fetch_resolved_window(days)

    # Build candidate bets (with price + outcome), grouped by day.
    by_day = defaultdict(list)
    checked = 0
    for m in markets:
        if checked >= sample_cap:
            break
        tier = _longshot_tier(m["question"])
        if tier is None:
            continue
        yes_price = price_before_close(m["token_yes"], fraction=0.5)
        if yes_price is None:
            continue
        if not (FADE_MIN_YES <= yes_price <= FADE_MAX_YES):
            continue
        checked += 1
        no_price = round(1.0 - yes_price, 4)
        win = measured_no_win(m["question"], yes_price, no_price)
        if win["est"] - no_price < config.LONGSHOT_MIN_EDGE:
            continue
        by_day[m["day"]].append({
            "tier": tier, "no_price": no_price,
            "no_won": not m["yes_won"],
        })

    # Simulate each day: $500 cap, size bets, sum outcomes, skim above $500.
    per_bet_cap = DAILY_FLOAT * config.LONGSHOT_MAX_BET_FRAC
    base = DAILY_FLOAT / max(1, config.LONGSHOT_MAX_BETS)

    daily = []
    total_profit = 0.0
    total_staked = 0.0
    total_bets = 0
    wins = 0
    for day in sorted(by_day):
        bets = by_day[day]
        spent = 0.0
        ending = DAILY_FLOAT          # start the day at the float
        day_staked = 0.0
        day_bets = 0
        for b in bets:
            stake = round(min(base * TIER_STAKE_MULT[b["tier"]], per_bet_cap), 2)
            if spent + stake > DAILY_FLOAT:    # respect the daily cap
                break
            spent += stake
            day_staked += stake
            day_bets += 1
            shares = stake / b["no_price"] if b["no_price"] > 0 else 0
            if b["no_won"]:
                ending += round(shares - stake, 4)   # profit on the win
                wins += 1
            else:
                ending += round(-stake, 4)           # lose the stake
        skim = round(ending - DAILY_FLOAT, 2)         # profit swept at day end
        total_profit += skim
        total_staked += day_staked
        total_bets += day_bets
        daily.append({
            "day": day, "bets": day_bets, "staked": round(day_staked, 2),
            "end_balance": round(ending, 2), "profit": skim,
        })

    n_days = len(daily)
    return {
        "model": f"${DAILY_FLOAT:.0f} daily float, skim profit above float",
        "days_with_action": n_days,
        "window_days": days,
        "total_bets": total_bets,
        "total_staked": round(total_staked, 2),
        "total_profit": round(total_profit, 2),
        "avg_daily_profit": round(total_profit / n_days, 2) if n_days else 0,
        "win_rate": round(wins / total_bets, 3) if total_bets else 0,
        "daily": daily,
    }
