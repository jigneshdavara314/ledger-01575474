"""
Edge hunt — rigorously test whether NEW market patterns / categories carry the
same exploitable favorite-longshot bias that the soccer exact-score edge does.

Method (same rigor that confirmed the soccer edge):
  1. Pull a large sample of ALREADY-RESOLVED binary markets.
  2. Classify each by a PATTERN FAMILY parsed from the question text (exact-score,
     spread/handicap, over/under, moneyline-longshot, tweet-range, etc.) and a
     rough SPORT guess — because resolved markets carry no tags.
  3. For each market, fetch the mid-life YES price (price_before_close already
     filters dead/placeholder markets).
  4. We only care about LONGSHOTS: YES priced in [0.10, 0.55]. For those, the
     bet is "buy NO". Record whether NO actually won.
  5. Per (family) and (family, sport), compute:
        - measured NO-win rate
        - the AVERAGE NO entry price (what you'd pay)
        - after-fee EV per $1 staked on NO
        - Wilson 95% lower bound on the NO-win rate
     An edge is REAL only if the after-fee EV is positive AND the Wilson lower
     bound still beats the break-even win rate (so it's not just small-sample
     luck). We also split the sample in half (out-of-sample) to check the edge
     holds in both halves.

This is a measurement tool, not a trader. It tells you which families/sports to
promote into LONGSHOT_TIERS / CALIB, and which are noise.
"""
import math
import re
from collections import defaultdict

from .calibration import fetch_resolved_markets, price_before_close
from . import config


# Polymarket taker fee assumption for paper EV (conservative). The longshot
# module bids below ask, so real fees/slippage are roughly in this ballpark.
FEE = float(getattr(config, "PAPER_FEE", 0.0))  # Polymarket has no per-trade fee
# Break-even: buying NO at price q wins $1, so you need win_rate > q to profit.
# We require a margin above break-even, not just >q, to call it an edge.
EDGE_MARGIN = 0.04

FADE_MIN_YES = 0.10
FADE_MAX_YES = 0.55


# ---- pattern-family classification (the thing the edge actually lives in) ----

def family_of(q: str):
    ql = q.lower()
    if "exact score" in ql:
        return "exact_score"
    if "spread:" in ql or "1h spread" in ql or "handicap" in ql:
        return "spread_handicap"
    if re.search(r"o/u \d", ql) or "over/under" in ql or " total " in ql:
        return "over_under"
    if "posts from" in ql or "posts between" in ql or "tweets" in ql:
        return "tweet_range"
    if re.search(r"win (the |their |on |vs)", ql) or " to win " in ql:
        return "moneyline"
    if "draw" in ql:
        return "draw"
    return "other"


SPORT_HINTS = {
    "golf": ["golf", "pga", "masters", "open championship", "ryder", "hole",
             "to make the cut", "round leader"],
    "boxing": ["boxing", " ko ", "knockout", "by decision", "rounds:", "vs."],
    "cricket": ["cricket", "ipl", "wickets", "runs", " odi ", " t20 ", "innings",
                "test match"],
    "f1": ["f1", "formula 1", "grand prix", "pole position", "fastest lap",
           "podium", "qualifying"],
    "nhl": ["nhl", "hockey", "puck", "goalie", "stanley cup"],
    "tennis": ["set ", "tiebreak", "games o/u", "aces", "djokovic", "alcaraz",
               "sinner", "swiatek"],
    "soccer": ["exact score", " win on ", "o/u 0.5", "o/u 1.5", "o/u 2.5",
               "first half", "clean sheet", "both teams"],
    "esports": ["map ", "counter-strike", "valorant", "league of legends",
                "dota", " cs2", "rounds handicap"],
    "nba": ["nba", "knicks", "lakers", "celtics", "1h spread", "points o/u"],
    "mlb": ["mlb", "yankees", "braves", "innings", "home run", "strikeouts"],
    "nfl": ["nfl", "touchdown", "quarterback", "yards", "super bowl"],
}


def sport_of(q: str):
    ql = q.lower()
    best, bestn = "other", 0
    for sport, hints in SPORT_HINTS.items():
        n = sum(1 for h in hints if h in ql)
        if n > bestn:
            best, bestn = sport, n
    return best


from .stats import wilson_lower  # single source of truth


def _ev_no(no_win_rate: float, avg_no_price: float) -> float:
    """EV per $1 staked on NO: win_rate * (1/q) - 1, minus fee, where q=price."""
    if avg_no_price <= 0:
        return 0.0
    gross = no_win_rate * (1.0 / avg_no_price) - 1.0
    return gross - FEE


