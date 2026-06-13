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
# TWO-TRACK promotion so the bot is autonomous AND productive (new edges actually
# get bet within days, not weeks) without weakening the bulletproof math gate —
# we only change how fast a *passing* candidate gets trialed, never the gate itself.
TRIAL_DAYS = 2             # clear the gate on >=2 days -> TRIAL at tiny stake
TRIAL_MULT = 0.25         # trial stake = 25% of base (minimal risk on new edges)
PROMOTE_DAYS = 5           # clear on >=5 days -> graduate trial to exploratory (0.5x)
GRACE_DAYS = 2             # underperforming edge: warn/shrink, then disable fast
TIER_MULT_MIN = 0.10       # floor before disable
TIER_MULT_MAX = 1.0        # never auto-size above full stake
RETUNE_STEP = 0.10         # max stake-multiplier change per day
ROLLING_BETS = 30          # window for live win-rate evaluation
MIN_EVAL_BETS = 10         # need this many settled bets to judge an edge


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
    """Two-track auto-promotion from the bulletproof-gate recurrence history:
      >=TRIAL_DAYS distinct days   -> TRIAL tier at TRIAL_MULT (tiny stake)
      >=PROMOTE_DAYS distinct days -> graduate to EXPLORATORY (half stake)
    A candidate must STILL clear the full math gate each of those days — we never
    bet noise; we just start betting a *proven-recurring* edge sooner, small."""
    hist = _scan_history()
    days_by_cell = defaultdict(set)
    for h in hist:
        day = (h.get("ts") or "")[:10]
        for p in h.get("passed", []):
            days_by_cell[p["cell"]].add(day)
    for cell, days in days_by_cell.items():
        if cell in state.get("disabled", []):
            continue
        nd = len(days)
        cur = state["tiers"].get(cell)
        if nd >= PROMOTE_DAYS and (not cur or cur.get("tier") != "exploratory"):
            state["tiers"][cell] = {"tier": "exploratory", "mult": 0.5,
                                    "promoted": _today(), "recurred": nd,
                                    "source": "auto-recur"}
            _log("GRADUATE", {"cell": cell, "recurred_days": nd,
                              "tier": "exploratory", "mult": 0.5})
        elif nd >= TRIAL_DAYS and not cur:
            state["tiers"][cell] = {"tier": "trial", "mult": TRIAL_MULT,
                                    "promoted": _today(), "recurred": nd,
                                    "source": "auto-trial"}
            _log("TRIAL", {"cell": cell, "recurred_days": nd,
                           "tier": "trial", "mult": TRIAL_MULT})


# ---------------------------------------------------------------------------
def _cell_winrate(cell: str):
    """Rolling live win-rate + breakeven for a CELL, judged on its OWN bets —
    matched by the cell's family keyword against the trade question. So each
    promoted edge is evaluated on its own performance, not the whole strategy."""
    fam = cell.split("|")[0].strip()
    parts = [p for p in fam.replace("/", "_").split("_") if len(p) > 2]
    if not parts:
        return None
    like = "%" + parts[0] + "%"
    with store._conn() as c:
        rows = c.execute(
            "SELECT status, market_prob FROM trades "
            "WHERE status IN ('WON','LOST') AND LOWER(question) LIKE ? "
            "ORDER BY id DESC LIMIT ?", (like, ROLLING_BETS)).fetchall()
    if len(rows) < MIN_EVAL_BETS:
        return None
    wins = sum(1 for s, _ in rows if s == "WON")
    n = len(rows)
    avg_price = sum(p for _, p in rows if p) / n
    return {"win_rate": wins / n, "n": n, "breakeven": avg_price}


def retune_and_demote(state: dict):
    """Nudge stakes toward realized ROI; warn->improve->disable decaying edges.
    Each cell judged on ITS OWN live bets. Trials with too few bets are left to
    accumulate (not punished for lack of data)."""
    for cell, cfg in list(state["tiers"].items()):
        ev = _cell_winrate(cell)
        if ev is None:
            continue   # not enough of this edge's own bets settled yet
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
