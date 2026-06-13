"""
Longshot-fade strategy.

Research finding (run_calibration.py, 2026-06): soccer "Exact Score" and
"Spread" longshot markets are systematically OVERPRICED. People overpay for
unlikely big-payout outcomes (classic favorite-longshot bias). In the sampled
resolved markets, YES on exact-score longshots (avg priced ~0.39) won ~0% of
the time -> buying NO would have profited heavily.

This module finds those overpriced longshots that are still OPEN and proposes
buying NO on each, spread across many markets with small stakes. The thesis:
each individual NO is ~85-95% likely to win, and diversifying across many
uncorrelated longshots smooths the variance.

HONESTY / SAFEGUARDS (this is a fade of a bias, not a sure thing):
  - We only fade longshots in a sane price band (0.10-0.55). Below that the NO
    is already ~fully priced (no edge); above that it's not a longshot.
  - We require real liquidity.
  - We diversify: many small bets, never big on one.
  - The 38% edge in the backtest is partly because exact scores are genuinely
    rare; it may shrink on truly live, liquid markets. PAPER-test first and let
    `report` confirm the win rate before trusting it.
"""
from dataclasses import dataclass
from typing import List

from . import config
from .market_data import (fetch_short_term_markets, Market, limit_bid_price,
                          fillable_depth)
from .calib_table import measured_no_win


# Question patterns that historically showed the strongest overpricing, each
# with a confidence TIER from the calibration research:
#   "confirmed" = statistically significant edge (n>=25, CI excludes 0)
#   "exploratory" = likely overpriced by analogy but not yet confirmed at n>=25
LONGSHOT_TIERS = {
    "exact score":      "confirmed",    # soccer exact-score: +36% NO edge, n=32, CI clear
    # Baseball player Home-Run props ("Player: Home Runs O/U 0.5/1.5"): people
    # overpay that a hitter WILL homer; NO wins ~94% (n=64, +87% EV, WilsonLB 85%,
    # held out-of-sample) — confirmed by the edge hunt (2026-06). NOTE: this is
    # specific to HOME RUNS O/U; generic over/under is NOT an edge (it's -EV).
    "home runs o/u":    "confirmed",
    "spread:":          "exploratory",  # spread/handicap: same bias likely, not yet confirmed
    "handicap":         "exploratory",
    # Tweet-count RANGE markets ("post X-Y posts this week?"): same longshot bias
    # — many mutually-exclusive ranges, each overpriced. NO won ~100% (n=9, +24%)
    # in the hunt. Exploratory until more resolve. Recurs weekly (Trump/Musk/etc).
    "posts from":       "exploratory",
    "posts between":    "exploratory",
}
LONGSHOT_PATTERNS = list(LONGSHOT_TIERS.keys())

# Confirmed edges get a bigger share of the budget; exploratory ones get less
# until they accumulate their own resolved sample. (Win probabilities come from
# the empirical calib_table.)
TIER_STAKE_MULT = {"confirmed": 1.0, "exploratory": 0.5}


def budget_base_stake() -> float:
    """
    Base per-bet stake derived from the CURRENT BANKROLL BALANCE (compounding),
    spread across the expected number of bets. As the balance grows from
    reinvested winnings, bets grow too; as it shrinks, they shrink. This realises
    the user's "$200 once, then compound" model.

    Falls back to DAILY_BUDGET_USD if the bankroll isn't initialised.
    """
    try:
        from .bankroll import balance
        pool = balance()
    except Exception:
        pool = config.daily_budget()
    # Never plan to deploy more than the user's daily budget in a single day,
    # even if the bankroll is large — this is the "invest $X/day" ceiling.
    pool = min(pool, config.daily_budget())
    n = max(1, config.LONGSHOT_MAX_BETS)
    return max(pool / n, 0.0)

# Price band where fading is worthwhile: the YES longshot is priced high enough
# to be overpriced, but not so high it's a real contender.
FADE_MIN_YES = 0.10
FADE_MAX_YES = 0.55


@dataclass
class FadeSignal:
    market: Market
    side: str            # always "NO" — we fade the longshot
    no_price: float      # what we pay for NO
    yes_price: float     # the overpriced longshot side
    est_win_prob: float  # our estimate that NO wins (longshot misses)
    size_usd: float
    reason: str
    tier: str = "exploratory"   # "confirmed" | "exploratory"
    bid_price: float = None     # the limit price we'll actually try (<= ask)
    ask_price: float = None     # the ask we'd otherwise pay
    fill_prob: float = 1.0      # estimated chance the limit order fills
    win_source: str = "market"  # "measured" | "blended" | "market"
    win_n: int = 0              # sample size backing the win estimate
    desired_usd: float = 0.0    # what we WANTED to stake
    fillable_usd: float = 0.0   # what the book can absorb near our price


def _longshot_tier(q: str):
    """Return the confidence tier for a question, or None if not a longshot."""
    ql = q.lower()
    for pat, tier in LONGSHOT_TIERS.items():
        if pat in ql:
            return tier
    return None


