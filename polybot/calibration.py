"""
Calibration study: is Polymarket's price actually right, per category?

The honest way to find a real edge: take many ALREADY-RESOLVED markets, look
at the price some time before they resolved, and check whether "buying at
price p" actually won p% of the time. If a category systematically wins MORE
than its price implies, buying there is +EV. If less, it's -EV.

This is what separates a real edge from "high win rate" (which is just buying
favorites and making pennies).

Data sources (all public, no key):
  - Gamma /markets?closed=true  -> resolved markets + outcome (outcomePrices)
  - CLOB /prices-history         -> historical price of a token before close
  - event tags                   -> category
"""
import json
import time
import requests
from collections import defaultdict

from . import config
from .market_data import _category_from_tags, _safe_float


def _category_from_text(q: str) -> str:
    """Infer category from the question text when tags are missing (resolved
    markets don't carry tags). Pattern-based, conservative."""
    ql = q.lower()
    if any(k in ql for k in ["map ", "rounds:", "counter-strike", "valorant",
                             "league of legends", "dota", "esports", " cs2"]):
        return "esports"
    if any(k in ql for k in ["set ", "games o/u", "set handicap", "tiebreak"]):
        return "tennis"
    if any(k in ql for k in ["exact score", "o/u 0.5", "o/u 1.5", "o/u 2.5",
                             "draw?", " win on ", "spread:"]):
        return "soccer"
    if any(k in ql for k in ["up or down", "bitcoin", "ethereum", "above $",
                             "dogecoin", "solana", " btc ", " eth "]):
        return "crypto"
    if any(k in ql for k in ["nba", "knicks", "lakers", "celtics", " 1h spread"]):
        return "nba"
    if any(k in ql for k in ["mlb", "yankees", "braves", "innings"]):
        return "mlb"
    if any(k in ql for k in ["temperature", "highest temp", "weather"]):
        return "weather"
    return "other"


def _parse(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except Exception:
        return []


def fetch_resolved_markets(pages: int = 10, per_page: int = 100) -> list:
    """
    Pull resolved markets with a clean YES/NO outcome and their CLOB token ids.
    Returns list of dicts: {question, category, token_yes, yes_won, end}.
    """
    out = []
    offset = 0
    for _ in range(pages):
        r = requests.get(
            f"{config.GAMMA_HOST}/markets",
            params={"closed": "true", "limit": per_page, "offset": offset,
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
            # clean binary resolution only
            if prices not in (["1", "0"], ["0", "1"]):
                continue
            yes_won = prices[0] == "1"

            # category from event tags if present, else infer from question text
            tags = []
            if m.get("events"):
                tags = m["events"][0].get("tags") or []
            cat = _category_from_tags(tags)
            if cat == "other":
                cat = _category_from_text(m.get("question", ""))

            out.append({
                "question": m.get("question", ""),
                "category": cat,
                "token_yes": str(toks[0]),
                "yes_won": yes_won,
                "end": m.get("endDate", ""),
            })
        offset += per_page
        time.sleep(0.1)
    return out


def price_before_close(token_yes: str, fraction: float = 0.5) -> float:
    """
    Return the YES price at a point `fraction` of the way through the market's
    life (0.5 = midway). This approximates the price you'd have 'bid' at well
    before resolution, avoiding the final collapse to 0/1.

    Returns None for markets that are NOT genuinely tradeable signals:
      - too few price points (illiquid)
      - the price NEVER moved meaningfully away from 0.50 (dead / placeholder
        market, e.g. an illiquid prop or a 5-min crypto market that opened at
        0.50 and barely traded). Including these inflates apparent edge.
      - already collapsed to a near-certain outcome at the sample point.
    """
    try:
        r = requests.get(
            f"{config.CLOB_HOST}/prices-history",
            params={"market": token_yes, "interval": "max", "fidelity": 180},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        hist = r.json().get("history", [])
        if len(hist) < 5:
            return None

        prices = [_safe_float(h.get("p")) for h in hist]

        # Dead-market filter: require the price to have genuinely moved away
        # from 0.50 at some point BEFORE the final-collapse region. If it never
        # did, the market never formed a real opinion -> not a usable signal.
        pre_collapse = prices[: int(len(prices) * 0.7)]
        if not any(abs(p - 0.5) > 0.08 for p in pre_collapse):
            return None

        idx = int(len(hist) * fraction)
        idx = max(0, min(len(hist) - 1, idx))
        p = prices[idx]

        # skip points at the placeholder 0.50 exactly, or already decided
        if p <= 0.02 or p >= 0.98 or abs(p - 0.5) < 0.001:
            return None
        return p
    except Exception:
        return None


def run_calibration(pages: int = 8, sample_cap: int = 400):
    """
    Main study. For each resolved market, fetch its mid-life YES price and
    record (price_bucket, category, yes_won). Then compute, per category and
    per price bucket, the ACTUAL win rate vs the IMPLIED win rate (the price).

    A positive (actual - implied) means buying YES there was underpriced (+EV).
    """
    markets = fetch_resolved_markets(pages=pages)
    print(f"Fetched {len(markets)} cleanly-resolved markets. "
          f"Sampling price history (cap {sample_cap})...\n")

    # bucket -> list of (implied_price, won)
    by_cat = defaultdict(list)
    by_bucket = defaultdict(list)  # global price-bucket calibration
    checked = 0

    for m in markets:
        if checked >= sample_cap:
            break
        p = price_before_close(m["token_yes"], fraction=0.5)
        if p is None:
            continue
        checked += 1
        won = 1 if m["yes_won"] else 0
        by_cat[m["category"]].append((p, won))
        bucket = round(p * 10) / 10  # 0.1-wide buckets
        by_bucket[bucket].append((p, won))
        if checked % 50 == 0:
            print(f"  ...{checked} sampled")

    return by_cat, by_bucket, checked
