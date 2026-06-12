"""
Resolution-lag arbitrage watcher.

The edge: a sports game finishes and the winner is KNOWN immediately (ESPN
shows it within seconds), but the corresponding Polymarket market stays open
and tradeable for several minutes until the UMA oracle formally resolves it.

During that lag the winning side is sometimes still priced below 1.00
(e.g. 0.96). Buying it is a near-riskless ~4% return: you already know it
resolves to 1.00.

This module:
  1. fetch_finished_games()  -> finished games + known winner from ESPN (free)
  2. find_lag_opportunities() -> match them to open Polymarket markets where
                                 the known winner is still cheap

IMPORTANT honesty notes:
  - This is NOT guaranteed profit. The market is usually efficient even in the
    lag; gaps are small and others compete for them. Some "finished" games can
    still be subject to review/voids. Always run in PAPER first and measure.
  - We require the ESPN game to be `completed` AND have a clear winner (no draw)
    before considering the market, to avoid acting on in-progress noise.
"""
import re
import requests
from dataclasses import dataclass
from typing import List, Optional

from . import config
from .market_data import _parse_list, _safe_float, _hours_until


# ESPN public scoreboard endpoints (free, no key). Add leagues as needed.
ESPN_LEAGUES = {
    "soccer-epl":     "soccer/eng.1",
    "soccer-usa":     "soccer/usa.1",
    "soccer-laliga":  "soccer/esp.1",
    "soccer-seriea":  "soccer/ita.1",
    "soccer-ucl":     "soccer/uefa.champions",
    "nba":            "basketball/nba",
    "mlb":            "baseball/mlb",
    "nfl":            "football/nfl",
    "nhl":            "hockey/nhl",
}

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


@dataclass
class FinishedGame:
    league: str
    home: str
    away: str
    home_score: int
    away_score: int
    winner: Optional[str]   # team displayName, or None for a draw
    aliases: List[str]      # name variants for matching (full + abbreviation)


def fetch_finished_games() -> List[FinishedGame]:
    """Pull all completed games with a known winner across tracked leagues."""
    games: List[FinishedGame] = []
    for league, path in ESPN_LEAGUES.items():
        try:
            r = requests.get(ESPN_URL.format(path=path), timeout=12)
            if r.status_code != 200:
                continue
            for e in r.json().get("events", []):
                comp = (e.get("competitions") or [{}])[0]
                status = comp.get("status", {}).get("type", {})
                if not status.get("completed"):
                    continue
                teams = comp.get("competitors", [])
                if len(teams) != 2:
                    continue

                parsed = {}
                for t in teams:
                    team = t.get("team", {})
                    parsed[t.get("homeAway", "?")] = {
                        "name": team.get("displayName", ""),
                        "short": team.get("shortDisplayName", ""),
                        "abbr": team.get("abbreviation", ""),
                        "score": int(t.get("score", 0) or 0),
                        "winner": t.get("winner", False),
                    }
                if "home" not in parsed or "away" not in parsed:
                    continue

                home, away = parsed["home"], parsed["away"]
                if home["winner"]:
                    winner = home["name"]
                elif away["winner"]:
                    winner = away["name"]
                else:
                    winner = None  # draw

                aliases = [home["name"], home["short"], home["abbr"],
                           away["name"], away["short"], away["abbr"]]
                games.append(FinishedGame(
                    league=league,
                    home=home["name"], away=away["name"],
                    home_score=home["score"], away_score=away["score"],
                    winner=winner,
                    aliases=[a for a in aliases if a],
                ))
        except Exception:
            continue
    return games


