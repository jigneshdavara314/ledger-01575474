"""
The brain. Decides which markets are mispriced and how much to bet.

Two parts:
  1. estimate_fair_probability()  -> what we think the TRUE probability is
  2. evaluate()                   -> turns an edge into a sized trade signal

Fair-probability estimation supports three engines:
  - "heuristic" : pure math, no API cost. Applies favorite-longshot-bias
                  correction (Berg & Rietz 2018) — markets overprice longshots
                  and underprice favorites. Only fires where the bias is large.
  - "openai"    : asks GPT-4o-mini to estimate the true probability.
  - "claude"    : asks Claude to reason about the market context, outcomes,
                  and any available base-rate information. Better for sports
                  and esports where context matters.
"""
from dataclasses import dataclass
from typing import Optional

from . import config
from .market_data import Market


@dataclass
class Signal:
    market: Market
    side: str             # "YES" or "NO"
    fair_prob: float      # our estimate of true probability of that side
    market_prob: float    # what the market charges for that side (our bid)
    edge: float           # fair_prob - market_prob (positive = profitable)
    size_usd: float       # how much to stake
    reason: str
    estimator: str        # "heuristic" | "openai" | "claude"
    # Live order-book context (optional) so the PAPER fill model can price a
    # resting sub-ask limit realistically (fills near the ASK, not the bid) instead
    # of a flat slippage guess. None -> fall back to bid + PAPER_SLIPPAGE.
    ask_price: float = None


# ---------------------------------------------------------------------------
# Fair probability estimators
# ---------------------------------------------------------------------------

def estimate_fair_probability_heuristic(price_yes: float) -> float:
    """
    Favorite-longshot-bias correction.
    Empirically, true probability is closer to 0.5 than the market price for
    extremes. We apply a 15% shrinkage toward 0.5. This is only meaningful
    when the price is already at an extreme (< 0.25 or > 0.75) — otherwise
    the produced edge is below our min_edge threshold and gets filtered out.
    """
    shrink = 0.15
    return price_yes + (0.5 - price_yes) * shrink


def _build_ai_prompt(market: Market) -> str:
    """Shared prompt template for both AI estimators."""
    return (
        "You are a calibrated forecaster for prediction markets. "
        "Your task: estimate the TRUE probability that the market question "
        "resolves YES, as a decimal between 0.0 and 1.0.\n\n"
        f"Market: {market.question}\n"
        f"Category: {market.category}\n"
        f"Event: {market.event_title}\n"
        f"Hours until resolution: {market.hours_to_resolution:.1f}\n"
        f"Current market price (implied probability of YES): {market.price_yes:.3f}\n\n"
        "Consider: recent form, base rates, market efficiency for this category. "
        "Be honest about uncertainty. "
        "Reply with ONLY a single number (e.g. 0.62). Nothing else."
    )


def estimate_fair_probability_openai(market: Market) -> Optional[float]:
    """Ask GPT-4o-mini to estimate the true probability of YES."""
    if not config.OPENAI_API_KEY:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": _build_ai_prompt(market)}],
            temperature=0.2,
            max_tokens=10,
        )
        text = resp.choices[0].message.content.strip()
        val = float(text.split()[0])
        return max(0.01, min(0.99, val))
    except Exception:
        return None


_MANUAL_CACHE = None  # lazily-loaded dict: condition_id -> fair_prob


def _load_manual_estimates() -> dict:
    """Load human/Claude-provided probability estimates from a JSON file.

    This powers the 'manual AI' mode: instead of calling a paid API, an AI
    (e.g. Claude in an interactive session) fills in estimates.json, and the
    bot reads them here. Format: {"<condition_id>": 0.62, ...}
    """
    global _MANUAL_CACHE
    if _MANUAL_CACHE is not None:
        return _MANUAL_CACHE
    import json, os
    path = config.ESTIMATES_PATH
    if not os.path.exists(path):
        _MANUAL_CACHE = {}
        return _MANUAL_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # accept either {"id": 0.6} or {"id": {"fair_prob": 0.6, ...}}
        cache = {}
        for cid, v in data.items():
            if cid.startswith("_"):
                continue  # skip _comment and other metadata keys
            if isinstance(v, dict):
                p = v.get("fair_prob")
            else:
                p = v
            try:
                if p is not None:
                    cache[cid] = max(0.01, min(0.99, float(p)))
            except (ValueError, TypeError):
                continue  # ignore non-numeric values
        _MANUAL_CACHE = cache
    except Exception:
        _MANUAL_CACHE = {}
    return _MANUAL_CACHE


def estimate_fair_probability_manual(market: Market) -> Optional[float]:
    """Look up a pre-computed estimate for this market (manual AI mode)."""
    return _load_manual_estimates().get(market.condition_id)


