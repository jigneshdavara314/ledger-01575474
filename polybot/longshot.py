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
    # Baseball player Home-Run props ("Player: Home Runs O/U 0.5/1.5"): the edge
    # hunt suggested NO wins ~94%, but a follow-up audit (2026-06) found NO stored,
    # reproducible artifact backing that number and the family is otherwise -EV.
    # DEMOTED to exploratory (half stake) until a real measurement is committed.
    # NOTE: still specific to HOME RUNS O/U; generic over/under is -EV.
    "home runs o/u":    "exploratory",
    "spread:":          "exploratory",  # spread/handicap: same bias likely, not yet confirmed
    "handicap":         "exploratory",
    # Tweet-count RANGE markets ("post X-Y posts this week?"): same longshot bias
    # — many mutually-exclusive ranges, each overpriced. NO won ~100% (n=9, +24%)
    # in the hunt. Exploratory until more resolve. Recurs weekly (Trump/Musk/etc).
    "posts from":       "exploratory",
    "posts between":    "exploratory",
    # DATA-CONFIRMED fades (1.1B-trade archive, price-aware test 2026-06):
    #   draw: NO won 73% @ avg NO 0.69 -> +0.041 EV (n=8190)
    #   novelty 'will X say...': NO won 55% @ avg 0.52 -> +0.048 EV (n=9531)
    # Both sized off their measured CALIB rows (Wilson-LB shrunk). Exploratory
    # until they accumulate their own live settled sample.
    "draw":             "exploratory",
    "will he say":      "exploratory",   # novelty "will X say Y" variants
    "will she say":     "exploratory",
    "will trump say":   "exploratory",
    " say ":            "exploratory",   # broad novelty catch; calib subtype gates it
    # Soccer player props ("<Name>: N+ goals/assists/shots"). NOTE: a rigorous
    # entry-price test REFUTED the deep band; the calib row now only claims a thin
    # shallow band (NO 0.45-0.55) which Wilson-shrinks to near-veto. Scanned so it
    # can build a live sample, but it rarely clears the gate (correct/honest).
    "+ goals":          "exploratory",
    "+ assists":        "exploratory",
    "+ shots":          "exploratory",
    "+ saves":          "exploratory",
    "+ tackles":        "exploratory",
    "+ passes":         "exploratory",
    # Method-of-victory (UFC/boxing "by KO/decision/submission"): entry-price+OOS
    # confirmed at NO 0.75-0.85 only (small n) — calib gates it to that band.
    "method of victory": "exploratory",
    " by ko":           "exploratory",
    "by decision":      "exploratory",
    "by submission":    "exploratory",
    " by tko":          "exploratory",
    # WEATHER/temperature: STRONG entry-price+OOS edge (n~76k, all bands +EV both
    # halves), and resolves DAILY -> high-frequency recurring edge.
    "highest temperature": "exploratory",
    "lowest temperature":  "exploratory",
    "temperature in":      "exploratory",
}
LONGSHOT_PATTERNS = list(LONGSHOT_TIERS.keys())

# Confirmed edges get a bigger share of the budget; exploratory ones get less
# until they accumulate their own resolved sample. (Win probabilities come from
# the empirical calib_table.)
TIER_STAKE_MULT = {"confirmed": 1.0, "exploratory": 0.5, "trial": 0.25}


# Spread the pool across at least this many bets (don't over-concentrate on a
# single-edge day) but no more than LONGSHOT_MAX_BETS.
MIN_DIVERSIFICATION = int(__import__("os").getenv("LONGSHOT_MIN_DIVERSIFY", "6"))


def budget_base_stake(n_expected: int = None) -> float:
    """
    Base per-bet stake from the current bankroll (compounding), spread across the
    EXPECTED number of qualifying bets — not the hard cap of 40. When only a few
    markets qualify, sizing on /40 structurally under-deploys; dividing by the
    real candidate count (clamped to [MIN_DIVERSIFICATION, LONGSHOT_MAX_BETS])
    deploys meaningfully more per bet while keeping the per-bet cap, depth cap, and
    exposure ceiling in control. Capped at the daily-budget "invest $X/day" ceiling.
    """
    try:
        from .bankroll import balance
        pool = balance()
    except Exception:
        pool = config.daily_budget()
    pool = min(pool, config.daily_budget())
    if n_expected is None:
        n = config.LONGSHOT_MAX_BETS
    else:
        n = min(config.LONGSHOT_MAX_BETS, max(MIN_DIVERSIFICATION, n_expected))
    return max(pool / max(1, n), 0.0)

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


