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
                " btc ", " eth ", " xrp", "cardano", "litecoin",
                "above $", "below $", "hit $", "price of", "reach $", "trade above"]


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
    # Esports "any player <feat>" — must precede player_prop (which greedily
    # matches the substring "player"). Validated esports_any_player_feat edge.
    if re.search(r"(map|game) \d+:?\s*any player "
                 r"(rampage|ultra kill|godlike|ace|first blood)", ql):
        return "esports_any_player_feat"
    # Player / esports props BEFORE generic over_under so "Home Runs O/U" is a
    # player_prop (specific), not lumped with team totals.
    #   - US-sport props: "home runs", "strikeouts", "passing yards", ...
    #   - Soccer player props: "<Name>: N+ goals|assists|shots|saves|tackles|
    #     passes" (and "goals + assists"). These were landing in 'other' — the
    #     archive shows fading them (buy NO) at NO 0.55-0.75 wins ~93% (the
    #     discovered 'other | pay NO 0.55-0.75' edge is really player props).
    if any(k in ql for k in ("home runs", "strikeouts", "passing yards",
                             "to record", "player")):
        return "player_prop"
    if re.search(r":\s*\d+\+\s*(goal|assist|shot|save|tackle|pass|block|"
                 r"clearance|interception|point|rebound|three)", ql):
        return "player_prop"
    # Esports props. NOTE: "rounds" alone is NOT esports — UFC/boxing fights have
    # "Total Rounds O/U", which previously leaked to esports_prop and borrowed its
    # edge rate (a fabricated cross-sport edge). Gate "rounds"/"kills" to an esports
    # context (map/game/cs2/valorant), and keep the unambiguous esports tokens.
    if (any(k in ql for k in ("first blood", " cs2", "counter-strike", "valorant",
                              "league of legends", " dota")) or
            re.search(r"\bmap \d", ql) or
            re.search(r"(map|game) \d[^?]*\b(rounds?|kills?)\b", ql)):
        return "esports_prop"
    if any(k in ql for k in ("posts from", "posts between", "tweets", "mentions")):
        return "tweet_range"
    if re.search(r"o/u\s*\d", ql) or "over/under" in ql:
        return "over_under"
    if "moneyline" in ql or " to win" in ql or re.search(r"\bwin\b", ql):
        return "moneyline"
    if "to advance" in ql or "advance" in ql:
        return "to_advance"
    if "to qualify" in ql or "qualify to" in ql or "qualify for" in ql:
        return "to_qualify"
    # Esports in-game OBJECTIVE props (recurring, structured) — carved out of the
    # old 'other' grab-bag where ~26% of markets were landing invisibly.
    if any(k in ql for k in ("slay a dragon", "slay baron", "beat roshan",
                             "destroy inhibitor", "destroy barrack",
                             "both teams slay", "both teams destroy",
                             "ends in daytime")):
        return "esports_objective"
    if "both teams to score" in ql or "both teams score" in ql or " btts" in ql:
        return "both_teams_score"
    # ITF / lower-tier tennis match winner markets (very high daily volume).
    if "itf " in ql or "completed match:" in ql:
        return "tennis_match"
    if any(k in ql for k in ("method of victory", " by ko", "by decision",
                             "by submission", " by tko")):
        return "method_of_victory"
    if "winning margin" in ql or "margin of victory" in ql:
        return "winning_margin"
    # WEATHER / temperature markets ("Will the highest/lowest temperature ..."):
    # resolve daily, and the favorite-longshot fade is STRONG + entry-price/OOS
    # confirmed (n~76k, both halves +EV across NO 0.55-0.85). A genuine recurring
    # edge — carved out of 'other' so it's bettable.
    if any(k in ql for k in ("highest temperature", "lowest temperature",
                             "high temperature", "temperature in")):
        return "weather_temp"
    # --- Entry-price + OOS validated families (2026-06-24 archive hunt) ---
    # AI "best/top model by date" cohorts (~13-15 firms, ~89% NO base). EV +0.266.
    if re.search(r"have the (best|top) (coding )?ai model (at the end of|on) ", ql):
        return "ai_best_model_by_date"
    # Approval/disapproval rating narrow bands. EV +0.240.
    if re.search(r"(approval|disapproval) rating "
                 r"(be (between|greater than|[0-9.]+% or)|of)", ql):
        return "approval_rating_band"
    # (esports_any_player_feat handled earlier, before player_prop)
    # Geopolitical "will <country> strike <target> by date" — overpriced longshots.
    # Country-gated so it doesn't catch bowling/labor "strike". EV +0.229.
    if re.search(r"will (the us|the u\.s\.|israel|iran|russia|ukraine|india|"
                 r"pakistan)[^?]* strike ", ql):
        return "geopolitical_strike_event"
    # Company "beat quarterly earnings" — MARGINAL (probationary, small stake).
    if re.search(r"\([a-z]+\) beat quarterly earnings|"
                 r"will [a-z .&]+ beat quarterly earnings", ql):
        return "company_beat_quarterly_earnings"
    # Novelty "will X say <QUOTED phrase>" — a SPECIFIC, validated subtype (EV
    # +0.104, n=1693). MUST precede generic novelty_says below.
    if ql.startswith("will ") and " say " in ql and '"' in ql:
        return "politician_say_phrase"
    if "winner" in ql or "champion" in ql:
        return "outright_winner"
    if "draw" in ql:
        return "draw"
    # Novelty "will X say/happen" markets — classified (so they're TESTABLE) but
    # likely noise; the gate will reject them unless they prove a real edge.
    if ql.startswith("will ") and (" say " in ql or " said " in ql):
        return "novelty_says"
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
    "weather_temp": ["highest temperature", "lowest temperature",
                     "high temperature", "temperature in"],
    # Entry-price+OOS validated families (2026-06-24):
    "ai_best_model_by_date": ["best ai model", "top ai model",
                              "best coding ai model", "top coding ai model"],
    "approval_rating_band": ["approval rating", "disapproval rating"],
    "esports_any_player_feat": ["any player rampage", "any player ultra kill",
                                "any player godlike", "any player ace",
                                "any player first blood"],
    "geopolitical_strike_event": [" strike "],  # country-gated in family_of
    "company_beat_quarterly_earnings": ["beat quarterly earnings"],
    "politician_say_phrase": ['say "'],  # quoted-phrase gated in family_of
    "tweet_range": ["posts from", "posts between", "tweets", "mentions"],
    "player_prop": ["home runs", "strikeouts", "passing yards", "to record", "player",
                    "+ goals", "+ assists", "+ shots", "+ saves", "+ tackles",
                    "+ passes", "+ blocks", "+ goals + assists", "+ points",
                    "+ rebounds", "+ threes"],
    "esports_prop": ["map ", "rounds", "first blood", "kills", " cs2", "valorant"],
    # Newly carved out of 'other' — bettable structured families:
    "to_qualify": ["to qualify", "qualify to", "qualify for"],
    "esports_objective": ["slay a dragon", "slay baron", "beat roshan",
                          "destroy inhibitor", "destroy barrack",
                          "both teams slay", "both teams destroy", "ends in daytime"],
    "both_teams_score": ["both teams to score", "both teams score", "btts"],
    "tennis_match": ["itf ", "completed match:"],
    "method_of_victory": ["method of victory", "by ko", "by decision",
                          "by submission", "by tko"],
    "winning_margin": ["winning margin", "margin of victory"],
    # novelty_says IS a validated edge now (archive-confirmed) — bridge it so a
    # promoted novelty cell can match live markets.
    "novelty_says": ["will he say", "will she say", "will trump say", " say ",
                     " said ", " tweet"],
    # NOTE: crypto_pricetail is DELIBERATELY ABSENT — proven coin-flip, never bet.
}