def estimate_fair_probability_claude(market: Market) -> Optional[float]:
    """
    Ask Claude to estimate the true probability of YES.
    Claude is particularly good at sports/esports markets because it can
    reason about team form, tournament context, and common market biases.
    Uses claude-haiku-4-5 (fast + cheap) for quick scanning.
    """
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        _kw = {"api_key": config.ANTHROPIC_API_KEY}
        if getattr(config, "ANTHROPIC_BASE_URL", ""):
            _kw["base_url"] = config.ANTHROPIC_BASE_URL
            _kw["default_headers"] = {
                "Authorization": f"Bearer {config.ANTHROPIC_API_KEY}"}
        client = anthropic.Anthropic(**_kw)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            temperature=0.2,
            messages=[{"role": "user", "content": _build_ai_prompt(market)}],
        )
        text = resp.content[0].text.strip()
        val = float(text.split()[0])
        return max(0.01, min(0.99, val))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Position sizing — fractional Kelly criterion
# ---------------------------------------------------------------------------

def kelly_size(fair_prob: float, market_prob: float, cfg) -> float:
    """
    Kelly fraction for a binary bet bought at price `market_prob`.
    Payout is 1.0 per share, cost is market_prob, so:
       b = (1 - market_prob) / market_prob   (net odds)
       f* = (b*p - q) / b   where p=fair_prob, q=1-p
    We then apply the configured kelly_fraction and the per-trade cap.
    """
    p = fair_prob
    q = 1.0 - p
    if market_prob <= 0 or market_prob >= 1:
        return 0.0
    b = (1.0 - market_prob) / market_prob
    f_star = (b * p - q) / b
    f_star = max(0.0, f_star) * cfg.kelly_fraction
    stake = f_star * cfg.bankroll_usd
    return round(min(stake, cfg.max_position_usd), 2)


# ---------------------------------------------------------------------------
# Main evaluation (category-aware)
# ---------------------------------------------------------------------------

def evaluate(
    market: Market,
    use_ai: bool = False,
    ai_engine: str = "claude",
) -> Optional[Signal]:
    """
    Evaluate a market for a trade signal.

    Args:
        market    : the Market to evaluate
        use_ai    : whether to use an AI estimator (slower, costs tokens)
        ai_engine : "claude" (default) or "openai"

    Returns a Signal if there's a tradeable edge, otherwise None.
    """
    # Get category-specific config overrides
    cfg = config.STRATEGY.for_category(market.category)

    # --- quality filters ---
    if market.liquidity < cfg.min_liquidity_usd:
        return None
    if not (cfg.min_price <= market.price_yes <= cfg.max_price):
        return None
    if market.spread and market.spread > cfg.max_spread:
        return None

    # --- estimate fair value ---
    fair_yes = None
    estimator = "heuristic"

    if use_ai:
        # 1. Manual AI estimates (from estimates.json) take top priority.
        #    This is the "Claude-in-the-loop, no API key" mode.
        fair_yes = estimate_fair_probability_manual(market)
        if fair_yes is not None:
            estimator = "manual"

        # 2. Claude API
        if fair_yes is None and ai_engine == "claude":
            fair_yes = estimate_fair_probability_claude(market)
            if fair_yes is not None:
                estimator = "claude"

        # 3. OpenAI API
        if fair_yes is None and config.OPENAI_API_KEY:
            fair_yes = estimate_fair_probability_openai(market)
            if fair_yes is not None:
                estimator = "openai"

    if fair_yes is None:
        fair_yes = estimate_fair_probability_heuristic(market.price_yes)
        estimator = "heuristic"

    # --- compute edge on both sides, pick the better one ---
    edge_yes = fair_yes - market.price_yes
    edge_no  = (1.0 - fair_yes) - market.price_no

    if edge_yes >= edge_no and edge_yes > 0:
        side, fair_p, mkt_p, edge = "YES", fair_yes, market.price_yes, edge_yes
    elif edge_no > 0:
        side, fair_p, mkt_p, edge = "NO", 1.0 - fair_yes, market.price_no, edge_no
    else:
        return None

    # Edge gate: the mispricing must be big enough to be worth trading.
    if edge < cfg.min_edge:
        return None

    # Confidence gate: only bet a side we actually think is likely to win.
    if fair_p < cfg.min_confidence:
        return None

    size = kelly_size(fair_p, mkt_p, cfg)
    if size <= 0:
        return None

    reason = (
        f"{cfg.name}/{market.category}: edge={edge:.3f}, "
        f"fair={fair_p:.2f} vs mkt={mkt_p:.2f} [{estimator}]"
    )
    return Signal(
        market=market,
        side=side,
        fair_prob=round(fair_p, 4),
        market_prob=round(mkt_p, 4),
        edge=round(edge, 4),
        size_usd=size,
        reason=reason,
        estimator=estimator,
    )
