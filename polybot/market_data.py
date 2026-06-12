"""
Reads live market data from Polymarket's public APIs.
No wallet / key required for reading — only for trading.

Uses:
  - Gamma API  -> rich market metadata, events, tags, sub-markets
  - CLOB API   -> live order book (best bid/ask), resolution outcomes
"""
import json
import requests
import datetime
from dataclasses import dataclass, field
from typing import List, Optional, Dict

from . import config


@dataclass
class Market:
    condition_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    price_yes: float          # current market probability of YES (0..1)
    liquidity: float
    volume: float
    volume_24h: float
    spread: float
    end_date: str
    hours_to_resolution: float
    category: str             # e.g. "soccer", "esports", "crypto", "tennis"
    event_title: str          # parent event title for context

    @property
    def price_no(self) -> float:
        return round(1.0 - self.price_yes, 4)


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


def _hours_until(end_date_str: str) -> float:
    """Hours from now until the market end date. Negative = already past."""
    if not end_date_str:
        return 9999.0
    try:
        end_dt = datetime.datetime.fromisoformat(end_date_str.replace("Z", ""))
        delta = (end_dt - datetime.datetime.utcnow()).total_seconds()
        return round(delta / 3600, 2)
    except Exception:
        return 9999.0


def _category_from_tags(tags: list) -> str:
    """Map Polymarket tag slugs to our internal category names."""
    if not tags:
        return "other"
    tag_set = {(t.get("slug", "") if isinstance(t, dict) else str(t)).lower() for t in tags}

    # Priority order — more specific first
    if any(s in tag_set for s in ["counter-strike-2", "valorant", "league-of-legends",
                                   "esports", "dota-2", "fortnite"]):
        return "esports"
    if any(s in tag_set for s in ["soccer", "fifa-world-cup", "2026-fifa-world-cup"]):
        return "soccer"
    if any(s in tag_set for s in ["tennis"]):
        return "tennis"
    if any(s in tag_set for s in ["basketball", "nba", "nba-finals"]):
        return "nba"
    if any(s in tag_set for s in ["baseball", "mlb"]):
        return "mlb"
    if any(s in tag_set for s in ["football", "nfl"]):
        return "nfl"
    if any(s in tag_set for s in ["ufc", "mma"]):
        return "ufc"
    if any(s in tag_set for s in ["golf", "pga", "pga-tour"]):
        return "golf"
    if any(s in tag_set for s in ["crypto", "bitcoin", "ethereum", "weekly",
                                   "multi-strikes", "hit-price", "crypto-prices"]):
        return "crypto"
    if any(s in tag_set for s in ["politics", "elections", "global-elections"]):
        return "politics"
    return "other"


def fetch_markets(limit: int = 30) -> List[Market]:
    """Fetch active, liquid binary markets sorted by volume (legacy function)."""
    url = f"{config.GAMMA_HOST}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "order": "volumeNum",
        "ascending": "false",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return _parse_gamma_markets(resp.json(), event_title="", category="other")