def find_longshot_fades(
    max_hours: float = None,
    stake_usd: float = None,
) -> List[FadeSignal]:
    """
    Scan open short-term markets for overpriced longshots and propose a small
    NO bet on each. Returns a diversified list of fade signals.
    """
    max_hours = max_hours or config.MAX_HOURS_TO_RESOLUTION
    # Per-bet base derived from the daily budget (overrides the flat stake).
    stake_usd = stake_usd or budget_base_stake()
    # Per-bet hard cap: never more than this share of the daily budget on one market.
    per_bet_cap = config.daily_budget() * config.LONGSHOT_MAX_BET_FRAC

    # Pull sports (exact-score subs) + tweets-markets (post-count range longshots).
    # Broadened for more breadth: the edge is the same favorite-longshot bias on
    # spread/handicap/exact-score sub-markets, which exist across many sports and
    # esports leagues. More categories = more safe places to deploy the same
    # dollar (diversification), NOT bigger bets per market.
    cats = ["soccer", "nba", "mlb", "nfl", "nhl", "tennis", "ufc", "boxing",
            "cricket", "golf", "f1", "esports", "tweets", "politics"]
    markets = fetch_short_term_markets(max_hours=max_hours, categories=cats,
                                       limit_per_event=40)

    signals: List[FadeSignal] = []
    for m in markets:
        tier = _longshot_tier(m.question)
        if tier is None:
            continue
        # only fade genuine longshots in the sane band
        if not (FADE_MIN_YES <= m.price_yes <= FADE_MAX_YES):
            continue
        if m.liquidity < config.LONGSHOT_MIN_LIQUIDITY:
            continue

        no_price = round(1.0 - m.price_yes, 4)
        # Our estimate that the longshot MISSES (NO wins), from the EMPIRICAL
        # calibration table: the actual measured NO-win rate for this sub-type
        # and price bucket, shrunk toward the market price when the sample is
        # thin. This replaces the old generic heuristic so sizing reflects the
        # real evidence we collected, not a guess.
        implied_no = no_price
        win = measured_no_win(m.question, m.price_yes, implied_no)
        est_win_prob = win["est"]
        win_source = win["source"]
        win_n = win["n"]

        # --- MID-PRICE BIDDING ---
        # Instead of paying the ask, fetch the live order book for the NO token
        # and place a limit order between the midpoint and the ask. If it fills,
        # we paid less -> bigger edge. Fall back to the Gamma price if the book
        # is unavailable.
        quote = limit_bid_price(m.token_id_no, aggression=config.LONGSHOT_BID_AGGRESSION)
        if quote:
            bid_price = quote["price"]
            ask_price = quote["ask"]
            fill_prob = quote["fill_prob_estimate"]
        else:
            bid_price = no_price
            ask_price = no_price
            fill_prob = 1.0

        # Edge is now measured against the price we actually try to pay (bid).
        edge = round(est_win_prob - bid_price, 4)
        if edge < config.LONGSHOT_MIN_EDGE:
            continue

        # Desired stake from the budget, scaled by tier, then capped so no single
        # bet exceeds LONGSHOT_MAX_BET_FRAC of the daily budget (risk control).
        desired_usd = round(stake_usd * TIER_STAKE_MULT[tier], 2)
        desired_usd = round(min(desired_usd, per_bet_cap), 2)

        # REALISTIC SIZING: cap the stake at what the order book can absorb at
        # prices within LONGSHOT_FILL_TOLERANCE of the best ask. We never "bet"
        # more than the thin market can fill near a good price.
        depth = fillable_depth(m.token_id_no,
                               max_price=bid_price + config.LONGSHOT_FILL_TOLERANCE)
        fillable_usd = depth["usd"] if depth else desired_usd
        actual_stake = round(min(desired_usd, fillable_usd), 2)

        # If the market can't even absorb our minimum, skip it (too thin to bet).
        if actual_stake < config.LONGSHOT_MIN_STAKE:
            continue

        signals.append(FadeSignal(
            market=m, side="NO", no_price=no_price, yes_price=m.price_yes,
            est_win_prob=est_win_prob, size_usd=actual_stake, tier=tier,
            bid_price=bid_price, ask_price=ask_price, fill_prob=fill_prob,
            win_source=win_source, win_n=win_n,
            desired_usd=desired_usd, fillable_usd=fillable_usd,
            reason=f"fade {tier} longshot: YES@{m.price_yes:.2f} overpriced, "
                   f"bid NO@{bid_price:.3f}, stake ${actual_stake:.2f} "
                   f"(wanted ${desired_usd:.2f}, fillable ${fillable_usd:.2f})",
        ))

    # Prioritize CONFIRMED edges first (soccer exact-score), then by edge size.
    # This puts the most money on the statistically-proven mispricing.
    tier_rank = {"confirmed": 0, "exploratory": 1}
    signals.sort(key=lambda s: (tier_rank.get(s.tier, 9),
                                -(s.est_win_prob - s.bid_price)))
    return signals[: config.LONGSHOT_MAX_BETS]


def capacity_now(max_hours: float = None) -> dict:
    """
    How much money could realistically be deployed RIGHT NOW across all current
    longshot edges — the honest answer to "can I invest $1000?".

    Sums the order-book depth available near a good price across every qualifying
    fade (no per-bet or budget cap — pure market capacity). This is the ceiling:
    if total_fillable < your capital, the rest can't be deployed at a good price
    today and would have to wait for new markets or worse fills.
    """
    fades = find_longshot_fades(max_hours=max_hours)
    by_tier = {"confirmed": 0.0, "exploratory": 0.0}
    total = 0.0
    for f in fades:
        cap = f.fillable_usd or 0.0
        by_tier[f.tier] = by_tier.get(f.tier, 0.0) + cap
        total += cap
    return {
        "markets": len(fades),
        "total_fillable_usd": round(total, 2),
        "confirmed_fillable_usd": round(by_tier.get("confirmed", 0), 2),
        "exploratory_fillable_usd": round(by_tier.get("exploratory", 0), 2),
    }