def hunt(pages: int = 16, sample_cap: int = 1200, by_sport: bool = True):
    """
    Returns a dict of results keyed by family and (family,sport), each with the
    measured stats and a verdict. `pages*100` resolved markets are pulled; up to
    `sample_cap` get their price history sampled (the slow part).
    """
    markets = fetch_resolved_markets(pages=pages)
    # records: key -> list of (no_price, no_won, idx_for_oos)
    fam = defaultdict(list)
    fam_sport = defaultdict(list)
    checked = 0
    seq = 0

    for m in markets:
        if checked >= sample_cap:
            break
        f = family_of(m["question"])
        if f == "other":
            continue
        p = price_before_close(m["token_yes"], fraction=0.5)
        if p is None:
            continue
        if not (FADE_MIN_YES <= p <= FADE_MAX_YES):
            continue
        checked += 1
        seq += 1
        no_price = round(1.0 - p, 4)
        no_won = not m["yes_won"]
        fam[f].append((no_price, no_won, seq))
        if by_sport:
            s = sport_of(m["question"])
            fam_sport[(f, s)].append((no_price, no_won, seq))
        if checked % 100 == 0:
            print(f"  ...{checked} longshots sampled")

    def summarize(records):
        n = len(records)
        if n == 0:
            return None
        wins = sum(1 for _, w, _ in records if w)
        avg_q = sum(q for q, _, _ in records) / n
        wr = wins / n
        wl = wilson_lower(wins, n)
        ev = _ev_no(wr, avg_q)
        ev_lb = _ev_no(wl, avg_q)          # EV using the conservative win bound
        breakeven = avg_q                   # need wr > q
        # out-of-sample: split by sequence parity (first-half vs second-half)
        recs = sorted(records, key=lambda r: r[2])
        half = len(recs) // 2
        h1 = recs[:half]
        h2 = recs[half:]
        def wr_of(rs):
            return (sum(1 for _, w, _ in rs if w) / len(rs)) if rs else 0.0
        wr1, wr2 = wr_of(h1), wr_of(h2)
        # verdict
        real = (n >= 25 and ev > 0 and wl > breakeven + EDGE_MARGIN
                and wr1 > breakeven and wr2 > breakeven)
        promising = (not real and n >= 10 and ev > 0 and wr > breakeven)
        return {
            "n": n, "wins": wins, "no_win_rate": round(wr, 4),
            "avg_no_price": round(avg_q, 4), "wilson_lower": round(wl, 4),
            "ev_per_$1": round(ev, 4), "ev_lower": round(ev_lb, 4),
            "breakeven": round(breakeven, 4),
            "oos_h1": round(wr1, 4), "oos_h2": round(wr2, 4),
            "verdict": "REAL" if real else ("promising" if promising else "noise"),
        }

    out = {"by_family": {}, "by_family_sport": {}, "checked": checked}
    for f, recs in fam.items():
        out["by_family"][f] = summarize(recs)
    if by_sport:
        for key, recs in fam_sport.items():
            s = summarize(recs)
            if s and s["n"] >= 10:           # ignore tiny sport buckets
                out["by_family_sport"][f"{key[0]}::{key[1]}"] = s
    return out


def _fmt(title, d):
    if not d:
        return f"{title}: (no data)\n"
    line = (f"{title:36} n={d['n']:>4} NOwin={d['no_win_rate']*100:5.1f}% "
            f"avgQ={d['avg_no_price']:.3f} EV/$1={d['ev_per_$1']:+.3f} "
            f"WilsonLB={d['wilson_lower']*100:5.1f}% "
            f"oos[{d['oos_h1']*100:.0f}/{d['oos_h2']*100:.0f}] "
            f"-> {d['verdict']}")
    return line


def main(pages: int = 16, sample_cap: int = 1200):
    print(f"Hunting edges across resolved markets "
          f"(pages={pages}, cap={sample_cap})...\n")
    res = hunt(pages=pages, sample_cap=sample_cap)
    print(f"\nSampled {res['checked']} longshot markets.\n")
    print("=" * 100)
    print("BY PATTERN FAMILY (the edge lives in the bet TYPE):")
    print("=" * 100)
    for f, d in sorted(res["by_family"].items(),
                       key=lambda kv: -(kv[1]["ev_per_$1"] if kv[1] else -9)):
        print(_fmt(f, d))
    print("\n" + "=" * 100)
    print("BY FAMILY x SPORT (only buckets with n>=10):")
    print("=" * 100)
    for key, d in sorted(res["by_family_sport"].items(),
                         key=lambda kv: -(kv[1]["ev_per_$1"] if kv[1] else -9)):
        print(_fmt(key, d))
    print("\nLEGEND: REAL = n>=25, +EV, Wilson lower bound beats break-even+4%, "
          "AND holds in both out-of-sample halves.")
    return res


if __name__ == "__main__":
    import sys
    pg = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
    main(pages=pg, sample_cap=cap)
