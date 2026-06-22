"""
Empirical calibration table — the MEASURED win rates from the calibration study
(run_calibration.py / deepcal.py, ~560 resolved markets, 2026-06).

This replaces the old heuristic where est_win_prob was just "shade halfway to a
cap." Instead we use the ACTUAL observed NO-win rate for each (subtype, price
bucket), with the sample size, so sizing reflects real evidence — and we shrink
toward the market-implied probability when the sample is thin (Bayesian-style),
so we never over-trust a small n.

How to read an entry:
  (subtype, yes_price_bucket) -> {"no_win": <measured P(NO wins)>, "n": <sample>}

Where a bucket isn't measured, we fall back to the market-implied NO price
(no edge claimed). This is the honest default.
"""

# Measured NO-win rates by sub-type and YES-price bucket.
# Derived from the study: soccer exact-score YES priced ~0.39 won only ~3% of
# the time => NO won ~97% (n=29-32). Spread/handicap showed weaker, noisier
# overpricing. Over/under and others were calibrated (no edge).
#
# We keep this deliberately conservative: only the soccer exact-score row claims
# a strong edge, because that's the only one that cleared significance (CI
# excluded 0). Everything else defers to the market.
CALIB = {
    # subtype: list of (yes_price_upper_bound, no_win_rate, n)
    # Rates corrected to the COMMITTED measured artifacts (backtest_result.json:
    # n=29, NO-win 0.931, Wilson CI [0.78, 0.981]) — was an optimistic 0.97.
    # Sizing additionally discounts toward the Wilson LOWER bound (see
    # measured_no_win), so the thin deep-longshot rows can't clear the edge gate
    # on hairline margin.
    "exact_score": [
        (0.25, 0.95, 12),   # deep longshots: NO almost always wins (small n)
        (0.45, 0.931, 29),  # the confirmed bucket (measured 0.931, CI[0.78,0.981])
        (0.60, 0.88, 8),    # higher-priced "longshots" — less edge, smaller n
    ],
    # SPREAD/HANDICAP: REMOVED. A rigorous ENTRY-PRICE test (win rate at the price
    # trades actually executed, not market averages — 2026-06-23) refuted it:
    # NO 0.55-0.75 EV -0.004, NO 0.45-0.55 EV -0.037. The earlier "edge" was
    # survivorship bias in averaging. No row -> falls back to market (no edge).
    # DRAW markets ("will it be a draw?"). ENTRY-PRICE test (real executed trade
    # prices, n=9227): NO won 70.9% @ avg NO 0.699 -> +0.005 EV (THIN but positive,
    # survives where spread/over_under did not). Kept conservative & exploratory.
    # (The earlier +0.041 from market-AVG prices was inflated; entry price is +0.005.)
    "draw": [
        (0.45, 0.709, 9227),   # YES(draw)<=0.45 (NO>=0.55): entry-price NO-win 70.9%
    ],
    # NOVELTY "will X say/do Y" markets. Archive test (n=9531, real prices): NO
    # won 54.9% at avg NO price 0.52 -> +0.048 after-fee EV. People overpay the
    # "yes it'll happen" novelty side. (Previously quarantined as noise; the DATA
    # showed a real edge — corrected.)
    "novelty_says": [
        (0.60, 0.549, 9531),  # NO wins ~55% but trades cheap enough to profit
    ],
    # SOCCER PLAYER PROPS ("<Name>: N+ goals/assists/shots"). CORRECTED 2026-06-23.
    # The earlier row claimed +0.505 EV for NO 0.55-0.75 from market-AVERAGE prices.
    # A rigorous ENTRY-PRICE test (win rate at the price trades ACTUALLY executed)
    # REFUTED that band: NO 0.55-0.75 -> 61.2% @ 0.634 = -0.045 EV (survivorship
    # bias in the averaging). The edge that survives at entry prices is the
    # SHALLOWER band: NO 0.45-0.55 -> 53.8% @ 0.500 = +0.066 EV (n=104). So we
    # claim ONLY that band and DEFER everywhere else.
    #   - YES <=0.45 (NO 0.55-0.95): rate=None -> market (no claim; -EV at entry)
    #   - YES 0.45-0.55 (NO 0.45-0.55): the entry-price-confirmed edge
    "player_prop": [
        (0.45, None,  0),     # NO>=0.55: refuted at entry price -> defer to market
        (0.55, 0.538, 104),   # NO 0.45-0.55: entry-price-confirmed +0.066 EV
    ],
    # Baseball Home-Run props: REMOVED. The edge-hunt rates (~0.93-0.96) had no
    # stored, reproducible artifact and an audit (2026-06) flagged them as
    # unsupported. With no CALIB row, measured_no_win() falls back to the market
    # price (no edge claimed) — the honest default until a real measurement is
    # committed. The pattern is still scanned but sized at the market (exploratory).
}

