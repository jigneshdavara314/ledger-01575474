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
    "exact_score": [
        (0.25, 0.98, 12),   # deep longshots: NO almost always wins
        (0.45, 0.97, 29),   # the confirmed bucket (avg YES 0.39, NO won ~97%)
        (0.60, 0.90, 8),    # higher-priced "longshots" — less edge, smaller n
    ],
    "spread/handicap": [
        (0.35, 0.84, 9),    # exploratory: weak/noisy overpricing
        (0.55, 0.74, 15),
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


def _subtype_for(question: str) -> str:
    q = question.lower()
    if "exact score" in q:
        return "exact_score"
    if "home runs o/u" in q:
        return "home_runs_ou"
    if "spread:" in q or "handicap" in q:
        return "spread/handicap"
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
    if measured is None:
        return {"est": implied_no, "n": 0, "source": "market"}

    # Shrinkage blend toward the market-implied probability.
    w = n / (n + MIN_TRUST_N)
    est = w * measured + (1 - w) * implied_no
    # TWO-SIDED: cap optimism at HARD_CAP, but DO NOT floor at the market price.
    # If the measured rate is at or below the market-implied NO probability, the
    # estimate is allowed to come out at/below market -> the edge goes to zero or
    # negative downstream and the bet is correctly vetoed. (Previously a max()
    # floor meant the data could never veto a bet — a structural pro-bet bias.)
    est = min(HARD_CAP, est)
    return {"est": round(est, 4), "n": n,
            "source": "measured" if n >= MIN_TRUST_N else "blended"}
