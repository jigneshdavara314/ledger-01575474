"""
Multi-strategy tournament — 5 DISTINCT strategy logics run in parallel, each on
its own bankroll, so live P&L decides which actually earns (no guessing a
threshold). Every strategy is still +EV-gated; they differ in THESIS, not just a
number nudge.

  conservative_fade : the proven longshot-fade, strict gate (0.06), confirmed band.
  aggressive_fade   : same fade but looser gate (0.04) + wider band — catches thin
                      edges the conservative one skips. More bets, smaller edge.
  confirmed_only    : ONLY confirmed families (exact_score, the artifact-backed
                      edges), full stake, ignores exploratory/promoted — purest.
  promoted_only     : ONLY self-discovered/promoted families (rides the hunt's new
                      edges) — tests whether auto-discovery actually pays.
  lagwatch_only     : resolution-lag arbitrage only (no longshot fade) — a totally
                      different mechanism, isolated so its P&L is measurable.

Each runs from its own bankroll (polybot.bankroll multi-book) and tags its trades
with `strategy=<name>` so the dashboard can rank them.
"""

# name -> config. `kind` selects the betting mechanism; the rest are knobs.
STRATEGIES = {
    "conservative_fade": {
        "kind": "fade", "min_edge": 0.06, "band_lo": 0.10, "band_hi": 0.55,
        "tiers": ("confirmed", "exploratory", "trial"),
        "blurb": "Proven longshot-fade, strict gate",
    },
    "aggressive_fade": {
        "kind": "fade", "min_edge": 0.04, "band_lo": 0.07, "band_hi": 0.60,
        "tiers": ("confirmed", "exploratory", "trial"),
        "blurb": "Looser gate + wider band — more bets, thinner edges",
    },
    "confirmed_only": {
        "kind": "fade", "min_edge": 0.06, "band_lo": 0.10, "band_hi": 0.55,
        "tiers": ("confirmed",),
        "blurb": "Only artifact-backed confirmed families, full stake",
    },
    "promoted_only": {
        "kind": "fade", "min_edge": 0.06, "band_lo": 0.10, "band_hi": 0.60,
        "tiers": ("trial", "exploratory"), "promoted_only": True,
        "blurb": "Only self-discovered/promoted edges (tests the hunt)",
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