def fetch_short_term_markets(
    max_hours: float = None,
    categories: list = None,
    limit_per_event: int = 5,
) -> List[Market]:
    """
    Fetch markets that resolve within `max_hours` hours, filtered to the
    specified categories. Uses the events API to get tag metadata.

    This is the main scanner for the short-term strategy.
    """
    max_hours = max_hours or config.MAX_HOURS_TO_RESOLUTION
    categories = categories or config.TARGET_CATEGORIES

    url = f"{config.GAMMA_HOST}/events"
    params = {
        "active": "true",
        "closed": "false",
        "limit": 200,
        "order": "volume24hr",
        "ascending": "false",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    events = resp.json()

    markets: List[Market] = []

    for event in events:
        # Category filter
        tags = event.get("tags") or []
        cat = _category_from_tags(tags)
        if cat not in categories:
            continue

        # Time filter at the event level (quick pre-filter)
        event_end = event.get("endDate", "")
        event_hrs = _hours_until(event_end)
        if event_hrs > max_hours or event_hrs < 0:
            continue

        event_title = event.get("title", "") or event.get("slug", "")

        # Parse the sub-markets embedded in the event
        sub_markets = event.get("markets") or []
        parsed = _parse_gamma_markets(
            sub_markets,
            event_title=event_title,
            category=cat,
            max_hours=max_hours,
        )
        markets.extend(parsed[:limit_per_event])

    return markets


def _parse_gamma_markets(
    raw: list,
    event_title: str,
    category: str,
    max_hours: float = 9999,
) -> List[Market]:
    """Convert raw Gamma market dicts into Market objects."""
    markets = []
    for m in raw:
        if not m.get("active") or m.get("closed") or m.get("archived"):
            continue

        token_ids = _parse_list(m.get("clobTokenIds"))
        prices_raw = _parse_list(m.get("outcomePrices"))
        if len(token_ids) < 2 or len(prices_raw) < 2:
            continue

        price_yes = _safe_float(prices_raw[0])
        end_date = m.get("endDate", "")
        hrs = _hours_until(end_date)

        if hrs < 0 or hrs > max_hours:
            continue

        # Category from this market's own events tags if not already known
        cat = category
        if cat == "other" and m.get("events"):
            ev_tags = (m["events"][0].get("tags") or []) if m.get("events") else []
            cat = _category_from_tags(ev_tags)

        markets.append(Market(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", ""),
            token_id_yes=str(token_ids[0]),
            token_id_no=str(token_ids[1]),
            price_yes=price_yes,
            liquidity=_safe_float(m.get("liquidityNum") or m.get("liquidity")),
            volume=_safe_float(m.get("volumeNum") or m.get("volume")),
            volume_24h=_safe_float(m.get("volume24hr") or m.get("volume24hrClob") or 0),
            spread=_safe_float(m.get("spread")),
            end_date=end_date,
            hours_to_resolution=hrs,
            category=cat,
            event_title=event_title,
        ))
    return markets


def fetch_resolution(condition_id: str) -> Optional[str]:
    """
    Check whether a market has resolved, and if so which side won.

    Uses the CLOB single-market endpoint (the authoritative source), which
    exposes each outcome token with a `winner` boolean once UMA resolves it.
    Token order matches clobTokenIds: index 0 = YES, index 1 = NO — so we
    decide by INDEX, not by the human label ("Over"/"Yes"/a player name).

    Returns "YES", "NO", or None (not yet resolved / voided / unknown).
    """
    try:
        resp = requests.get(
            f"{config.CLOB_HOST}/markets/{condition_id}",
            timeout=15,
        )
        resp.raise_for_status()
        m = resp.json()
        tokens = m.get("tokens", [])
        if len(tokens) < 2:
            return None

        # Primary signal: the explicit winner flag.
        if tokens[0].get("winner"):
            return "YES"
        if tokens[1].get("winner"):
            return "NO"

        # Fallback: a closed market whose price has collapsed to a near-certain
        # outcome (>= 0.99 / <= 0.01) is effectively resolved even if the
        # winner flag hasn't propagated yet.
        if m.get("closed"):
            p_yes = _safe_float(tokens[0].get("price"))
            if p_yes >= 0.99:
                return "YES"
            if p_yes <= 0.01:
                return "NO"
        return None  # not resolved, or voided / 50-50
    except Exception:
        return None


def fetch_order_book_spread(token_id: str) -> Optional[float]:
    """Get live best-bid/best-ask spread from the CLOB for a token."""
    try:
        resp = requests.get(
            f"{config.CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None
        best_bid = max(_safe_float(b["price"]) for b in bids)
        best_ask = min(_safe_float(a["price"]) for a in asks)
        return round(best_ask - best_bid, 4)
    except Exception:
        return None
