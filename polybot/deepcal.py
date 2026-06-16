"""
Deep calibration sampler — reusable, category-filterable, sub-type aware.

Used by the multi-agent research sweep: each worker calls run_category_study()
for one category and returns structured edge findings. Also usable standalone.

The core measurement (honest): for resolved markets, take the YES price mid-life
(filtering out dead markets that never moved off 0.50), and compare actual win
rate to the price paid. A category/sub-type where YES systematically wins LESS
than its price => buying NO there is +EV (and vice-versa).
"""
import json
import time
import math
import requests
from collections import defaultdict

from . import config
from .calibration import _category_from_text, price_before_close, _parse


# Sub-type classifier — finer than category, since edge often lives in a niche
# (e.g. "exact score" within soccer).
def classify_subtype(question: str) -> str:
    q = question.lower()
    if "exact score" in q:                         return "exact_score"
    if "spread:" in q or "handicap" in q:          return "spread/handicap"
    if any(k in q for k in ["o/u", "over/under", "total rounds", "total:"]):
        return "over_under"
    if "draw" in q:                                return "draw"
    if " win" in q or "winner" in q:               return "moneyline/winner"
    if "up or down" in q:                          return "up_down"
    if "above $" in q or "above " in q:            return "price_threshold"
    if any(k in q for k in ["odd", "even"]):       return "odd_even"
    if any(k in q for k in ["penta", "first ", "any player"]): return "prop"
    return "other"


from .stats import wilson_ci  # single source of truth


def fetch_resolved_for_category(category: str, max_pages: int = 25) -> list:
    """Pull resolved binary markets whose inferred category matches."""
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
        for m in batch:
            prices = _parse(m.get("outcomePrices"))
            toks = _parse(m.get("clobTokenIds"))
            if len(prices) < 2 or len(toks) < 2:
                continue
            if prices not in (["1", "0"], ["0", "1"]):
                continue
            q = m.get("question", "")
            if _category_from_text(q) != category:
                continue
            out.append({
                "question": q,
                "token_yes": str(toks[0]),
                "yes_won": prices[0] == "1",
            })
        offset += 100
        time.sleep(0.05)
    return out


def run_category_study(category: str, sample_cap: int = 250) -> dict:
    """
    Sample resolved markets for one category, measure calibration by sub-type.
    Returns a structured dict suitable for an agent to return.
    """
    markets = fetch_resolved_for_category(category)
    by_sub = defaultdict(list)   # subtype -> [(price, won)]
    overall = []
    checked = 0

    for m in markets:
        if checked >= sample_cap:
            break
        p = price_before_close(m["token_yes"], fraction=0.5)
        if p is None:
            continue
        checked += 1
        won = 1 if m["yes_won"] else 0
        sub = classify_subtype(m["question"])
        by_sub[sub].append((p, won))
        overall.append((p, won))

    def summarize(rows):
        n = len(rows)
        if n == 0:
            return None
        avg_price = sum(p for p, _ in rows) / n
        wins = sum(w for _, w in rows)
        actual = wins / n
        lo, hi = wilson_ci(wins, n)
        # edge on the YES side; NO-side edge is the negative of this
        edge_yes = actual - avg_price
        ci_lo, ci_hi = lo - avg_price, hi - avg_price
        # which side is +EV, and is it significant?
        side = None
        if n >= 25:
            if ci_lo > 0.03:
                side = "BUY_YES"
            elif ci_hi < -0.03:
                side = "BUY_NO"
        return {
            "n": n,
            "avg_price": round(avg_price, 3),
            "actual_win": round(actual, 3),
            "edge_yes": round(edge_yes, 3),
            "ci": [round(ci_lo, 3), round(ci_hi, 3)],
            "signal": side,
        }

    subtypes = {}
    for sub, rows in by_sub.items():
        s = summarize(rows)
        if s:
            subtypes[sub] = s

    return {
        "category": category,
        "sampled": checked,
        "overall": summarize(overall),
        "subtypes": subtypes,
    }
