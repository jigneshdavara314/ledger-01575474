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
# Optional Anthropic-compatible gateway (e.g. cloudeapi.omeecron.cloud, a drop-in
# for the Anthropic Messages API). If set, the anthropic SDK is pointed here — no
# other code changes needed. Used ONLY for family-classification (names/groups
# patterns), never to estimate a probability or decide a bet.
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "")
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
# LIVE-TRADING SAFETY RAILS (only consulted when MODE=LIVE). These are HARD
# ceilings for a small, controlled real-money trial — defaults are deliberately
# tiny so flipping MODE=LIVE without setting them can only ever risk a few
# dollars per bet, never the whole wallet. All fail CLOSED (a bet that would
# exceed a cap is skipped, never silently shrunk past the floor and forced).
#   LIVE_MAX_BET_USD     : absolute hard cap on a single real order, in USDC.
#   LIVE_MAX_DAILY_USD   : max total real USDC the bot may stake across one UTC day.
#   LIVE_KILL_SWITCH     : set "1"/"true" to BLOCK all real orders instantly,
#                          regardless of MODE (an out-of-band emergency stop).
LIVE_MAX_BET_USD   = float(os.getenv("LIVE_MAX_BET_USD", "5"))      # $5/bet trial cap
LIVE_MAX_DAILY_USD = float(os.getenv("LIVE_MAX_DAILY_USD", "25"))   # $25/day trial cap
LIVE_KILL_SWITCH   = os.getenv("LIVE_KILL_SWITCH", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Short-term market targeting
# ---------------------------------------------------------------------------

# Only look at markets that resolve within this many hours.
# 48h covers same-day and next-day: soccer, esports, tennis, nba.
MAX_HOURS_TO_RESOLUTION = float(os.getenv("MAX_HOURS", "120"))

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
# --- Budget-based sizing ---
# Total capital to deploy per day across ALL longshot bets. The per-bet stake is
# derived from this (budget / expected number of bets), scaled by tier confidence
# and capped by real order-book depth. THIS is the main knob — set it to your
# daily budget and the bot sizes each bet sensibly.
#
# Read from settings.json (adjustable from the dashboard / one edit) so you can
# scale your daily investment up later without code changes. Env var DAILY_BUDGET
# still overrides for one-off runs. Use config.daily_budget() for a LIVE value
# that reflects edits made during a running process (e.g. the local server).
def daily_budget() -> float:
    from .settings import daily_budget as _db
    return _db()

DAILY_BUDGET_USD = daily_budget()

# Legacy flat per-bet stake — used only as a fallback / floor. The budget logic
# above normally overrides this.
LONGSHOT_STAKE_USD     = float(os.getenv("LONGSHOT_STAKE", "10.0"))
LONGSHOT_MIN_LIQUIDITY = float(os.getenv("LONGSHOT_MIN_LIQ", "3000"))
LONGSHOT_MIN_EDGE      = float(os.getenv("LONGSHOT_MIN_EDGE", "0.06"))
# FRICTION MODEL (honest paper P&L). Real execution is worse than the quoted bid:
#   - PAPER_SLIPPAGE: average price you actually pay ABOVE your bid (adverse
#     selection — resting limits fill disproportionately when the market moves
#     against you). Added to the entry NO price in paper.
#   - PAPER_FEE_FRAC: round-trip fee/gas as a fraction of stake, deducted from
#     P&L at settlement. Polymarket has no per-trade fee today, but gas + spread
#     decay justify a small non-zero default so paper isn't free-money optimistic.
PAPER_SLIPPAGE  = float(os.getenv("PAPER_SLIPPAGE", "0.01"))   # +1 cent worse fill
PAPER_FEE_FRAC  = float(os.getenv("PAPER_FEE_FRAC", "0.01"))   # 1% round-trip drag
LONGSHOT_MAX_BETS      = int(os.getenv("LONGSHOT_MAX_BETS", "40"))   # diversify
# Mid-price bidding: 0.0 = bid at midpoint (best price, may not fill),
# 1.0 = bid at ask (fills immediately). 0.4 leans toward a better price.
# 0.6 (closer to ask) recovers the ~35-60% of qualified +EV bets that 0.4 dropped
# as "not filled". SAFE: edge is recomputed against the actual bid and must still
# clear LONGSHOT_MIN_EDGE, so a worse bid that erases the edge auto-vetoes itself
# — raising aggression can never create a -EV fill, only forgo a little price
# improvement on bets that stay +EV. (Not 1.0: pure ask-taking discards all edge.)
LONGSHOT_BID_AGGRESSION = float(os.getenv("LONGSHOT_BID_AGG", "0.6"))

# Realistic-fill sizing: the actual stake is capped at the order-book depth
# available within LONGSHOT_FILL_TOLERANCE of the best ask, so we never "bet"
# more than the thin market can absorb near a good price.
LONGSHOT_FILL_TOLERANCE = float(os.getenv("LONGSHOT_FILL_TOL", "0.02"))  # 2 cents
LONGSHOT_MIN_STAKE      = float(os.getenv("LONGSHOT_MIN_STAKE", "1.0"))  # skip if thinner
# Per-bet hard cap as a fraction of the daily budget (risk control — never put
# more than this share of the day's money on one market).
LONGSHOT_MAX_BET_FRAC   = float(os.getenv("LONGSHOT_MAX_BET_FRAC", "0.20"))  # 20%
# Correlation control: the many sub-markets of ONE match (every exact-score line,
# every spread) resolve together, so they are NOT independent diversification.
# Cap how many bets we place on a single event.
LONGSHOT_MAX_PER_EVENT  = int(os.getenv("LONGSHOT_MAX_PER_EVENT", "3"))
# Cumulative daily-spend governor: the scan runs many times per day (every 15 min
# in the cloud). Without this, the per-run budget is re-applied each run. This
# caps TOTAL stake deployed across ALL of today's runs to the daily budget.
LONGSHOT_DAILY_SPEND_CAP = os.getenv("LONGSHOT_DAILY_SPEND_CAP", "1") == "1"
# Drawdown circuit breaker: stop opening NEW bets if total equity has fallen
# below this fraction of PEAK equity (ratcheted ruin guard). 0.70 = halt at -30%.
DRAWDOWN_HALT_FRAC = float(os.getenv("DRAWDOWN_HALT_FRAC", "0.70"))
# Aggregate open-exposure ceiling: total live stake across all open bets may not
# exceed this fraction of equity, limiting correlated tail clusters.
AGG_EXPOSURE_FRAC = float(os.getenv("AGG_EXPOSURE_FRAC", "0.60"))

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "trades.db")

# File where manual/Claude-in-the-loop probability estimates live.
# Format: {"<condition_id>": 0.62, ...}  (see `run.py export`)
ESTIMATES_PATH = os.path.join(os.path.dirname(__file__), "..", "estimates.json")