def _self_improve_mult(question: str) -> float:
    """Stake multiplier from the day-by-day self-improvement state.
    Returns 0.0 if this market's family was auto-DISABLED (decayed edge), else the
    auto-tuned multiplier (default 1.0 when self-improve hasn't touched it). The
    state file is written by polybot.self_improve; reading it here is how the
    auto-tuned dials actually take effect in live sizing. Safe default = 1.0."""
    try:
        from .self_improve import load_state
        state = load_state()
    except Exception:
        return 1.0
    ql = question.lower()

    def _fam_matches(cell_fam: str) -> bool:
        # family labels are like "spread_handicap", "exact_score", "over_under";
        # match if ANY of their word-parts appears in the question text.
        parts = [p for p in cell_fam.replace("/", "_").split("_") if len(p) > 2]
        return any(p in ql for p in parts) if parts else False

    for cell in state.get("disabled", []):
        if _fam_matches(cell.split("|")[0].strip()):
            return 0.0
    for cell, cfg in state.get("tiers", {}).items():
        if _fam_matches(cell.split("|")[0].strip()):
            # The cell's TIER already sets the base stake via TIER_STAKE_MULT.
            # The state `mult` is the retune-adjusted target; return it RELATIVE
            # to the tier base so net (tier_base * this) == the tuned target,
            # avoiding double-counting (a trial would otherwise be 0.25*0.25).
            tier = cfg.get("tier", "exploratory")
            base = TIER_STAKE_MULT.get(tier, 0.5) or 0.5
            return float(cfg.get("mult", base)) / base
    return 1.0


# Family -> question-text keywords the scanner matches a market by. Single source
# of truth in taxonomy.py, so the discovery classifier (family_of) and this live
# bridge can never drift apart (crypto_pricetail is deliberately absent there).
from .taxonomy import FAMILY_KEYWORDS as _FAMILY_KEYWORDS


def _family_keywords(fam: str):
    """Keywords to match a family to a live market question. Static taxonomy first;
    then a FALLBACK to auto-DISCOVERED families' keywords (discovered_families.json).
    This closes the self-improvement loop: when the hunt discovers a NEW family
    (e.g. 'extremes_highest' = weather) and self-improve promotes it, the live
    scanner can actually find + bet its markets WITHOUT a manual taxonomy edit.
    Crypto stays quarantined (never in either source)."""
    kws = list(_FAMILY_KEYWORDS.get(fam, []))
    if kws:
        return kws
    try:
        import json, os
        p = os.path.join(os.path.dirname(__file__), "..", "discovered_families.json")
        with open(p) as fh:
            for c in (json.load(fh).get("candidates") or []):
                if c.get("family") == fam and c.get("keyword"):
                    # normalize defensively: underscores->spaces so an un-normalized
                    # snake_case keyword still matches real question text.
                    kw = c["keyword"].replace("_", " ").strip().lower()
                    if kw:
                        kws.append(kw)
    except Exception:
        pass
    return kws


def _self_promoted_tier(ql: str):
    """If the live self-improve state has an ACTIVE promoted family matching this
    question, return its tier ('trial'/'exploratory'). Lets auto-discovered edges
    actually get scanned. Returns None if no promoted family matches."""
    try:
        from .self_improve import load_state
        tiers = load_state().get("tiers", {})
    except Exception:
        return None
    for cell, cfg in tiers.items():
        fam = cell.split("|")[0].strip()
        for kw in _family_keywords(fam):
            if kw in ql:
                return cfg.get("tier", "exploratory")
    return None


