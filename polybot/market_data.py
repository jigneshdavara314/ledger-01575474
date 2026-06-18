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
    event_slug: str = ""      # Polymarket event slug, for building a view link

    @property
    def polymarket_url(self) -> str:
        """Link to view this market on polymarket.com (event page)."""
        if self.event_slug:
            return f"https://polymarket.com/event/{self.event_slug}"
        return "https://polymarket.com/markets"

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
    if any(s in tag_set for s in ["hockey", "nhl"]):
        return "nhl"
    if any(s in tag_set for s in ["ufc", "mma"]):
        return "ufc"
    if any(s in tag_set for s in ["boxing"]):
        return "boxing"
    if any(s in tag_set for s in ["cricket", "ipl"]):
        return "cricket"
    if any(s in tag_set for s in ["golf", "pga", "pga-tour"]):
        return "golf"
    if any(s in tag_set for s in ["f1", "formula-1", "formula-one", "motorsport"]):
        return "f1"
    if any(s in tag_set for s in ["crypto", "bitcoin", "ethereum", "weekly",
                                   "multi-strikes", "hit-price", "crypto-prices"]):
        return "crypto"
    if "tweets-markets" in tag_set:
        return "tweets"
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
    max_event_pages: int = 4,
) -> List[Market]:
    """
    Fetch markets that resolve within `max_hours` hours, filtered to the
    specified categories. Uses the events API to get tag metadata.

    max_event_pages: how many 200-event pages to pull (by 24h volume). More pages
    reach lower-volume markets like player props (the home-run edge) at the cost
    of a few extra API calls.

    This is the main scanner for the short-term strategy.
    """
    max_hours = max_hours or config.MAX_HOURS_TO_RESOLUTION
    categories = categories or config.TARGET_CATEGORIES

    # Paginate through events so LOW-VOLUME markets (e.g. baseball player props
    # like Home-Run O/U) are reachable too — they fall well outside the top 200
    # by 24h volume, so a single page would silently miss confirmed edges.
    url = f"{config.GAMMA_HOST}/events"
    events = []
    for _offset in range(0, max_event_pages * 200, 200):
        resp = requests.get(url, params={
            "active": "true", "closed": "false", "limit": 200,
            "offset": _offset, "order": "volume24hr", "ascending": "false",
        }, timeout=30)
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        events.extend(batch)
        if len(batch) < 200:
            break

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

        # Category + event slug from this market's own events if not already known
        cat = category
        ev_slug = ""
        if m.get("events"):
            ev0 = m["events"][0]
            ev_slug = ev0.get("slug", "") or ""
            if cat == "other":
                cat = _category_from_tags(ev0.get("tags") or [])

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
            event_slug=ev_slug,
        ))
    return markets