# Below this sample size we shrink hard toward the market price (don't trust it).
MIN_TRUST_N = 25
# Cap on how much win-prob we'll ever claim (no "100% sure" sizing).
HARD_CAP = 0.97


from .stats import wilson_lower_rate as _wilson_lower  # single source of truth


def _subtype_for(question: str) -> str:
    q = question.lower()
    if "exact score" in q:
        return "exact_score"
    if "home runs o/u" in q:
        return "home_runs_ou"
    if "spread:" in q or "handicap" in q:
        return "spread/handicap"
    # data-confirmed fades (archive price test, 2026-06):
    if "draw" in q:
        return "draw"
    if q.startswith("will ") and (" say" in q or " said" in q or " tweet" in q):
        return "novelty_says"
    # soccer player props: "<Name>: N+ goals/assists/shots/..." (archive-confirmed
    # band-specific fade). US-sport props (home runs/strikeouts) have no calib row
    # yet, so only the "N+ <stat>" soccer pattern maps here.
    import re as _re
    if _re.search(r":\s*\d+\+\s*(goal|assist|shot|save|tackle|pass|block|"
                  r"clearance|interception|point|rebound|three)", q):
        return "player_prop"
    return "other"


def measured_no_win(question: str, yes_price: float, implied_no: float) -> dict:
    """
    Return the best estimate of P(NO wins) for this market, blending the measured
    calibration table with the market-implied NO price.

    Returns {"est": <prob>, "n": <sample backing it>, "source": "measured"|"market"}.

    The blend: with sample n, weight the measured rate by n/(n+MIN_TRUST_N) and the
    market-implied NO by the remainder. Thin samples => trust the market; large
    samples => trust the data. This is a standard shrinkage estimator and is the
    honest way to use a small backtest without over-betting it.
    """
    subtype = _subtype_for(question)
    rows = CALIB.get(subtype)
    if not rows:
        return {"est": implied_no, "n": 0, "source": "market"}

    measured = None
    n = 0
    for upper, rate, count in rows:
        if yes_price <= upper:
            measured, n = rate, count
            break
    # measured is None either when no bucket matched (price above all uppers) OR
    # when the matched bucket explicitly carries rate=None ("defer to market, no
    # edge claimed here"). Both mean: fall back to the market price, no edge.
    if measured is None:
        return {"est": implied_no, "n": 0, "source": "market"}

    # CONSERVATIVE basis: size on the Wilson LOWER bound of the measured rate,
    # not the point estimate. Thin samples (small n) get a wider, lower bound, so
    # a 1-loss/n=12 row can't clear the edge gate on hairline margin. This is the
    # honest way to use a small backtest — bet what the data robustly supports.
    conservative = _wilson_lower(measured, n)

    # Shrinkage blend toward the market-implied probability.
    w = n / (n + MIN_TRUST_N)
    est = w * conservative + (1 - w) * implied_no
    # TWO-SIDED: cap optimism at HARD_CAP, but DO NOT floor at the market price,
    # so a weak measured rate yields zero/negative edge and vetoes the bet.
    est = min(HARD_CAP, est)
    return {"est": round(est, 4), "n": n, "measured": measured,
            "wilson_lower": round(conservative, 4),
            "source": "measured" if n >= MIN_TRUST_N else "blended"}