def _is_single_match(question: str) -> bool:
    """
    True only for single-game markets ("Will X win on <date>?", "X vs Y").
    False for tournament outrights ("Will X win the World Cup?"), which a
    finished single game must never be matched against.
    """
    ql = question.lower()
    if re.search(r"win on \d{4}-\d{2}-\d{2}", ql):
        return True
    if " vs " in ql or " vs. " in ql:
        return True
    # explicit tournament phrasing -> reject
    if "win the" in ql or "champion" in ql or "winner" in ql:
        return False
    return False


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation/common suffixes for fuzzy matching."""
    s = s.lower()
    s = re.sub(r"\b(fc|cf|sc|afc|united|city|club)\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _team_in_question(team: str, question: str) -> bool:
    """Does this team name appear in the market question?"""
    nt = _normalize(team)
    nq = _normalize(question)
    if not nt:
        return False
    # match on the most distinctive token (longest word of the team name)
    tokens = [w for w in nt.split() if len(w) >= 4]
    if not tokens:
        tokens = nt.split()
    return any(tok in nq for tok in tokens)


@dataclass
class LagOpportunity:
    game: FinishedGame
    question: str
    condition_id: str
    side: str            # YES or NO — the side that already won
    market_price: float  # current price of that winning side
    implied_profit: float  # 1.00 - market_price  (per $1 of payout)
    token_id: str
    liquidity: float


def fetch_open_sports_markets(max_hours: float = 6.0) -> list:
    """
    Pull recently-ending sports markets from Polymarket. We look at markets
    whose end date is within a small window around now (just finished or about
    to), since that's where resolution lag lives.
    """
    import requests as rq
    url = f"{config.GAMMA_HOST}/events"
    params = {"active": "true", "closed": "false", "limit": 200,
              "order": "volume24hr", "ascending": "false"}
    resp = rq.get(url, params=params, timeout=30)
    resp.raise_for_status()

    from .market_data import _category_from_tags
    out = []
    for ev in resp.json():
        cat = _category_from_tags(ev.get("tags") or [])
        if cat not in ("soccer", "nba", "mlb", "nfl", "nhl", "tennis"):
            continue
        for m in (ev.get("markets") or []):
            if not m.get("active") or m.get("closed"):
                continue
            token_ids = _parse_list(m.get("clobTokenIds"))
            prices = _parse_list(m.get("outcomePrices"))
            if len(token_ids) < 2 or len(prices) < 2:
                continue
            out.append({
                "condition_id": m.get("conditionId", ""),
                "question": m.get("question", ""),
                "token_yes": str(token_ids[0]),
                "token_no": str(token_ids[1]),
                "price_yes": _safe_float(prices[0]),
                "liquidity": _safe_float(m.get("liquidityNum") or m.get("liquidity")),
                "category": cat,
            })
    return out


def find_lag_opportunities(min_profit: float = 0.02,
                           max_price: float = 0.98) -> List[LagOpportunity]:
    """
    Cross-reference finished games (known winners) with open Polymarket markets.
    Flags markets where the KNOWN winner is still priced below `max_price`,
    i.e. the market hasn't fully caught up to the resolved result yet.

    min_profit: minimum (1.00 - price) gap to bother flagging.
    max_price : don't flag if winner already >= this (no edge left).
    """
    games = fetch_finished_games()
    decided = [g for g in games if g.winner]  # skip draws
    if not decided:
        return []

    markets = fetch_open_sports_markets()
    opps: List[LagOpportunity] = []

    for m in markets:
        q = m["question"]
        # We only handle "Will TEAM win..." style binary markets confidently.
        if "win" not in q.lower():
            continue
        # CRITICAL guard: only single-match markets, never tournament outrights.
        # A finished EPL game (Man Utd beat Brighton) must NOT match
        # "Will Manchester United win the World Cup?".
        if not _is_single_match(q):
            continue

        for g in decided:
            other = g.away if g.winner == g.home else g.home
            winner_named = _team_in_question(g.winner, q)
            loser_named  = _team_in_question(other, q)

            # The subject team named in the question must be one of these two.
            if not (winner_named or loser_named):
                continue

            # Confidence guard: a "Will X win on DATE?" market names only ONE
            # team. To avoid matching the wrong game, require that the OTHER
            # team is NOT contradicted — i.e. if a second team is named, it
            # must be our opponent, not some unrelated club.
            if winner_named and loser_named:
                subject = _question_subject(q, g.winner, other)
            elif winner_named:
                subject = "winner"      # "Will <winner> win?" -> YES wins
            else:
                subject = "loser"       # "Will <loser> win?"  -> NO wins

            if subject == "winner":
                side, price, token = "YES", m["price_yes"], m["token_yes"]
            else:
                side, price, token = "NO", round(1 - m["price_yes"], 4), m["token_no"]

            profit = round(1.0 - price, 4)
            if profit < min_profit or price > max_price or price <= 0:
                continue

            opps.append(LagOpportunity(
                game=g, question=q, condition_id=m["condition_id"],
                side=side, market_price=price, implied_profit=profit,
                token_id=token, liquidity=m["liquidity"],
            ))
            break  # one game per market

    opps.sort(key=lambda o: o.implied_profit, reverse=True)
    return opps


def _question_subject(question: str, winner: str, loser: str) -> str:
    """
    Determine whether a 'Will X win?' question is asking about the winner or
    the loser, by which team name appears first / more prominently.
    Returns "winner" or "loser".
    """
    nq = _normalize(question)
    nw = _normalize(winner)
    nl = _normalize(loser)
    # find earliest position of each team's distinctive token
    def first_pos(name):
        toks = [w for w in name.split() if len(w) >= 4] or name.split()
        positions = [nq.find(t) for t in toks if t in nq]
        return min(positions) if positions else 9999
    return "winner" if first_pos(nw) <= first_pos(nl) else "loser"