def fetch_resolution(condition_id: str) -> Optional[str]:
    """
    Check whether a market has resolved, and if so which side won.

    Uses the CLOB single-market endpoint (the authoritative source), which
    exposes each outcome token with a `winner` boolean once UMA resolves it.
    Token order matches clobTokenIds: index 0 = YES, index 1 = NO — so we
    decide by INDEX, not by the human label ("Over"/"Yes"/a player name).

    Returns:
      "YES"  - YES side won
      "NO"   - NO side won
      "VOID" - market cancelled/refunded (50-50): no winner, stake returned
      None   - not yet resolved / fetch failed (unknown)
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

        # VOID / cancelled market. When a market is closed but NEITHER token won
        # (Polymarket flags is_50_50_outcome and parks both prices at 0.5), the
        # event was annulled (e.g. an e-sports map that was never played) and all
        # stakes are REFUNDED. Without this, such positions never get a winner and
        # sit OPEN forever — the "stuck for days" bug. Settle as VOID -> refund.
        if m.get("closed"):
            p_yes = _safe_float(tokens[0].get("price"))
            p_no = _safe_float(tokens[1].get("price"))
            if m.get("is_50_50_outcome"):
                return "VOID"
            # Fallback: closed, neither winner, both prices ~0.5 = de-facto void.
            if 0.4 <= p_yes <= 0.6 and 0.4 <= p_no <= 0.6:
                return "VOID"
            # A closed market whose price collapsed to a near-certain outcome is
            # effectively resolved even if the winner flag hasn't propagated yet.
            if p_yes >= 0.99:
                return "YES"
            if p_yes <= 0.01:
                return "NO"
        return None  # genuinely not resolved yet
    except Exception as e:
        # Distinguish FETCH FAILURE from "not resolved": a silent None here would
        # let a persistent API/schema break freeze every position OPEN forever and
        # look like quiet markets. Surface it so the resolver/operator can see it.
        print(f"[fetch_resolution] WARNING: could not fetch {condition_id}: {e} "
              f"(treating as unresolved this cycle — NOT a confirmed no-resolution)")
        return None


def fetch_quote(token_id: str) -> Optional[dict]:
    """
    Return the live order-book quote for a token:
      {best_bid, best_ask, mid, spread}
    or None if the book is empty/thin. `mid` is the midpoint we can place a
    limit order at to try to buy below the ask (better price if it fills).
    """
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
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None
        return {
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
            "mid": round((best_bid + best_ask) / 2, 4),
            "spread": round(best_ask - best_bid, 4),
        }
    except Exception:
        return None


def stable_unit(key: str) -> float:
    """
    Deterministic uniform [0,1) draw from a string key, using a STABLE hash
    (hashlib) — unlike Python's built-in hash(), which is salted per process
    (PYTHONHASHSEED) and so is NOT reproducible across runs. Used to simulate
    paper fills reproducibly: the same market always rolls the same number.
    """
    import hashlib
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return (int(h[:8], 16) % 1_000_000) / 1_000_000.0


def fillable_depth(token_id: str, max_price: float) -> Optional[dict]:
    """
    How much capital (USD) can realistically be deployed buying this token at or
    below `max_price`, reading the live ask side of the order book.

    Returns {usd, shares, levels, best_ask} — the cumulative USD and shares
    available at prices <= max_price, i.e. what you could fill WITHOUT walking
    the book up to bad prices. None if the book is empty.

    This is the honest size ceiling for a market: betting more than `usd` here
    means paying worse prices and eroding your edge.
    """
    try:
        resp = requests.get(
            f"{config.CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        asks = resp.json().get("asks", [])
        if not asks:
            return None
        asks_sorted = sorted(asks, key=lambda a: _safe_float(a["price"]))
        best_ask = _safe_float(asks_sorted[0]["price"])
        cum_usd = 0.0
        cum_shares = 0.0
        levels = 0
        for a in asks_sorted:
            p = _safe_float(a["price"])
            sz = _safe_float(a["size"])
            if p > max_price:
                break
            cum_usd += p * sz
            cum_shares += sz
            levels += 1
        return {
            "usd": round(cum_usd, 2),
            "shares": round(cum_shares, 2),
            "levels": levels,
            "best_ask": best_ask,
        }
    except Exception:
        return None


def limit_bid_price(token_id: str, aggression: float = 0.5,
                    hours_to_res: float = None) -> Optional[dict]:
    """
    Compute a limit-buy price between the midpoint and the ask.

    aggression in [0,1]:
      0.0 = bid at the midpoint (best price, lowest fill chance)
      1.0 = bid at the ask      (worst price, fills immediately)
      0.5 = halfway between mid and ask (default: a bit better than ask,
            still reasonably likely to fill)

    hours_to_res: time until the market resolves. A resting limit below the ask
      has MORE chances to be crossed the longer it sits, so more time -> higher
      fill probability. This is the dominant real-world driver and was previously
      missing from the estimate.

    Returns {price, mid, ask, bid, spread, fill_prob_estimate} or None.
    """
    q = fetch_quote(token_id)
    if not q:
        return None
    mid, ask, bid = q["mid"], q["best_ask"], q["best_bid"]
    price = round(mid + (ask - mid) * aggression, 4)
    price = max(bid + 0.001, min(ask, price))  # stay inside the book

    # HONEST fill-probability estimate. A BUY limit only executes when a seller
    # is willing to trade at or below our price:
    #   - at/above the ASK  -> we cross the spread and fill ~immediately (prob~1)
    #   - between bid and ask -> we must wait for a seller to cross DOWN to us.
    #     The closer our price sits to the current best BID, the less likely a
    #     seller gives us that price within our (short, pre-resolution) window.
    # We model this as the price's position in the [bid, ask] band, but NOT the
    # over-generous "0.45 floor at mid" the audit flagged: a resting buy well
    # below the ask is genuinely uncertain. Position 0 (at bid) -> ~0.10,
    # position 1 (at ask) -> ~0.97, roughly linear. A wider spread also lowers
    # the chance of a cross, so we shade down on large spreads.
    band = ask - bid
    if band > 1e-6:
        pos = (price - bid) / band           # 0 at bid, 1 at ask
    else:
        pos = 1.0                              # no spread -> crossing fills
    base = 0.10 + 0.87 * pos
    spread_penalty = min(0.25, max(0.0, (band - 0.01) * 2.0))  # wide spread = harder

    # TIME-TO-RESOLUTION: a resting sub-ask limit has more chances to be crossed
    # the longer it sits. Scale a below-ask bid's fill chance up with available
    # time (saturating ~48h); a crossing bid (pos~1) is unaffected. None -> neutral.
    time_factor = 1.0
    if hours_to_res is not None and pos < 0.999:
        import math
        # 0 at no time, ~1 by ~48h; multiplies the "uncertain" portion of base.
        avail = 1.0 - math.exp(-max(0.0, hours_to_res) / 18.0)
        time_factor = 0.55 + 0.45 * avail     # never below 0.55 of base
    fill_prob = round(max(0.05, min(0.99, base * time_factor - spread_penalty)), 3)

    return {
        "price": price, "mid": mid, "ask": ask, "bid": bid,
        "spread": q["spread"], "fill_prob_estimate": fill_prob,
    }