def _promoted_win(ql: str, no_price: float):
    """For a self-promoted NO-direction edge whose measured price band contains
    `no_price`, return its scan-measured Wilson lower bound + sample n. This is
    the bridge that lets auto-discovered edges actually size a bet — using the
    rigorous recurring measurement (cleared the bulletproof gate + multi-day
    recurrence), never a hardcoded value. Returns None if no eligible cell.

    Only NO-direction cells are usable: longshot.py bets NO. A YES-side promoted
    edge is intentionally skipped (we never invert a measurement we didn't make)."""
    try:
        from .self_improve import load_state
        tiers = load_state().get("tiers", {})
    except Exception:
        return None
    for cell, cfg in tiers.items():
        if cfg.get("direction") != "NO":
            continue
        wl = cfg.get("measured_wilson_lower")
        lo, hi = cfg.get("band_lo"), cfg.get("band_hi")
        if wl is None or lo is None or hi is None:
            continue
        if not (lo <= no_price < hi):       # price must be in the measured band
            continue
        fam = cell.split("|")[0].strip()
        for kw in _family_keywords(fam):
            if kw in ql:
                return {"wl": float(wl), "n": int(cfg.get("measured_n") or 0)}
    return None


def _promoted_win_yes(ql: str, yes_price: float):
    """Mirror of _promoted_win for self-promoted YES-direction edges (buy YES).
    The band is on the YES price (NOT inverted): we buy YES when yes_price is in
    [band_lo, band_hi] and size on the cell's measured Wilson lower bound. Kept
    SEPARATE from _promoted_win so a YES band/price can never leak into a NO bet
    or vice-versa. Returns {wl, n, band} or None."""
    try:
        from .self_improve import load_state
        tiers = load_state().get("tiers", {})
    except Exception:
        return None
    for cell, cfg in tiers.items():
        if cfg.get("direction") != "YES":
            continue
        wl = cfg.get("measured_wilson_lower")
        lo, hi = cfg.get("band_lo"), cfg.get("band_hi")
        if wl is None or lo is None or hi is None:
            continue
        if not (lo <= yes_price < hi):       # YES price must be in the measured band
            continue
        fam = cell.split("|")[0].strip()
        for kw in _family_keywords(fam):
            if kw in ql:
                return {"wl": float(wl), "n": int(cfg.get("measured_n") or 0),
                        "lo": lo, "hi": hi}
    return None


def _longshot_tier(q: str):
    """Return the confidence tier for a question, or None if not a longshot.
    Checks the hardcoded confirmed/exploratory patterns first, then any family
    the self-improvement engine has auto-promoted (trial/exploratory)."""
    ql = q.lower()
    for pat, tier in LONGSHOT_TIERS.items():
        if pat in ql:
            return tier
    return _self_promoted_tier(ql)


def _build_yes_signal(m, py, tier, stake_usd, per_bet_cap):
    """Build a buy-YES FadeSignal for a market matching a promoted YES edge.
    Mirrors the NO path but on the YES token/price; sizes on the measured Wilson
    lower bound (py['wl']); reuses the SAME edge gate, depth cap, and self-improve
    multiplier. Returns None if it doesn't clear the gate / is too thin."""
    yes_price = m.price_yes
    quote = limit_bid_price(m.token_id_yes,
                            aggression=config.LONGSHOT_BID_AGGRESSION,
                            hours_to_res=getattr(m, "hours_to_resolution", None))
    if quote:
        bid_price = quote["price"]; ask_price = quote["ask"]
        fill_prob = quote["fill_prob_estimate"]
    else:
        bid_price = yes_price; ask_price = yes_price; fill_prob = 1.0

    est_win_prob = py["wl"]                       # conservative measured floor
    edge = round(est_win_prob - bid_price, 4)
    if edge < config.LONGSHOT_MIN_EDGE:          # SAME +EV gate as NO path
        return None

    si_mult = _self_improve_mult(m.question)
    if si_mult == 0.0:
        return None
    desired_usd = round(stake_usd * TIER_STAKE_MULT[tier] * si_mult, 2)
    desired_usd = round(min(desired_usd, per_bet_cap), 2)

    # depth on the YES token up to the ASK — the price we'd actually fill at.
    # (Sizing at the sub-ask bid returns $0 because no size rests below the ask;
    # that was the bug that zeroed every stake — see the NO path for the full
    # explanation. Whether the resting bid fills is modelled by fill_prob.)
    depth = fillable_depth(m.token_id_yes, max_price=ask_price)
    fillable_usd = depth["usd"] if depth else 0.0
    actual_stake = round(min(desired_usd, fillable_usd), 2)
    if actual_stake < config.LONGSHOT_MIN_STAKE:
        return None

    return FadeSignal(
        market=m, side="YES", no_price=round(1 - yes_price, 4), yes_price=yes_price,
        est_win_prob=est_win_prob, size_usd=actual_stake, tier=tier,
        bid_price=bid_price, ask_price=ask_price, fill_prob=fill_prob,
        win_source="promoted", win_n=py["n"],
        desired_usd=desired_usd, fillable_usd=fillable_usd,
        reason=f"buy YES {tier} (promoted): YES@{yes_price:.2f} in measured band "
               f"[{py['lo']:.2f}-{py['hi']:.2f}], bid@{bid_price:.3f}, "
               f"stake ${actual_stake:.2f} (wl {est_win_prob:.3f})",
    )


