"""
Multi-strategy tournament — 5 DISTINCT strategy logics run in parallel, each on
its own bankroll, so live P&L decides which actually earns (no guessing a
threshold). Every strategy is still +EV-gated; they differ in THESIS, not just a
number nudge.

  conservative_fade : the PROVEN longshot-fade, strict gate (0.06). LOCKED to the
                      proven family set — new/experimental edges can NEVER leak in.
  aggressive_fade   : same proven families, looser gate (0.04) + wider band —
                      more bets, smaller edge. Also locked to proven families.
  confirmed_only    : ONLY confirmed families (exact_score), full stake — purest.
  promoted_only     : ONLY self-discovered/promoted families (rides the hunt's new
                      edges) — tests whether auto-discovery actually pays.
  experimental_fade : ONLY newly-added, not-yet-proven edges (tweet_range,
                      esports_prop, method_of_victory, player_prop). Isolated on
                      its OWN bankroll so a new edge proves itself here WITHOUT
                      touching the proven strategies' books.
  lagwatch_only     : resolution-lag arbitrage only (no longshot fade) — a totally
                      different mechanism, isolated so its P&L is measurable.

Each runs from its own bankroll (polybot.bankroll multi-book) and tags its trades
with `strategy=<name>` so the dashboard can rank them.

STRATEGY ISOLATION (so new edges can't affect existing ones):
  - `families_allow`: whitelist — the strategy bets ONLY these families.
  - `families_deny` : blacklist — the strategy NEVER bets these families.
  When you validate a NEW edge, it goes in EXPERIMENTAL_FAMILIES (consumed only by
  experimental_fade). It is added to PROVEN_FAMILIES — and thus to the main
  strategies — ONLY as a deliberate promotion after it earns its own live record.
"""

# The families the PROVEN strategies are allowed to bet. Adding an edge here is a
# DELIBERATE promotion (it then affects conservative/aggressive). Everything else
# stays out of the main books. exact_score/draw/novelty_says are the long-standing
# validated set; over_under YES rides through the promoted-tier path separately.
PROVEN_FAMILIES = ("exact_score", "draw", "novelty_says", "over_under")

# Newly entry-price+OOS-validated edges that are REAL but young: isolated to the
# experimental strategy until they build their own live settled track record.
# (Promote into PROVEN_FAMILIES only after `run.py confidence` shows them READY.)
EXPERIMENTAL_FAMILIES = ("tweet_range", "esports_prop", "method_of_victory",
                         "player_prop", "weather_temp",
                         # 2026-06-24 entry-price+OOS validated batch:
                         "ai_best_model_by_date", "approval_rating_band",
                         "esports_any_player_feat", "geopolitical_strike_event",
                         "politician_say_phrase", "company_beat_quarterly_earnings")

# name -> config. `kind` selects the betting mechanism; the rest are knobs.
STRATEGIES = {
    "conservative_fade": {
        "kind": "fade", "min_edge": 0.06, "band_lo": 0.10, "band_hi": 0.55,
        "tiers": ("confirmed", "exploratory", "trial"),
        "families_allow": PROVEN_FAMILIES,
        "blurb": "Proven longshot-fade, strict gate (proven families only)",
    },
    "aggressive_fade": {
        "kind": "fade", "min_edge": 0.04, "band_lo": 0.07, "band_hi": 0.60,
        "tiers": ("confirmed", "exploratory", "trial"),
        "families_allow": PROVEN_FAMILIES,
        "blurb": "Looser gate + wider band, proven families only",
    },
    "confirmed_only": {
        "kind": "fade", "min_edge": 0.06, "band_lo": 0.10, "band_hi": 0.55,
        "tiers": ("confirmed",),
        "families_allow": ("exact_score",),
        "blurb": "Only artifact-backed confirmed families, full stake",
    },
    "promoted_only": {
        "kind": "fade", "min_edge": 0.06, "band_lo": 0.10, "band_hi": 0.60,
        "tiers": ("trial", "exploratory"), "promoted_only": True,
        "blurb": "Only self-discovered/promoted edges (tests the hunt)",
    },
    "experimental_fade": {
        "kind": "fade", "min_edge": 0.06, "band_lo": 0.10, "band_hi": 0.60,
        "tiers": ("confirmed", "exploratory", "trial"),
        "families_allow": EXPERIMENTAL_FAMILIES,
        "blurb": "ONLY new/unproven edges — isolated so they can't affect proven books",
    },
    "lagwatch_only": {
        "kind": "lagwatch",
        "blurb": "Resolution-lag arbitrage only (different mechanism)",
    },
}

# The starting deposit for EACH strategy's own bankroll (paper).
PER_STRATEGY_DEPOSIT = 500.0

# The conservative one inherits the existing single-book history (so we don't
# orphan the real trades already placed). Others start fresh.
DEFAULT_STRATEGY = "conservative_fade"


def names():
    return list(STRATEGIES.keys())


def get(name):
    return STRATEGIES.get(name)
