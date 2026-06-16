"""
Daily operations report — what the automation actually DID each day, in one
coherent table. Aggregates four sources the bot already writes:

  - trades.db (real bets placed/settled, by day)
  - edge_scan_history.jsonl  (edges that PASSED the bulletproof gate per scan)
  - self_improve_log.jsonl   (edges promoted/trialed/graduated/disabled)
  - bankroll                 (balance, equity, utilization)

Used by `run.py report-daily` (CLI table) and the dashboard "Daily operations" tab.
"""
import json
import os
import datetime
from collections import defaultdict

from . import config, store, bankroll

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_jsonl(name):
    path = os.path.join(BASE, name)
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line[0] in "<=>":      # skip any stray conflict markers
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def build(days: int = 14) -> list:
    """Return a list of per-day dicts (newest first) describing the day's activity."""
    # --- trades placed + settled, grouped by day ---
    with store._conn() as c:
        placed = c.execute(
            "SELECT substr(ts,1,10) AS d, COUNT(*), COALESCE(SUM(size_usd),0) "
            "FROM trades WHERE " + store._REAL_ONLY + " GROUP BY d"
        ).fetchall()
        settled = c.execute(
            "SELECT substr(resolved_ts,1,10) AS d, "
            "SUM(status='WON'), SUM(status='LOST'), COALESCE(SUM(pnl_usd),0), "
            "COALESCE(SUM(size_usd),0) "
            "FROM trades WHERE status IN ('WON','LOST') AND resolved_ts IS NOT NULL "
            "AND " + store._REAL_ONLY + " GROUP BY d"
        ).fetchall()
    placed_by = {d: (n, stk) for d, n, stk in placed}
    settled_by = {d: (w, l, pnl, stk) for d, w, l, pnl, stk in settled}

    # --- edges that passed the gate, per day (distinct cells) ---
    edges_by = defaultdict(set)
    for h in _read_jsonl("edge_scan_history.jsonl"):
        day = (h.get("ts") or "")[:10]
        for p in h.get("passed", []):
            edges_by[day].add(p.get("cell", ""))

    # --- self-improve actions per day (new families promoted/trialed, disabled) ---
    promo_by = defaultdict(list)
    for r in _read_jsonl("self_improve_log.jsonl"):
        day = (r.get("ts") or "")[:10]
        promo_by[day].append((r.get("action", ""), r.get("cell", "")))

    # --- assemble the day set ---
    all_days = set(placed_by) | set(settled_by) | set(edges_by) | set(promo_by)
    all_days = sorted(all_days, reverse=True)[:days]

    rows = []
    for d in all_days:
        n_placed, staked = placed_by.get(d, (0, 0.0))
        won, lost, pnl, settled_stake = settled_by.get(d, (0, 0, 0.0, 0.0))
        resolved = (won or 0) + (lost or 0)
        win_rate = (won / resolved * 100) if resolved else 0.0
        roi = (pnl / settled_stake * 100) if settled_stake else 0.0
        edges_found = len(edges_by.get(d, set()))
        actions = promo_by.get(d, [])
        # DISTINCT cells (the log records a line every run; we want unique edges)
        new_promos = sorted({c for a, c in actions if a in ("TRIAL", "GRADUATE")})
        disabled = sorted({c for a, c in actions if a == "DISABLE"})
        rows.append({
            "day": d,
            "edges_found": edges_found,
            "new_promoted": len(new_promos),
            "promoted_cells": new_promos,
            "disabled": len(disabled),
            "bids_placed": n_placed or 0,
            "invested": round(staked or 0.0, 2),
            "settled": resolved,
            "won": won or 0, "lost": lost or 0,
            "win_rate": round(win_rate, 1),
            "pnl": round(pnl or 0.0, 2),
            "roi": round(roi, 1),
        })
    return rows


def totals(rows: list) -> dict:
    bk = bankroll.summary()
    util = (bk["open_exposure"] / bk["total_equity"] * 100) if bk["total_equity"] else 0
    return {
        "edges_found": sum(r["edges_found"] for r in rows),
        "new_promoted": sum(r["new_promoted"] for r in rows),
        "bids_placed": sum(r["bids_placed"] for r in rows),
        "invested": round(sum(r["invested"] for r in rows), 2),
        "pnl": round(sum(r["pnl"] for r in rows), 2),
        "balance": bk["balance"],
        "equity": bk["total_equity"],
        "open_exposure": bk["open_exposure"],
        "utilization": round(util, 1),
        "deposit": bk["initial_deposit"],
        "total_return": bk["return_pct"],
    }


def print_table(days: int = 14):
    rows = build(days)
    t = totals(rows)
    print("=" * 100)
    print("  DAILY OPERATIONS REPORT — what the automation did")
    print("=" * 100)
    print(f"{'day':11} {'edges':>5} {'new':>4} {'bids':>5} {'invested':>9} "
          f"{'W-L':>7} {'win%':>5} {'P&L':>9} {'ROI':>6}")
    print("-" * 100)
    if not rows:
        print("  (no activity recorded yet)")
    for r in rows:
        print(f"{r['day']:11} {r['edges_found']:>5} {r['new_promoted']:>4} "
              f"{r['bids_placed']:>5} ${r['invested']:>8.2f} "
              f"{str(r['won'])+'-'+str(r['lost']):>7} {r['win_rate']:>4.0f}% "
              f"${r['pnl']:>+8.2f} {r['roi']:>+5.0f}%")
    print("-" * 100)
    print(f"TOTALS: edges found {t['edges_found']} · new edges promoted "
          f"{t['new_promoted']} · bids {t['bids_placed']} · invested "
          f"${t['invested']:.2f} · P&L ${t['pnl']:+.2f}")
    print(f"ACCOUNT: balance ${t['balance']:.2f} + ${t['open_exposure']:.2f} on stake "
          f"= ${t['equity']:.2f} equity · utilization {t['utilization']:.0f}% · "
          f"return {t['total_return']:+.1f}% on ${t['deposit']:.0f}")
    print("=" * 100)


if __name__ == "__main__":
    import sys
    print_table(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
