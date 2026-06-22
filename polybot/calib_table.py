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
    "spread/handicap": [
        (0.35, 0.84, 9),    # exploratory: weak/noisy overpricing
        (0.55, 0.74, 15),
    ],
    # DRAW markets ("will it be a draw?"). Price-aware test on the 1.1B-trade
    # archive (n=8190, real fill prices): NO won 73.0% while trading at avg NO
    # price 0.69 -> +0.041 after-fee EV. A genuine fade (people overpay "draw").
    "draw": [
        (0.45, 0.73, 8190),   # YES (draw) priced up to ~0.45 -> NO wins ~73%
    ],
    # NOVELTY "will X say/do Y" markets. Archive test (n=9531, real prices): NO
    # won 54.9% at avg NO price 0.52 -> +0.048 after-fee EV. People overpay the
    # "yes it'll happen" novelty side. (Previously quarantined as noise; the DATA
    # showed a real edge — corrected.)
    "novelty_says": [
        (0.60, 0.549, 9531),  # NO wins ~55% but trades cheap enough to profit
    ],
    # SOCCER PLAYER PROPS ("<Name>: N+ goals/assists/shots"). The discovered edge
    # ('other | pay NO 0.55-0.75', live scan n=203, 79.3%). Archive price test
    # (real fill prices) confirms it is BAND-SPECIFIC:
    #   NO 0.55-0.75  (YES 0.25-0.45): NO won 93.5% @ avg NO 0.617 -> +0.505 EV  ** REAL **
    #   NO 0.45-0.55  (YES 0.45-0.55): too few to call
    #   NO < 0.45     (YES > 0.55):    NO won  4.3% -> -0.892 EV  ** CATASTROPHIC **
    # So we ONLY claim the edge for YES in 0.25-0.45. The first matching row wins,
    # so the <=0.25 row defers to market (no data that deep), the <=0.45 row carries
    # the measured edge, and ANYTHING above 0.45 has NO row -> falls back to market
    # (no edge claimed), which correctly vetoes the catastrophic high-NO-price band.
    "player_prop": [
        (0.25, None,  0),    # deeper than measured: defer to market (no claim)
        (0.45, 0.935, 31),   # the measured edge band (NO 0.55-0.75)
        # (no row above 0.45 -> market fallback -> no edge -> veto)
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
