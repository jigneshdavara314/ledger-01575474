"""
Per-category 30-day backfill — measure the longshot-fade NO win% for EACH
category separately, so the dashboard can show category tabs.

For every resolved market in the last 30 days that our strategy would bet
(via _longshot_tier), we record which category it's in and whether the NO
fade would have won. Output: per-category win rate, edge, P&L, sample size.

Saves category_backfill.json for the dashboard tabs. Honest: categories with
no edge or tiny samples are shown as-is, not hidden.
"""
import json
import math
import time
import datetime
import requests
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from polybot import config
from polybot.calibration import price_before_close, _parse
from polybot.longshot import _longshot_tier, FADE_MIN_YES, FADE_MAX_YES


def fine_category(q: str) -> str:
    """Fine-grained category for tabs (split sports into sub-sports)."""
    ql = q.lower()
    if "post" in ql and "posts" in ql:
        return "tweets"
    if "exact score" in ql:
        return "soccer-exactscore"
    if any(k in ql for k in ["map ", "rounds:", "counter-strike", "valorant",
                             "league of legends", " cs2", "dota", "leo team",
                             "ursa", "roshan", "baron"]):
        return "esports"
    if any(k in ql for k in ["set ", "games o/u", "set handicap", "/ ", "doubles"]):
        return "tennis"
    if any(k in ql for k in ["1h spread", "nba", "knicks", "spurs", "lakers",
                             "celtics", "o/u 2", "o/u 21"]):
        return "basketball"
    if any(k in ql for k in ["spread:", "draw?", " win on ", "o/u 0.5", "o/u 1.5"]):
        return "soccer"
    return "other"


def _wilson(w, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = w / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    m = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / d
    return (max(0, c-m), min(1, c+m))


def fetch_recent(days=30, max_pages=40):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    out, offset = [], 0
    for _ in range(max_pages):
        r = requests.get(f"{config.GAMMA_HOST}/markets",
            params={"closed": "true", "limit": 100, "offset": offset,
                    "order": "closedTime", "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        stop = False
        for m in batch:
            ct = m.get("closedTime") or m.get("endDate") or ""
            try:
                if datetime.datetime.fromisoformat(ct.replace("Z", "")[:19]) < cutoff:
                    stop = True
                    continue
            except Exception:
                continue
            if _longshot_tier(m.get("question", "")) is None:
                continue
            prices = _parse(m.get("outcomePrices"))
            toks = _parse(m.get("clobTokenIds"))
            if len(prices) < 2 or len(toks) < 2 or prices not in (["1", "0"], ["0", "1"]):
                continue
            out.append({"question": m.get("question", ""),
                        "token_yes": str(toks[0]),
                        "yes_won": prices[0] == "1",
                        "category": fine_category(m.get("question", ""))})
        offset += 100
        if stop:
            break
        time.sleep(0.04)
    return out


def main():
    days = 30
    print(f"Backfilling per-category longshot-fade win% over last {days} days...\n")
    markets = fetch_recent(days)

    cats = {}
    for m in markets:
        p_yes = price_before_close(m["token_yes"])
        if p_yes is None or not (FADE_MIN_YES <= p_yes <= FADE_MAX_YES):
            continue
        no_price = round(1 - p_yes, 4)
        no_won = not m["yes_won"]
        c = m["category"]
        d = cats.setdefault(c, {"n": 0, "wins": 0, "pnl": 0.0,
                                "staked": 0.0, "prices": []})
        d["n"] += 1
        d["prices"].append(no_price)
        stake = 1.0
        if no_won:
            d["wins"] += 1
            d["pnl"] += stake / no_price - stake
        else:
            d["pnl"] += -stake
        d["staked"] += stake

    result = {"days": days, "generated": datetime.datetime.utcnow().isoformat(),
              "categories": {}}
    print(f"{'category':20} {'n':>4} {'win%':>6} {'avgNO':>6} {'edge':>6} {'ROI':>7} {'verdict'}")
    print("-" * 76)
    for c, d in sorted(cats.items(), key=lambda x: -x[1]["n"]):
        n = d["n"]
        wr = d["wins"] / n if n else 0
        avgp = sum(d["prices"]) / n if n else 0
        lo, hi = _wilson(d["wins"], n)
        roi = d["pnl"] / d["staked"] if d["staked"] else 0
        edge = wr - avgp
        verdict = ("CONFIRMED" if (n >= 20 and lo > 0.5 and roi > 0)
                   else "promising" if (n < 20 and roi > 0)
                   else "no edge")
        result["categories"][c] = {
            "n": n, "win_rate": round(wr, 3), "avg_no_price": round(avgp, 3),
            "edge": round(edge, 3), "ci": [round(lo, 3), round(hi, 3)],
            "roi": round(roi, 3), "pnl": round(d["pnl"], 2),
            "verdict": verdict,
        }
        print(f"{c:20} {n:>4} {wr*100:>5.0f}% {avgp:>6.2f} {edge*100:>+5.0f}% "
              f"{roi*100:>+6.0f}% {verdict}")

    with open("category_backfill.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\nSaved category_backfill.json ({len(result['categories'])} categories)")


if __name__ == "__main__":
    main()
