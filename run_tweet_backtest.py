"""
Backtest the tweet-count NO-fade edge on all resolved tweet-count markets.

    python run_tweet_backtest.py

Tweet-count markets ("Will <person> post X-Y posts this week?") are a NEW
longshot-fade candidate: many mutually-exclusive ranges, each overpriced, so
buying NO on each wins often. This pages deep through resolved markets to
collect as many as possible and measures the edge with a Wilson CI.

Re-run this over the coming week as more tweet markets resolve — the sample
grows and the verdict sharpens. Saves tweet_backtest.json for the dashboard.
"""
import json
import math
import statistics
import datetime
import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from polybot import config
from polybot.calibration import price_before_close, _parse


def _wilson(w, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = w / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    m = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / d
    return (max(0, c-m), min(1, c+m))


def collect_tweet_markets(pages=40):
    out, offset = [], 0
    for _ in range(pages):
        r = requests.get(
            f"{config.GAMMA_HOST}/markets",
            params={"closed": "true", "limit": 100, "offset": offset,
                    "order": "closedTime", "ascending": "false"},
            timeout=30,
        )
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        for m in batch:
            q = m.get("question", "").lower()
            if "post" in q and "posts" in q and ("from" in q or "between" in q):
                prices = _parse(m.get("outcomePrices"))
                toks = _parse(m.get("clobTokenIds"))
                if len(prices) < 2 or len(toks) < 2:
                    continue
                if prices not in (["1", "0"], ["0", "1"]):
                    continue
                out.append({"q": m.get("question", ""),
                            "token_yes": str(toks[0]),
                            "yes_won": prices[0] == "1"})
        offset += 100
    return out


def main():
    print("Collecting resolved tweet-count markets (paging deep)...")
    markets = collect_tweet_markets()
    print(f"Found {len(markets)} resolved tweet-count markets.\n")

    checked = wins = 0
    pnl = 0.0
    prices = []
    stake = 1.0
    for m in markets:
        p_yes = price_before_close(m["token_yes"])
        if p_yes is None:
            continue
        if not (0.10 <= p_yes <= 0.60):   # only the overpriced longshot ranges
            continue
        checked += 1
        no_price = round(1 - p_yes, 4)
        no_won = not m["yes_won"]
        shares = stake / no_price
        if no_won:
            wins += 1
            pnl += shares - stake
        else:
            pnl += -stake
        prices.append(no_price)

    if checked == 0:
        print("No usable tweet-count markets with a mid-life price yet.")
        return

    wr = wins / checked
    lo, hi = _wilson(wins, checked)
    avgp = statistics.mean(prices)
    roi = pnl / (checked * stake)
    verdict = ("EDGE CONFIRMED" if (lo > 0.5 and roi > 0 and checked >= 20)
               else "PROMISING (need >=20 bets to confirm)" if checked < 20
               else "INCONCLUSIVE")

    print("=" * 56)
    print("  TWEET-COUNT NO-FADE BACKTEST")
    print("=" * 56)
    print(f"  Bets         : {checked}")
    print(f"  NO win rate  : {wr*100:.0f}%   (95% CI {lo*100:.0f}-{hi*100:.0f}%)")
    print(f"  Avg NO price : {avgp:.2f}   edge {(wr-avgp)*100:+.0f}%")
    print(f"  P&L          : ${pnl:+.2f} on ${checked*stake:.0f}  (ROI {roi*100:+.0f}%)")
    print(f"  VERDICT      : {verdict}")

    out = {
        "bets": checked, "win_rate": round(wr, 3),
        "ci": [round(lo, 3), round(hi, 3)], "avg_price": round(avgp, 3),
        "edge": round(wr - avgp, 3), "roi": round(roi, 3),
        "pnl": round(pnl, 2), "verdict": verdict,
        "generated": datetime.datetime.utcnow().isoformat(),
    }
    with open("tweet_backtest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n  (saved to tweet_backtest.json — re-run weekly as more resolve)")


if __name__ == "__main__":
    main()
