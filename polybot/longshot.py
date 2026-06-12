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
from .market_data import fetch_short_term_markets, Market


# Question patterns that historically showed the strongest overpricing.
LONGSHOT_PATTERNS = [
    "exact score",     # strongest edge in the study
    "spread:",         # handicap longshots
]

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


def _is_longshot_question(q: str) -> bool:
    ql = q.lower()
    return any(pat in ql for pat in LONGSHOT_PATTERNS)


def find_longshot_fades(
    max_hours: float = None,
    stake_usd: float = None,
) -> List[FadeSignal]:
    """
    Scan open short-term markets for overpriced longshots and propose a small
    NO bet on each. Returns a diversified list of fade signals.
    """
    max_hours = max_hours or config.MAX_HOURS_TO_RESOLUTION
    stake_usd = stake_usd or config.LONGSHOT_STAKE_USD

    # Pull a wide set of soccer/sports markets (these carry the exact-score subs)
    cats = ["soccer", "nba", "mlb", "tennis", "esports"]
    markets = fetch_short_term_markets(max_hours=max_hours, categories=cats,
                                       limit_per_event=40)

    signals: List[FadeSignal] = []
    for m in markets:
        if not _is_longshot_question(m.question):
            continue
        # only fade genuine longshots in the sane band
        if not (FADE_MIN_YES <= m.price_yes <= FADE_MAX_YES):
            continue
        if m.liquidity < config.LONGSHOT_MIN_LIQUIDITY:
            continue

        no_price = round(1.0 - m.price_yes, 4)
        # Our estimate that the longshot MISSES (NO wins). The backtest implies
        # the true miss rate is much higher than the market's implied (1-yes).
        # We conservatively shade halfway between the implied NO price and 0.95
        # to avoid over-betting on the raw backtest's 100% figure.
        implied_no = no_price
        est_win_prob = round(min(0.95, implied_no + 0.5 * (0.95 - implied_no)), 4)
        edge = round(est_win_prob - no_price, 4)
        if edge < config.LONGSHOT_MIN_EDGE:
            continue

        signals.append(FadeSignal(
            market=m, side="NO", no_price=no_price, yes_price=m.price_yes,
            est_win_prob=est_win_prob, size_usd=stake_usd,
            reason=f"fade longshot: YES@{m.price_yes:.2f} overpriced, "
                   f"buy NO@{no_price:.2f}, est_win={est_win_prob:.2f}",
        ))

    # Diversify: sort by edge, cap the number per scan so we spread bets.
    signals.sort(key=lambda s: s.est_win_prob - s.no_price, reverse=True)
    return signals[: config.LONGSHOT_MAX_BETS]
