"""
Hunt for a 70%+ real-win-rate edge across ALL prediction-market categories.

The user wants "around 70% chance of win" — but the ONLY version of that worth
having is where the true win rate is >= ~70% AND the market price is BELOW it
(so you're not just paying full value for a favorite).

This scans resolved markets by category (inferred from text, since resolved
markets carry no tags) and computes, per price bucket, the actual win rate vs
the price paid. It flags any (category, bucket) where:
  - actual win rate >= 0.68  (the "70% chance" the user wants)
  - AND actual win rate - avg_price > 0.04  (a real edge, not just a favorite)
  - AND n >= 20  (enough sample to trust)
"""
import math
import time
import requests
from collections import defaultdict

from . import config
from .calibration import price_before_close, _parse


# Broader category inference covering the untested categories.
def category_of(q: str) -> str:
    ql = q.lower()
    if any(k in ql for k in ["election", "president", "prime minister", "nominee",
                             "governor", "senate", "mayor", "parliament", "referendum"]):
        return "politics_elections"
    if any(k in ql for k in ["ceasefire", "peace deal", "invade", "war", "sanction",
                             "nuclear", "treaty", "hostage", "strait", "airspace"]):
        return "geopolitics"
    if any(k in ql for k in ["fed", "rate cut", "inflation", "gdp", "recession",
                             "interest rate", "jerome powell", "cpi"]):
        return "macro_econ"
    if any(k in ql for k in ["ipo", "market cap", "acquire", "merger", "earnings",
                             "stock", "valuation"]):
        return "finance_corporate"
    if any(k in ql for k in ["gpt", "ai model", "openai", "llm", "agi", "grok",
                             "claude", "gemini"]):
        return "ai_tech"
    if any(k in ql for k in ["movie", "box office", "grossing", "album", "tweets",
                             "spotify", "oscar", "grammy"]):
        return "pop_culture"
    if any(k in ql for k in ["mlb", "yankees", "dodgers", "innings", "home run"]):
        return "mlb"
    if any(k in ql for k in ["exact score", "spread:", "o/u", "draw?", "win on"]):
        return "sports_match"
    if any(k in ql for k in ["bitcoin", "ethereum", "up or down", "above $"]):
        return "crypto"
    return "other"


def _wilson(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    m = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / d
    return (max(0, c-m), min(1, c+m))


def fetch_resolved(pages=30):
    out, offset = [], 0
    for _ in range(pages):
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
            out.append({
                "question": m.get("question", ""),
                "token_yes": str(toks[0]),
                "yes_won": prices[0] == "1",
                "category": category_of(m.get("question", "")),
            })
        offset += 100
        time.sleep(0.04)
    return out


def hunt(sample_cap=700):
    """
    For each category, on BOTH the YES and NO side, find price buckets where the
    chosen side wins >= 68% AND beats its price by > 4 points (real edge), n>=20.
    """
    markets = fetch_resolved()
    # bucket key: (category, side, price_bucket) -> list of (price_paid, won)
    data = defaultdict(list)
    checked = 0
    for m in markets:
        if checked >= sample_cap:
            break
        p_yes = price_before_close(m["token_yes"], fraction=0.5)
        if p_yes is None:
            continue
        checked += 1
        cat = m["category"]
        # YES side: paid p_yes, won if yes_won
        data[(cat, "YES", round(p_yes*10)/10)].append((p_yes, 1 if m["yes_won"] else 0))
        # NO side: paid (1-p_yes), won if NOT yes_won
        p_no = round(1 - p_yes, 4)
        data[(cat, "NO", round(p_no*10)/10)].append((p_no, 0 if m["yes_won"] else 1))

    findings = []
    for (cat, side, bucket), rows in data.items():
        n = len(rows)
        if n < 20:
            continue
        avg_price = sum(p for p, _ in rows) / n
        wins = sum(w for _, w in rows)
        wr = wins / n
        lo, hi = _wilson(wins, n)
        edge = wr - avg_price
        # The "70% chance with edge" filter
        if wr >= 0.68 and edge > 0.04 and lo > 0.55:
            findings.append({
                "category": cat, "side": side, "price_bucket": bucket,
                "n": n, "win_rate": round(wr, 3), "avg_price": round(avg_price, 3),
                "edge": round(edge, 3), "ci_low": round(lo, 3),
                "after_fee_edge": round(edge - 0.01, 3),  # ~1% PM spread
            })
    findings.sort(key=lambda f: -f["edge"])
    return {"sampled": checked, "findings": findings,
            "n_categories": len(set(c for c, _, _ in data.keys()))}
