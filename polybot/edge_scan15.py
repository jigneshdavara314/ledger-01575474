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
import os
import re
from collections import defaultdict

from . import config
from .calibration import fetch_resolved_markets, price_before_close, _parse

DAYS = 45                   # 45-day lookback (~3x the markets of 15d) so more
                            # cells reach n>=MIN_N and clear the Bonferroni bar —
                            # the breadth that surfaces MORE real edges to bet.
FEE = 0.01                  # ~1% round-trip friction (matches PAPER_FEE_FRAC)
MIN_N = 20                  # minimum INDEPENDENT observations to consider a cell
EDGE_MARGIN = 0.03          # Wilson LB must beat breakeven by this much
PRICE_BANDS = [(0.05, 0.25), (0.25, 0.45), (0.45, 0.55),
               (0.55, 0.75), (0.75, 0.95)]


# Statistics primitives come from the shared single-source module.
from .stats import wilson_lower, bonferroni_z as _z_for_family_wise


def _load_candidate_families():
    """Auto-discovered candidate patterns (from discover_families). These get
    TESTED through the same gate; they only ever bet if they pass + recur. Empty
    list if no queue yet."""
    import json
    p = os.path.join(os.path.dirname(__file__), "..", "discovered_families.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("candidates", [])
    except Exception:
        return []


def family_of_with_candidates(question, candidates):
    """family_of, but if it returns 'other', try to match a queued candidate
    keyword so auto-discovered patterns become their own testable family."""
    fam = family_of(question)
    if fam != "other":
        return fam
    ql = question.lower()
    for c in candidates:
        kw = (c.get("keyword") or "").lower()
        if kw and kw in ql:
            return c.get("family") or fam
    return fam


# Classification comes from the single-source taxonomy module (no drift).
from .taxonomy import family_of, CRYPTO_HINTS as _CRYPTO_HINTS


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
    # Use the ACTUAL current date so the scan window rolls forward each run.
    # (A hardcoded date forever replayed one fixed window, silently defeating the
    # cross-day recurrence gate that promotion depends on.)
    today = datetime.datetime.utcnow().date()
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


def scan(days=DAYS, cap=5000):   # higher cap for the wider 45-day window
    rows = fetch_window(days)
    print(f"Fetched {len(rows)} resolved markets over {days} days. "
          f"Sampling mid-life prices (cap {cap})...\n")

    # collapse to ONE observation per (event, family) so we don't double-count
    # correlated sub-markets; keep the first sampled price for that event/family.
    candidates = _load_candidate_families()   # auto-discovered patterns to also test
    if candidates:
        print(f"(testing {len(candidates)} auto-discovered candidate families too)")
    # 1) DEDUP FIRST (cheap, no API): one market per (event, family).
    seen = set()
    todo = []   # markets that still need a price fetch
    for m in rows:
        if len(todo) >= cap:
            break
        fam = family_of_with_candidates(m["question"], candidates)
        ek = (event_key(m), fam)
        if ek in seen:
            continue
        seen.add(ek)
        todo.append((fam, m))

    # 2) FETCH mid-life prices IN PARALLEL (the slow part — one HTTP call each).
    # Serial fetching of thousands of markets blew past the CI timeout; a thread
    # pool brings a 45-day scan from ~an hour down to a few minutes.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    obs = []   # each: (family, yes_price, yes_won, day)

    def _sample(item):
        fam, m = item
        p = price_before_close(m["token_yes"], fraction=0.5)
        if p is None:
            return None
        return (fam, p, m["yes_won"], m["day"])

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(_sample, it) for it in todo]
        done = 0
        for fu in as_completed(futs):
            r = fu.result()
            done += 1
            if r is not None:
                obs.append(r)
            if done % 500 == 0:
                print(f"  ...sampled {done}/{len(todo)} ({len(obs)} usable)")

    print(f"\n{len(obs)} INDEPENDENT observations (event-deduped).\n")

    # midpoint date for temporal OOS split
    days_sorted = sorted({o[3] for o in obs})
    if not days_sorted:
        print("No observations."); return
    mid_day = days_sorted[len(days_sorted)//2]

    # --- PASS 1: enumerate every testable cell (n>=MIN_N) so we know HOW MANY
    # hypotheses we are testing — required for the multiple-testing correction. ---
    cells = []   # (fam, lo, hi, direction, recs)
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
                    if not (lo <= price < hi):
                        continue
                    recs.append((price, won, day))
                if len(recs) >= MIN_N:
                    cells.append((fam, lo, hi, direction, recs))

    n_tests = max(1, len(cells))
    z_corr = _z_for_family_wise(n_tests)     # Bonferroni-corrected z (~3.0 for 70 cells)
    print(f"Testing {n_tests} cells -> Bonferroni z = {z_corr:.2f} "
          f"(vs naive 1.96). A cell must clear THIS bar to count.\n")

    # --- PASS 2: rigorous per-cell math with all corrections. ---
    results = []
    for fam, lo, hi, direction, recs in cells:
        n = len(recs)
        wins = sum(1 for _, w, _ in recs if w)
        wr = wins / n
        avg_q = sum(p for p, _, _ in recs) / n
        price_sd = (sum((p - avg_q) ** 2 for p, _, _ in recs) / n) ** 0.5

        # PER-BET EV then averaged (correct; avoids Jensen bias of avg-price EV):
        #   each bet pays $1 if won, costs its own price q_i -> profit (won/q_i - 1)
        ev = sum((1.0 / p - 1.0) if w else (-1.0) for p, w, _ in recs) / n - FEE

        # Bonferroni-corrected Wilson lower bound on the WIN RATE...
        wl = wilson_lower(wins, n, z=z_corr)
        # ...and the EV implied by that conservative win rate at the avg price.
        ev_lb = wl * (1.0 / avg_q) - 1.0 - FEE if avg_q > 0 else -1

        # Temporal OOS: split by date; each half must clear breakeven on its OWN
        # (naive-z) Wilson lower bound, with a real minimum size per half.
        h1 = [(p, w) for p, w, d in recs if d > mid_day]
        h2 = [(p, w) for p, w, d in recs if d <= mid_day]
        def half_lb(h):
            if not h:
                return 0.0, 0.0
            q = sum(p for p, _ in h) / len(h)
            w_ = sum(1 for _, w in h if w)
            return wilson_lower(w_, len(h)), q
        lb1, q1 = half_lb(h1)
        lb2, q2 = half_lb(h2)
        breakeven = avg_q

        # CONTAMINATION GUARD: if prices within the band vary wildly, the cell is
        # likely mislabeled/mixed -> distrust it.
        clean = price_sd <= (hi - lo)        # sd should fit within the band width

        # FULL GATE — a REAL edge must pass ALL of:
        real = (
            ev > 0                                 # profitable after fee (per-bet)
            and ev_lb > 0                          # profitable even on the conservative bound
            and wl > breakeven + EDGE_MARGIN       # Bonferroni-corrected LB beats price
            and len(h1) >= 10 and len(h2) >= 10    # enough in each OOS half
            and lb1 > q1 and lb2 > q2              # each half independently +edge (LB)
            and clean                              # not contaminated
        )
        results.append({
            "cell": f"{fam} | pay {direction} {lo:.2f}-{hi:.2f}",
            "n": n, "wins": wins, "win_rate": round(wr, 3),
            "avg_price": round(avg_q, 3), "price_sd": round(price_sd, 3),
            "breakeven": round(breakeven, 3),
            "ev": round(ev, 3), "ev_lower": round(ev_lb, 3),
            "wilson_lower": round(wl, 3), "z_corr": round(z_corr, 2),
            "oos": f"{lb1*100:.0f}>{q1*100:.0f} / {lb2*100:.0f}>{q2*100:.0f}",
            "clean": clean,
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

    # Write the latest result + APPEND a history line so continuous daily runs
    # build a persistence signal: a real edge recurs across many days; a one-day
    # fluke does not. (Confidence comes from re-appearance, not a single scan.)
    import json, os, datetime as _dt
    base = os.path.join(os.path.dirname(__file__), "..")
    stamp = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(base, "edge_scan15_result.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": stamp, "days": days,
                   "n_observations": len(obs), "cells": results,
                   "passed_gate": reals}, f, indent=2)
    with open(os.path.join(base, "edge_scan_history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": stamp, "days": days, "n_obs": len(obs),
                            "passed": [{"cell": r["cell"], "n": r["n"],
                                        "win_rate": r["win_rate"], "ev": r["ev"],
                                        "wilson_lower": r["wilson_lower"]}
                                       for r in reals]}) + "\n")
    print(f"\nResult written; history appended ({len(reals)} passing cells).")
    return results


if __name__ == "__main__":
    import sys
    d = int(sys.argv[1]) if len(sys.argv) > 1 else DAYS
    scan(days=d)
