"""
Central configuration. Everything you tune lives here or in the .env file.
Edit .env for secrets/keys; edit the strategy/category settings below for
trading behaviour.
"""
import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Secrets / connection (read from .env)
# ---------------------------------------------------------------------------
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
POLYGON_WALLET_PRIVATE_KEY = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "")

CLOB_HOST  = os.getenv("CLOB_HOST",  "https://clob.polymarket.com")
GAMMA_HOST = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CHAIN_ID   = int(os.getenv("CHAIN_ID", "137"))  # Polygon mainnet


# ---------------------------------------------------------------------------
# Mode
#   PAPER = simulate trades only, no money moves (SAFE default)
#   LIVE  = place real orders on Polymarket (needs funded wallet)
# ---------------------------------------------------------------------------
MODE = os.getenv("MODE", "PAPER").upper()


# ---------------------------------------------------------------------------
# Short-term market targeting
# ---------------------------------------------------------------------------

# Only look at markets that resolve within this many hours.
# 48h covers same-day and next-day: soccer, esports, tennis, nba.
MAX_HOURS_TO_RESOLUTION = float(os.getenv("MAX_HOURS", "48"))

# Market categories to scan. These map to tag slugs on Polymarket.
# Uncomment / add categories you want. Start with the fastest-resolving ones.
TARGET_CATEGORIES = [
    cat.strip()
    for cat in os.getenv("CATEGORIES",
        "soccer,esports,tennis,nba,ufc,crypto"
    ).split(",")
    if cat.strip()
]

# Daily profit target (for display/tracking only — the bot doesn't stop when hit).
# This is your goal; the report command will show how close you are each day.
DAILY_TARGET_USD = float(os.getenv("DAILY_TARGET", "500"))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
@dataclass
class StrategyConfig:
    """
    Per-category mean-reversion strategy with Kelly sizing.

    Each category can have its own edge/confidence thresholds because
    sports markets behave differently from crypto price markets.

    Key parameters:
      min_edge        - minimum (fair_prob - market_prob) to trigger a trade.
                        This guards against trading noise from the heuristic.
      min_confidence  - our estimated probability for the chosen side must
                        exceed this (so we only back sides we think are likely).
      kelly_fraction  - fraction of the full Kelly stake to bet (risk control).
      max_position_usd - hard cap per trade.
    """
    name: str           = "moderate"
    min_edge: float     = 0.08   # require 8% mispricing
    min_confidence: float = 0.55
    kelly_fraction: float = 0.50

    # Market quality filters
    min_liquidity_usd: float  = 1000.0   # allow thinner markets for short-term
    max_spread: float         = 0.08     # wider spread ok for fast-resolving
    min_price: float          = 0.04     # avoid near-resolved
    max_price: float          = 0.96

    # Position sizing
    bankroll_usd: float       = 100.0
    max_position_usd: float   = 5.0
    max_open_positions: int   = 10

    # Per-category overrides (applied on top of defaults above)
    # key = category string; value = dict of field overrides
    category_overrides: dict  = field(default_factory=dict)

    def for_category(self, category: str) -> "StrategyConfig":
        """Return a copy with this category's overrides applied."""
        overrides = self.category_overrides.get(category, {})
        if not overrides:
            return self
        import dataclasses
        return dataclasses.replace(self, **overrides)


# Three preset risk profiles. Aggressive is suitable for fast-resolving markets
# because more trades per day compensates for a lower per-trade threshold.
PROFILES = {
    "conservative": StrategyConfig(
        name="conservative",
        min_edge=0.15, min_confidence=0.65,
        kelly_fraction=0.25, max_position_usd=3.0,
        min_liquidity_usd=5000.0,
    ),
    "moderate": StrategyConfig(
        name="moderate",
        min_edge=0.08, min_confidence=0.55,
        kelly_fraction=0.50, max_position_usd=5.0,
        min_liquidity_usd=1000.0,
        # Esports and soccer often have 5-15% edges on sub-markets;
        # crypto weekly markets are more efficient so require a bigger edge.
        category_overrides={
            "esports": {"min_edge": 0.07, "min_liquidity_usd": 500.0, "max_spread": 0.12},
            "soccer":  {"min_edge": 0.08, "min_liquidity_usd": 1000.0},
            "tennis":  {"min_edge": 0.09, "min_liquidity_usd": 500.0},
            "crypto":  {"min_edge": 0.12, "min_liquidity_usd": 5000.0},
            "nba":     {"min_edge": 0.08, "min_liquidity_usd": 2000.0},
            "ufc":     {"min_edge": 0.10, "min_liquidity_usd": 500.0},
        },
    ),
    "aggressive": StrategyConfig(
        name="aggressive",
        min_edge=0.05, min_confidence=0.50,
        kelly_fraction=1.00, max_position_usd=10.0,
        min_liquidity_usd=500.0,
        max_spread=0.15,
    ),
}

PROFILE  = os.getenv("PROFILE", "moderate").lower()
STRATEGY = PROFILES.get(PROFILE, PROFILES["moderate"])

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
MARKET_LIMIT  = int(os.getenv("MARKET_LIMIT",  "30"))

# ---------------------------------------------------------------------------
# Longshot-fade strategy (from the calibration study: exact-score / spread
# longshots are systematically overpriced -> buy NO, spread across many).
# ---------------------------------------------------------------------------
LONGSHOT_STAKE_USD     = float(os.getenv("LONGSHOT_STAKE", "1.0"))   # small per-bet
LONGSHOT_MIN_LIQUIDITY = float(os.getenv("LONGSHOT_MIN_LIQ", "3000"))
LONGSHOT_MIN_EDGE      = float(os.getenv("LONGSHOT_MIN_EDGE", "0.06"))
LONGSHOT_MAX_BETS      = int(os.getenv("LONGSHOT_MAX_BETS", "20"))   # diversify

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trades.db")

# File where manual/Claude-in-the-loop probability estimates live.
# Format: {"<condition_id>": 0.62, ...}  (see `run.py export`)
ESTIMATES_PATH = os.path.join(os.path.dirname(__file__), "..", "estimates.json")
