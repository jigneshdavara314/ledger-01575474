"""
Strategy backtest — replay our ACTUAL longshot-fade logic on the last N days of
resolved markets and measure what it WOULD have earned.

This is the validation the user (rightly) asked for: instead of waiting for live
bets to resolve, we apply the exact same decision rules to markets that have
ALREADY resolved, and check whether our predicted NO bets actually won.

For each historical resolved longshot market:
  1. Would our strategy have bet on it? (same patterns, price band, edge gate)
  2. What price would we have paid (mid-life YES -> NO price)?
  3. Did NO actually win? (the longshot missed)
  4. Compute realised P&L exactly like the live settler.

Output: win rate, P&L, ROI, and a confidence verdict (Wilson CI) — so you know
whether the edge is real BEFORE risking anything.
"""
import math
import time
import requests
from collections import defaultdict

from . import config
from .calibration import _category_from_text, price_before_close, _parse
from .calib_table import measured_no_win
from .longshot import (_longshot_tier, FADE_MIN_YES, FADE_MAX_YES,
                       TIER_STAKE_MULT, budget_base_stake)


def _wilson(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    m = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / d
    return (max(0, c-m), min(1, c+m))


def fetch_recent_resolved(days: int = 30, max_pages: int = 30) -> list:
    """
    Pull resolved binary markets closed within the last `days` days, with their
    token id and outcome. Uses closedTime ordering (newest first) and stops once
    we pass the cutoff.
    """
    import datetime as _dt
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=days)
    out = []
    offset = 0
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
            # date filter
            ct = m.get("closedTime") or m.get("endDate") or ""
            try:
                cdt = _dt.datetime.fromisoformat(ct.replace("Z", "")[:19])
                if cdt < cutoff:
                    stop = True
                    continue
            except Exception:
                pass
            prices = _parse(m.get("outcomePrices"))
            toks = _parse(m.get("clobTokenIds"))
            if len(prices) < 2 or len(toks) < 2:
                continue
            if prices not in (["1", "0"], ["0", "1"]):
                continue
            out.append({
                "question": m.get("question", ""),
                "token_yes": str(toks[0]),
                "yes_won": prices[0] == "1",
                "category": _category_from_text(m.get("question", "")),
            })
        offset += 100
        if stop:
            break
        time.sleep(0.05)
    return out


def backtest_longshot(days: int = 30, sample_cap: int = 500) -> dict:
    """
    Replay the longshot-fade strategy on resolved markets from the last `days`.
    Returns a full results dict with per-tier and overall stats.
    """
    markets = fetch_recent_resolved(days=days)

    # Each simulated bet: dict with tier, paid, won, pnl, stake
    bets = []
    checked = 0
    for m in markets:
        if checked >= sample_cap:
            break
        tier = _longshot_tier(m["question"])
        if tier is None:
            continue
        # mid-life YES price (the price we'd have seen when betting)
        yes_price = price_before_close(m["token_yes"], fraction=0.5)
        if yes_price is None:
            continue
        if not (FADE_MIN_YES <= yes_price <= FADE_MAX_YES):
            continue
        checked += 1

        no_price = round(1.0 - yes_price, 4)
        win = measured_no_win(m["question"], yes_price, no_price)
        est_win = win["est"]
        edge = est_win - no_price
        if edge < config.LONGSHOT_MIN_EDGE:
            continue

        # We'd have bought NO at ~no_price (ignore mid-price improvement here to be
        # conservative — assume we paid the implied NO price).
        # Size from the DAILY BUDGET (same base as live), scaled by tier and capped
        # at the per-bet fraction. NOTE: the backtest cannot fetch historical book
        # depth, so it does NOT apply the live depth cap — real live stakes on thin
        # exact-score markets may be smaller than shown here. Treat this P&L as the
        # budget-scaled upper estimate; live depth capping makes it more conservative.
        base = budget_base_stake()
        stake = round(base * TIER_STAKE_MULT[tier], 2)
        stake = round(min(stake, config.DAILY_BUDGET_USD * config.LONGSHOT_MAX_BET_FRAC), 2)
        shares = stake / no_price if no_price > 0 else 0
        no_won = not m["yes_won"]      # NO wins when the longshot (YES) missed
        pnl = round(shares - stake, 4) if no_won else round(-stake, 4)

        bets.append({
            "tier": tier, "subtype": _subtype(m["question"]),
            "paid": no_price, "won": no_won, "pnl": pnl, "stake": stake,
            "est_win": est_win, "category": m["category"],
        })

    return _summarize_bets(bets, days)


def _subtype(q):
    ql = q.lower()
    if "exact score" in ql: return "exact_score"
    if "spread:" in ql or "handicap" in ql: return "spread/handicap"
    return "other"


def _summarize_bets(bets: list, days: int) -> dict:
    def block(rows):
        n = len(rows)
        if n == 0:
            return None
        wins = sum(1 for b in rows if b["won"])
        pnl = round(sum(b["pnl"] for b in rows), 2)
        staked = round(sum(b["stake"] for b in rows), 2)
        wr = wins / n
        lo, hi = _wilson(wins, n)
        roi = (pnl / staked) if staked else 0.0
        # predicted win rate (what our model claimed) vs actual
        pred = sum(b["est_win"] for b in rows) / n
        verdict = "INSUFFICIENT (n<20)"
        if n >= 20:
            if lo > 0.5 and roi > 0:
                verdict = "EDGE CONFIRMED (win-CI>50% and +ROI)"
            elif hi < 0.5:
                verdict = "NEGATIVE (NO side loses)"
            else:
                verdict = "INCONCLUSIVE (CI spans 50%)"
        return {
            "n": n, "wins": wins, "win_rate": round(wr, 3),
            "win_ci": [round(lo, 3), round(hi, 3)],
            "predicted_win": round(pred, 3),
            "pnl": pnl, "staked": staked, "roi": round(roi, 3),
            "verdict": verdict,
        }

    by_tier = defaultdict(list)
    by_sub = defaultdict(list)
    for b in bets:
        by_tier[b["tier"]].append(b)
        by_sub[b["subtype"]].append(b)

    return {
        "days": days,
        "total_bets": len(bets),
        "overall": block(bets),
        "by_tier": {k: block(v) for k, v in by_tier.items()},
        "by_subtype": {k: block(v) for k, v in by_sub.items()},
    }
