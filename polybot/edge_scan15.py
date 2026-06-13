"""
Rigorous 15-day edge scanner — hunt for a reliable, daily-bettable edge with the
math done properly and honestly.

WHAT "70% win rate" REALLY NEEDS (the critical math):
  Buying a side at price q wins $1 per share. You break even when win_rate == q.
  So a 70% win rate ONLY profits if q < 0.70. The quantity that matters is the
  AFTER-FEE EV per $1 staked:
        EV = win_rate * (1/q) - 1 - fee
  and we only TRUST it if the Wilson 95% lower bound on the win rate still beats
  break-even by a margin (so it's not small-sample luck).

METHOD (designed to avoid every trap the last hunt hit):
  - Pull resolved markets from the last `DAYS` days (your 10-15 day window).
  - ONE observation per EVENT (dedup) so correlated sub-markets of one match don't
    inflate n. This was the bug that faked the esports/tennis "edges".
  - Mid-life entry price (price_before_close already filters dead markets and only
    uses pre-entry info — no look-ahead).
  - For each (family, price_band, direction) cell, compute the FULL math.
  - Temporal out-of-sample: split the window in half by date; require the edge to
    hold in BOTH halves.
  - Report only cells with enough INDEPENDENT n and a Wilson LB beating breakeven.

Run:  python -m polybot.edge_scan15
"""
import datetime
import math
import re
from collections import defaultdict

from . import config
from .calibration import fetch_resolved_markets, price_before_close, _parse

DAYS = 15
FEE = 0.01                  # ~1% round-trip friction (matches PAPER_FEE_FRAC)
MIN_N = 20                  # minimum INDEPENDENT observations to consider a cell
EDGE_MARGIN = 0.03          # Wilson LB must beat breakeven by this much
PRICE_BANDS = [(0.05, 0.25), (0.25, 0.45), (0.45, 0.55),
               (0.55, 0.75), (0.75, 0.95)]


def wilson_lower(wins, n, z=1.96):
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    rad = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - rad) / denom)


def family_of(q):
    ql = q.lower()
    if "exact score" in ql: return "exact_score"
    if "spread" in ql or "handicap" in ql: return "spread_handicap"
    if re.search(r"o/u\s*\d", ql) or "over/under" in ql: return "over_under"
    if "moneyline" in ql or " to win" in ql or re.search(r"\bwin\b", ql): return "moneyline"
    if "to advance" in ql or "advance" in ql: return "to_advance"
    if "winner" in ql or "champion" in ql: return "outright_winner"
    if "draw" in ql: return "draw"
    return "other"


def event_key(m):
    """Best-effort EVENT id so sub-markets of one match collapse to ONE sample."""
    q = m.get("question", "")
    if ":" in q:
        return q.split(":", 1)[0].strip().lower()
    return q.strip().lower()


def fetch_window(days=DAYS):
    """Resolved markets in the last `days` days, with clean binary outcome + a
    mid-life YES price. One row per market (event dedup happens later)."""
    out = []
    today = datetime.date(2026, 6, 14)
    for d in range(days):
        day = (today - datetime.timedelta(days=d)).isoformat()
        offset = 0
        for _ in range(10):
            import requests
            r = requests.get(f"{config.GAMMA_HOST}/markets",
                             params={"closed": "true", "limit": 100, "offset": offset,
                                     "end_date_min": f"{day}T00:00:00Z",
                                     "end_date_max": f"{day}T23:59:59Z"}, timeout=30)
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            for m in batch:
                prices = _parse(m.get("outcomePrices"))
                toks = _parse(m.get("clobTokenIds"))
                if len(prices) < 2 or len(toks) < 2:
                    continue
                if prices not in (["1", "0"], ["0", "1"]):
                    continue
                out.append({"question": m.get("question", ""),
                            "token_yes": str(toks[0]),
                            "yes_won": prices[0] == "1",
                            "day": day,
                            "event": None})
            if len(batch) < 100:
                break
            offset += 100
    return out


