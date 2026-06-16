"""
Shared statistics primitives — the SINGLE source of truth.

Previously the Wilson interval was reimplemented in 4+ modules (calib_table,
deepcal, edge_hunt, edge_scan15) with subtle divergence risk. Centralising it
here means one correct implementation, used everywhere, with one test.
"""
import math


def wilson_lower(wins, n, z: float = 1.96) -> float:
    """Wilson score interval LOWER bound for a binomial proportion.

    A conservative floor on the true win rate: it sits below the observed rate,
    and the gap widens as n shrinks — so a thin sample can't masquerade as a
    strong edge. `z` is the standard-normal quantile (1.96 = 95% one-sided ~97.5%;
    pass a Bonferroni-corrected z for multiple testing).

    `wins` may be a count (int) or already a rate*n; we normalise via n.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    rad = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - rad) / denom)


def wilson_lower_rate(rate: float, n: int, z: float = 1.96) -> float:
    """Same as wilson_lower but takes an observed RATE (0-1) instead of a count."""
    return wilson_lower(rate * n, n, z)


def wilson_ci(wins, n, z: float = 1.96):
    """Two-sided Wilson score interval (lower, upper) for a proportion."""
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (max(0.0, c - m), min(1.0, c + m))


def bonferroni_z(n_tests: int, alpha: float = 0.05) -> float:
    """One-sided z for a family-wise error rate `alpha` across `n_tests` cells.
    Without this correction, ~alpha*n_tests null cells pass by chance."""
    from statistics import NormalDist
    per = alpha / max(1, n_tests)
    return NormalDist().inv_cdf(1 - per)
