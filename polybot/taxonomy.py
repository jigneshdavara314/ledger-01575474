"""
Market classification — the SINGLE source of truth for how a question maps to a
"family", and which families the live scanner is allowed to bet.

Previously family_of() was duplicated in edge_scan15 and edge_hunt with divergent
rules, and the live scanner's family->keyword map lived separately in longshot —
so a label could drift in one place and silently stop a promoted edge from ever
placing a bet. Centralising here removes that drift; one test enforces agreement.
"""
import re

# CRYPTO CONTAMINATION GUARD: crypto up/down (and hit-price strikes) are a PROVEN
# coin flip (no edge). They are quarantined to their own family that the live
# scanner NEVER bets, so they can't leak into a bettable bucket.
CRYPTO_HINTS = ["up or down", "bitcoin", "ethereum", "dogecoin", "solana",
                " btc ", " eth ", "above $", "below $", "hit $", "price of"]


def family_of(question: str) -> str:
    """Canonical family classification used by discovery (edge_scan15, edge_hunt)
    AND by the live scanner's keyword bridge. One definition, no drift."""
    ql = (question or "").lower()
    # Crypto first — hard quarantine before any other classification.
    if any(h in ql for h in CRYPTO_HINTS):
        return "crypto_pricetail"            # quarantined; never bet
    if "exact score" in ql:
        return "exact_score"
    if "spread" in ql or "handicap" in ql:
        return "spread_handicap"
    # Player / esports props BEFORE generic over_under so "Home Runs O/U" is a
    # player_prop (specific), not lumped with team totals.
    if any(k in ql for k in ("home runs", "strikeouts", "passing yards",
                             "to record", "player")):
        return "player_prop"
    if any(k in ql for k in ("map ", "rounds", "first blood", "kills",
                             " cs2", "valorant")):
        return "esports_prop"
    if any(k in ql for k in ("posts from", "posts between", "tweets", "mentions")):
        return "tweet_range"
    if re.search(r"o/u\s*\d", ql) or "over/under" in ql:
        return "over_under"
    if "moneyline" in ql or " to win" in ql or re.search(r"\bwin\b", ql):
        return "moneyline"
    if "to advance" in ql or "advance" in ql:
        return "to_advance"
    if "winner" in ql or "champion" in ql:
        return "outright_winner"
    if "draw" in ql:
        return "draw"
    return "other"


# Family -> the question-text keywords the LIVE scanner matches a market by, so a
# self-promoted family actually gets scanned and bet. crypto_pricetail is
# DELIBERATELY ABSENT — it must never be bettable.
FAMILY_KEYWORDS = {
    "over_under": ["o/u", "over/under"],
    "spread_handicap": ["spread", "handicap"],
    "exact_score": ["exact score"],
    "moneyline": [" to win", "moneyline"],
    "to_advance": ["to advance", "advance"],
    "outright_winner": ["winner", "champion"],
    "draw": ["draw"],
    "tweet_range": ["posts from", "posts between", "tweets", "mentions"],
    "player_prop": ["home runs", "strikeouts", "passing yards", "to record", "player"],
    "esports_prop": ["map ", "rounds", "first blood", "kills", " cs2", "valorant"],
}