def find_longshot_fades(
    max_hours: float = None,
    stake_usd: float = None,
    min_edge: float = None,
    band_lo: float = None,
    band_hi: float = None,
) -> List[FadeSignal]:
    """
    Scan open short-term markets for overpriced longshots and propose a small
    NO bet on each. Returns a diversified list of fade signals.

    min_edge / band_lo / band_hi let two STRATEGY VERSIONS run different gates off
    the same scan (e.g. conservative 0.06/0.10-0.55 vs aggressive 0.04/0.07-0.60),
    so a live A/B test decides which threshold actually earns. Default to config /
    module constants (the conservative behavior) when not passed.
    """
    max_hours = max_hours or config.MAX_HOURS_TO_RESOLUTION
    min_edge = config.LONGSHOT_MIN_EDGE if min_edge is None else min_edge
    band_lo = FADE_MIN_YES if band_lo is None else band_lo
    band_hi = FADE_MAX_YES if band_hi is None else band_hi
    # Per-bet hard cap: never more than this share of the daily budget on one market.
    per_bet_cap = config.daily_budget() * config.LONGSHOT_MAX_BET_FRAC

    # Pull sports (exact-score subs) + tweets-markets (post-count range longshots).
    # Broadened for more breadth: the edge is the same favorite-longshot bias on
    # spread/handicap/exact-score sub-markets, which exist across many sports and
    # esports leagues. More categories = more safe places to deploy the same
    # dollar (diversification), NOT bigger bets per market.
    cats = ["soccer", "nba", "mlb", "nfl", "nhl", "tennis", "ufc", "boxing",
            "cricket", "golf", "f1", "esports", "tweets", "politics", "weather"]
    markets = fetch_short_term_markets(max_hours=max_hours, categories=cats,
                                       limit_per_event=40)

    # Pre-count candidate markets (tier-matching + liquid) so the per-bet base
    # divides the pool across the EXPECTED number of bets, not the hard cap of 40.
    if stake_usd is None:
        n_candidates = sum(
            1 for m in markets
            if _longshot_tier(m.question) is not None
            and m.liquidity >= config.LONGSHOT_MIN_LIQUIDITY)
        stake_usd = budget_base_stake(n_candidates)

    signals: List[FadeSignal] = []
    for m in markets:
        tier = _longshot_tier(m.question)
        if tier is None:
            continue
        if m.liquidity < config.LONGSHOT_MIN_LIQUIDITY:
            continue

        # --- YES-DIRECTION promoted edge (buy YES, e.g. over_under favorites) ---
        # Handled FIRST and separately from the NO fade path: the band is on the
        # YES price (not the 0.10-0.55 fade band), we buy the YES token, and we
        # size on the cell's measured Wilson lower bound. A YES band/price can
        # never leak into a NO bet.
        py = _promoted_win_yes(m.question.lower(), m.price_yes)
        if py is not None:
            sig = _build_yes_signal(m, py, tier, stake_usd, per_bet_cap)
            if sig is not None:
                signals.append(sig)
            continue

        # only fade genuine longshots in the sane band (NO-side path).
        # Use the per-call band_lo/band_hi so each tournament STRATEGY actually
        # applies its own band (e.g. aggressive_fade's wider 0.07-0.60). These
        # default to the module FADE_MIN_YES/FADE_MAX_YES when not passed.
        if not (band_lo <= m.price_yes <= band_hi):
            continue

        no_price = round(1.0 - m.price_yes, 4)
        # Our estimate that the longshot MISSES (NO wins), from the EMPIRICAL
        # calibration table: the actual measured NO-win rate for this sub-type
        # and price bucket, shrunk toward the market price when the sample is
        # thin. This replaces the old generic heuristic so sizing reflects the
        # real evidence we collected, not a guess.
        implied_no = no_price
        # First: if this market belongs to a SELF-PROMOTED NO-direction edge whose
        # rigorously-measured price band contains our NO price, size on the SCAN-
        # MEASURED Wilson lower bound (the same conservative basis as calib_table,
        # but for an auto-discovered, multi-day-recurring edge). This is what lets
        # discovered edges actually place bids. Falls back to the calib table /
        # market otherwise — no hardcoded guesses anywhere.
        promoted = _promoted_win(m.question.lower(), no_price)
        if promoted is not None:
            est_win_prob = promoted["wl"]
            win_source = "promoted"
            win_n = promoted["n"]
        else:
            win = measured_no_win(m.question, m.price_yes, implied_no)
            est_win_prob = win["est"]
            win_source = win["source"]
            win_n = win["n"]

        # --- MID-PRICE BIDDING ---
        # Instead of paying the ask, fetch the live order book for the NO token
        # and place a limit order between the midpoint and the ask. If it fills,
        # we paid less -> bigger edge. Fall back to the Gamma price if the book
        # is unavailable.
        quote = limit_bid_price(m.token_id_no,
                                aggression=config.LONGSHOT_BID_AGGRESSION,
                                hours_to_res=getattr(m, "hours_to_resolution", None))
        if quote:
            bid_price = quote["price"]
            ask_price = quote["ask"]
            fill_prob = quote["fill_prob_estimate"]
        else:
            bid_price = no_price
            ask_price = no_price
            fill_prob = 1.0

        # Edge is now measured against the price we actually try to pay (bid).
        # Gate on the per-call min_edge so each strategy uses its own threshold
        # (conservative 0.06 vs aggressive 0.04) instead of always the config one.
        edge = round(est_win_prob - bid_price, 4)
        if edge < min_edge:
            continue

        # Desired stake from the budget, scaled by tier, then by the SELF-IMPROVE
        # multiplier (the day-by-day auto-tuned dial), then capped so no single
        # bet exceeds LONGSHOT_MAX_BET_FRAC of the daily budget (risk control).
        si_mult = _self_improve_mult(m.question)
        if si_mult == 0.0:                      # auto-disabled by the decay guard
            continue
        desired_usd = round(stake_usd * TIER_STAKE_MULT[tier] * si_mult, 2)
        desired_usd = round(min(desired_usd, per_bet_cap), 2)

        # REALISTIC SIZING: size on the order-book depth genuinely reachable for
        # this bet. We bid between the mid and the ask, so the liquidity we can
        # actually capture is the ask-side depth up to (and including) the ASK —
        # i.e. what a seller is offering right now and what we'd fill against as
        # the book moves to us. Measuring depth only AT-OR-BELOW our sub-ask bid
        # is wrong: there is essentially NEVER resting size priced below the best
        # ask, so that check returned $0 for ~every market and silently zeroed
        # every stake — the bug that froze longshot betting after 2026-06-13.
        # Whether the resting bid fills is modelled separately by fill_prob; the
        # SIZE is the real reachable depth, not phantom liquidity. We cap the
        # price ceiling at the ask (never count depth priced above the ask).
        depth = fillable_depth(m.token_id_no, max_price=ask_price)
        fillable_usd = depth["usd"] if depth else 0.0
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
    tier_rank = {"confirmed": 0, "exploratory": 1, "trial": 2}
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
