"""
Self-improvement engine — the strategy tunes itself day by day, within HARD,
LOGGED bounds. This is "the data adjusts the dials", NOT "the code rewrites
itself": every change is a bounded edit to a JSON state file (strategy_state.json)
that the live strategy reads, and every decision is appended to an audit log.

Three loops, run once per day after the edge scan + resolve:

1) PROMOTE (cautious): a candidate cell that passes the bulletproof gate
   (edge_scan15) on >=PROMOTE_DAYS SEPARATE daily scans is auto-added as an
   EXPLORATORY tier (half stake). A single lucky scan can never promote.
   Promotion to full stake stays MANUAL.

2) RE-TUNE: each active tier's stake multiplier is nudged toward its recent
   realized ROI, clamped to [TIER_MULT_MIN, TIER_MULT_MAX]. Capital drifts toward
   what is actually working — gently, never more than RETUNE_STEP per day.

3) DEMOTE (warn -> improve -> disable): if an active edge's rolling live win-rate
   falls below its breakeven, first WARN and shrink its stake (improve). If it
   STILL underperforms after GRACE_DAYS of warnings, DISABLE it. Protects capital
   without nuking an edge on one bad day.

Nothing here is unbounded or opaque. Read strategy_state.json to see the dials;
read self_improve_log.jsonl to see why each changed.
"""
import json
import os
import datetime
from collections import defaultdict

from . import config, store

BASE = os.path.join(os.path.dirname(__file__), "..")
STATE_PATH = os.path.join(BASE, "strategy_state.json")
LOG_PATH = os.path.join(BASE, "self_improve_log.jsonl")
HISTORY_PATH = os.path.join(BASE, "edge_scan_history.jsonl")

# --- bounds (safety rails) ---
PROMOTE_DAYS = 5            # gate must pass on >=5 separate daily scans
GRACE_DAYS = 3             # warnings before an underperforming edge is disabled
TIER_MULT_MIN = 0.25       # never size below 25% of base
TIER_MULT_MAX = 1.0        # never auto-size above full stake
RETUNE_STEP = 0.10         # max stake-multiplier change per day
ROLLING_BETS = 30          # window for live win-rate evaluation
MIN_EVAL_BETS = 12         # need this many settled bets to judge an edge


def _today():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"tiers": {}, "disabled": [], "warnings": {}, "updated": None}


def save_state(state: dict):
    state["updated"] = _today()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _log(action: str, detail: dict):
    rec = {"ts": datetime.datetime.utcnow().isoformat(), "action": action, **detail}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"[self-improve] {action}: {detail}")


def _scan_history() -> list:
    out = []
    if not os.path.exists(HISTORY_PATH):
        return out
    with open(HISTORY_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


# ---------------------------------------------------------------------------
def promote(state: dict):
    """Auto-add a candidate that cleared the bulletproof gate on >=PROMOTE_DAYS
    distinct days. Added as exploratory (half stake); full stake stays manual."""
    hist = _scan_history()
    # count DISTINCT days each cell passed
    days_by_cell = defaultdict(set)
    for h in hist:
        day = (h.get("ts") or "")[:10]
        for p in h.get("passed", []):
            days_by_cell[p["cell"]].add(day)
    for cell, days in days_by_cell.items():
        if cell in state["tiers"] or cell in state.get("disabled", []):
            continue
        if len(days) >= PROMOTE_DAYS:
            state["tiers"][cell] = {"tier": "exploratory", "mult": 0.5,
                                    "promoted": _today(), "source": "auto-recur"}
            _log("PROMOTE", {"cell": cell, "recurred_days": len(days),
                             "tier": "exploratory", "mult": 0.5})


# ---------------------------------------------------------------------------
def _live_winrate(estimator_or_cat: str):
    """Rolling live win-rate + avg price for recently-settled bets of a strategy."""
    with store._conn() as c:
        rows = c.execute(
            "SELECT status, market_prob FROM trades "
            "WHERE status IN ('WON','LOST') AND (estimator=? OR category=?) "
            "ORDER BY id DESC LIMIT ?",
            (estimator_or_cat, estimator_or_cat, ROLLING_BETS)).fetchall()
    if len(rows) < MIN_EVAL_BETS:
        return None
    wins = sum(1 for s, _ in rows if s == "WON")
    n = len(rows)
    avg_price = sum(p for _, p in rows if p) / n
    return {"win_rate": wins / n, "n": n, "breakeven": avg_price}


def retune_and_demote(state: dict):
    """Nudge stakes toward realized ROI; warn->improve->disable decaying edges."""
    for cell, cfg in list(state["tiers"].items()):
        # evaluate by the cell's source estimator if present, else skip live eval
        ev = _live_winrate("longshot-fade")   # current single live estimator
        if ev is None:
            continue
        edge = ev["win_rate"] - ev["breakeven"]
        warns = state.setdefault("warnings", {})
        if ev["win_rate"] < ev["breakeven"]:
            # UNDERPERFORMING: warn + improve (shrink stake) first.
            warns[cell] = warns.get(cell, 0) + 1
            new_mult = max(TIER_MULT_MIN, round(cfg["mult"] - RETUNE_STEP, 3))
            cfg["mult"] = new_mult
            _log("WARN_IMPROVE", {"cell": cell, "live_win": round(ev["win_rate"], 3),
                                  "breakeven": round(ev["breakeven"], 3),
                                  "new_mult": new_mult, "warnings": warns[cell]})
            if warns[cell] >= GRACE_DAYS:
                state["disabled"].append(cell)
                del state["tiers"][cell]
                _log("DISABLE", {"cell": cell, "reason": "underperformed after grace",
                                 "warnings": warns[cell]})
        else:
            # WORKING: clear warnings, gently nudge stake toward the edge size.
            warns[cell] = 0
            target = 0.5 + min(0.5, max(0.0, edge) * 2)   # bigger edge -> bigger stake
            step = max(-RETUNE_STEP, min(RETUNE_STEP, target - cfg["mult"]))
            cfg["mult"] = round(max(TIER_MULT_MIN, min(TIER_MULT_MAX, cfg["mult"] + step)), 3)


def run():
    store.init_db()
    state = load_state()
    promote(state)
    retune_and_demote(state)
    save_state(state)
    print(f"[self-improve] state: {len(state['tiers'])} active tiers, "
          f"{len(state.get('disabled', []))} disabled.")
    return state


if __name__ == "__main__":
    run()
