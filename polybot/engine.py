"""
Bet-placement & settlement ENGINE — the orchestration logic, extracted from the
CLI (run.py) into the importable, UNIT-TESTABLE package.

Previously the entire bet loop (every risk gate inline) lived in run.py's
cmd_longshot and was copy-pasted into cmd_lagwatch, so the rigorous package could
size a signal but could not run — or test — the bet->resolve half of its own
pipeline. This module fixes that: each risk gate is a small pure predicate, the
fill decision is a pure function, and place_fades() composes them. run.py becomes
a thin printing wrapper around these.

Design: the gates take explicit inputs (not globals) wherever practical so they
can be tested in isolation; the few that read store/bankroll do so through the
same singletons the live path uses.
"""
import datetime
from dataclasses import dataclass
from typing import Optional

from . import config, store, bankroll
from .market_data import stable_unit


# --------------------------------------------------------------------------
# Gate result: why a candidate was skipped, or that it passed.
# --------------------------------------------------------------------------
@dataclass
class GateResult:
    ok: bool
    reason: str = ""        # human-readable skip reason ("" when ok)


PASS = GateResult(True)


def event_group_key(question: str) -> str:
    """Match identifier from a question when no event slug exists — the part
    before the first colon (usually 'Team A vs B'), so all of one match's lines
    share a correlation group and the per-event cap can't be silently bypassed."""
    q = (question or "").strip()
    return (q.split(":", 1)[0].strip().lower()) if ":" in q else q.lower()


# --------------------------------------------------------------------------
# Risk GATES — each a small, independently-testable predicate.
# A candidate must pass ALL of them (in this order) to be placed.
# --------------------------------------------------------------------------
def gate_already_open(condition_id: str) -> GateResult:
    if store.already_open(condition_id):
        return GateResult(False, "already open")
    return PASS


def gate_position_cap(max_positions: int = 50) -> GateResult:
    if store.open_position_count() >= max_positions:
        return GateResult(False, "position cap")
    return PASS


def gate_per_event(question: str, event_slug: str = "") -> GateResult:
    key = (event_slug or "") or event_group_key(question)
    if key and store.open_count_for_event_like(key) >= config.LONGSHOT_MAX_PER_EVENT:
        return GateResult(False, f"per-event cap ({config.LONGSHOT_MAX_PER_EVENT})")
    return PASS


def gate_daily_spend(stake: float) -> GateResult:
    if config.LONGSHOT_DAILY_SPEND_CAP:
        already = store.staked_today()
        if already + stake > config.daily_budget():
            return GateResult(False, f"daily budget reached (${already:.2f})")
    return PASS


def gate_exposure(stake: float) -> GateResult:
    if not bankroll.exposure_ok(stake):
        return GateResult(False, "aggregate exposure ceiling")
    return PASS


def gate_affordable(stake: float) -> GateResult:
    if not bankroll.can_afford(stake):
        return GateResult(False, f"insufficient bankroll (${bankroll.balance():.2f})")
    return PASS


def run_gates(condition_id: str, question: str, stake: float,
              event_slug: str = "") -> GateResult:
    """Run the full gate chain in order; return the first failure or PASS."""
    for g in (
        gate_already_open(condition_id),
        gate_position_cap(),
        gate_per_event(question, event_slug),
        gate_daily_spend(stake),
        gate_exposure(stake),
        gate_affordable(stake),
    ):
        if not g.ok:
            return g
    return PASS


# --------------------------------------------------------------------------
# FILL decision — pure function. In PAPER we simulate with a stable, per-UTC-day
# re-sampled draw vs fill_prob; in LIVE we never simulate (the exchange decides).
# --------------------------------------------------------------------------
def paper_fills(condition_id: str, fill_prob: float,
                day: Optional[str] = None) -> bool:
    """True if a below-ask limit would (simulated) fill today. Reproducible within
    a day, re-sampled across days. LIVE callers must NOT use this."""
    day = day or datetime.datetime.utcnow().strftime("%Y-%m-%d")
    return stable_unit(f"{condition_id}:{day}") <= fill_prob


def should_attempt(fill_prob: float, condition_id: str, mode: str = None) -> bool:
    """Whether to send the order at all. LIVE always attempts (real order, real
    fill); PAPER only 'attempts' when the simulated draw fills."""
    mode = mode or config.MODE
    if mode == "LIVE":
        return True
    return paper_fills(condition_id, fill_prob)