def scan(days=DAYS, cap=2500):
    rows = fetch_window(days)
    print(f"Fetched {len(rows)} resolved markets over {days} days. "
          f"Sampling mid-life prices (cap {cap})...\n")

    # collapse to ONE observation per (event, family) so we don't double-count
    # correlated sub-markets; keep the first sampled price for that event/family.
    seen = set()
    obs = []   # each: (family, yes_price, yes_won, day)
    checked = 0
    for m in rows:
        if checked >= cap:
            break
        fam = family_of(m["question"])
        ek = (event_key(m), fam)
        if ek in seen:
            continue
        p = price_before_close(m["token_yes"], fraction=0.5)
        if p is None:
            continue
        checked += 1
        seen.add(ek)
        obs.append((fam, p, m["yes_won"], m["day"]))
        if checked % 100 == 0:
            print(f"  ...{checked} independent observations")

    print(f"\n{len(obs)} INDEPENDENT observations (event-deduped).\n")

    # midpoint date for temporal OOS split
    days_sorted = sorted({o[3] for o in obs})
    if not days_sorted:
        print("No observations."); return
    mid_day = days_sorted[len(days_sorted)//2]

    # cell -> for each direction, list of (price_paid, won, day)
    # direction: buy YES (win if yes_won, pay yes_price) or buy NO (win if !yes_won, pay 1-yes_price)
    results = []
    for fam in sorted({o[0] for o in obs}):
        for lo, hi in PRICE_BANDS:
            for direction in ("YES", "NO"):
                recs = []
                for f, yp, yw, day in obs:
                    if f != fam:
                        continue
                    if direction == "YES":
                        price = yp; won = yw
                    else:
                        price = round(1 - yp, 4); won = not yw
                    # band is on the PRICE PAID for the chosen side
                    if not (lo <= price < hi):
                        continue
                    recs.append((price, won, day))
                n = len(recs)
                if n < MIN_N:
                    continue
                wins = sum(1 for _, w, _ in recs if w)
                wr = wins / n
                avg_q = sum(p for p, _, _ in recs) / n
                wl = wilson_lower(wins, n)
                ev = wr * (1.0/avg_q) - 1.0 - FEE if avg_q > 0 else -1
                ev_lb = wl * (1.0/avg_q) - 1.0 - FEE if avg_q > 0 else -1
                # temporal OOS: first-half vs second-half by date
                h1 = [(p, w) for p, w, d in recs if d > mid_day]   # earlier dates (d desc)
                h2 = [(p, w) for p, w, d in recs if d <= mid_day]
                wr1 = (sum(w for _, w in h1)/len(h1)) if h1 else 0
                wr2 = (sum(w for _, w in h2)/len(h2)) if h2 else 0
                breakeven = avg_q
                # an edge: after-fee +EV, Wilson LB beats breakeven+margin, holds both halves
                real = (ev > 0 and wl > breakeven + EDGE_MARGIN
                        and wr1 > breakeven and wr2 > breakeven
                        and len(h1) >= 8 and len(h2) >= 8)
                results.append({
                    "cell": f"{fam} | pay {direction} {lo:.2f}-{hi:.2f}",
                    "n": n, "wins": wins, "win_rate": round(wr, 3),
                    "avg_price": round(avg_q, 3), "breakeven": round(breakeven, 3),
                    "ev": round(ev, 3), "ev_lower": round(ev_lb, 3),
                    "wilson_lower": round(wl, 3),
                    "oos": f"{wr1*100:.0f}/{wr2*100:.0f}",
                    "verdict": "REAL" if real else (
                        "promising" if ev > 0 and wl > breakeven else "noise"),
                })

    results.sort(key=lambda r: -r["ev"])
    print("="*112)
    print(f"{'cell':46} {'n':>4} {'win%':>5} {'payQ':>5} {'b/e':>5} "
          f"{'EV/$1':>7} {'WilsonLB':>8} {'oos':>7}  verdict")
    print("="*112)
    for r in results:
        print(f"{r['cell']:46} {r['n']:>4} {r['win_rate']*100:>4.0f}% "
              f"{r['avg_price']:>5.2f} {r['breakeven']:>5.2f} {r['ev']:>+7.3f} "
              f"{r['wilson_lower']*100:>7.0f}% {r['oos']:>7}  {r['verdict']}")
    reals = [r for r in results if r["verdict"] == "REAL"]
    print("\n" + ("="*112))
    if reals:
        print(f"FOUND {len(reals)} cell(s) passing the full gate (after-fee +EV, "
              f"Wilson LB > breakeven+{EDGE_MARGIN}, holds both OOS halves):")
        for r in reals:
            print(f"  -> {r['cell']}  win {r['win_rate']*100:.0f}% @ {r['avg_price']:.2f} "
                  f"EV +{r['ev']:.3f}/$1  (n={r['n']})")
    else:
        print("NO cell passed the full gate. (High win rates exist but are already "
              "priced in — EV<=0 — or fail the Wilson/OOS test.)")

    # Write the result to disk so a cloud run can commit it back for review.
    import json, os
    out_path = os.path.join(os.path.dirname(__file__), "..", "edge_scan15_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated_utc": "cloud-run", "days": days,
                   "n_observations": len(obs), "cells": results,
                   "passed_gate": reals}, f, indent=2)
    print(f"\nResult written to {out_path}")
    return results


if __name__ == "__main__":
    import sys
    d = int(sys.argv[1]) if len(sys.argv) > 1 else DAYS
    scan(days=d)
