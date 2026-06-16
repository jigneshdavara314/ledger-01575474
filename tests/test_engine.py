"""
Tests for the bet-placement ENGINE — the orchestration logic that used to live
untestable inside run.py's CLI. Each risk gate and the fill decision are now pure
and independently verified here. This is the safety net for the bet->resolve path.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_db():
    from polybot import config, store, bankroll
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = path
    store.init_db(); bankroll.init_bankroll()
    return path


def test_event_group_key():
    from polybot.engine import event_group_key
    assert event_group_key("Germany vs. France: Exact Score 2-1?") == "germany vs. france"
    assert event_group_key("No colon here") == "no colon here"


def test_gate_affordable():
    from polybot import engine, bankroll, config
    p = _fresh_db()
    try:
        assert engine.gate_affordable(10).ok            # $500 bankroll affords $10
        assert not engine.gate_affordable(10_000).ok    # cannot afford $10k
    finally:
        os.remove(p)


def test_gate_exposure_ceiling():
    from polybot import engine
    p = _fresh_db()
    try:
        assert engine.gate_exposure(50).ok              # small stake ok
        assert not engine.gate_exposure(400).ok         # exceeds 60% of $500 equity
    finally:
        os.remove(p)


def test_gate_per_event_caps_correlated_legs():
    from polybot import engine, store, config
    p = _fresh_db()
    try:
        from polybot.strategy import Signal
        from polybot.market_data import Market
        # book MAX_PER_EVENT bets on one match (same question prefix)
        for i in range(config.LONGSHOT_MAX_PER_EVENT):
            m = Market(condition_id=f"c{i}", question=f"Spain vs Italy: line {i}",
                       token_id_yes="y", token_id_no="n", price_yes=0.4,
                       liquidity=1e5, volume=0, volume_24h=0, spread=0,
                       end_date="", hours_to_resolution=1, category="soccer", event_title="")
            sig = Signal(market=m, side="NO", fair_prob=0.8, market_prob=0.4,
                         edge=0.4, size_usd=5, reason="t", estimator="test")
            store.record_trade(sig, {"mode": "PAPER", "status": "simulated", "price": 0.4})
        # a further leg of the SAME match must be blocked
        g = engine.gate_per_event("Spain vs Italy: another line")
        assert not g.ok, "per-event cap should block extra correlated legs"
        # a DIFFERENT match is fine
        assert engine.gate_per_event("Brazil vs Peru: line").ok
    finally:
        os.remove(p)


def test_paper_fill_is_deterministic_within_day():
    from polybot import engine
    # same key+day -> same decision (reproducible); threshold monotonic
    d = "2026-06-16"
    always = engine.paper_fills("cond-x", 1.0, day=d)
    never = engine.paper_fills("cond-x", 0.0, day=d)
    assert always is True and never is False
    a = engine.paper_fills("cond-y", 0.5, day=d)
    b = engine.paper_fills("cond-y", 0.5, day=d)
    assert a == b, "paper fill must be reproducible within a day"


def test_should_attempt_live_always_true():
    from polybot import engine
    # LIVE always attempts (real order); PAPER respects the simulated roll
    assert engine.should_attempt(0.0, "cond-z", mode="LIVE") is True
    assert engine.should_attempt(0.0, "cond-z", mode="PAPER") is False


def test_yes_promoted_edge_matches():
    """A promoted YES-direction edge must be recognized on the YES price within
    its band (buy YES), and NOT treated as a NO bet."""
    from polybot import self_improve as si, longshot
    orig = si.STATE_PATH
    fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
    si.STATE_PATH = tmp
    try:
        si.save_state({"tiers": {"over_under | pay YES 0.75-0.95": {
            "tier": "trial", "direction": "YES", "band_lo": 0.75, "band_hi": 0.95,
            "measured_win": 0.956, "measured_wilson_lower": 0.855, "measured_n": 113}},
            "disabled": [], "warnings": {}})
        # YES price 0.85 is in the band -> matched, sized on Wilson LB 0.855
        py = longshot._promoted_win_yes("match o/u 2.5", 0.85)
        assert py is not None and abs(py["wl"] - 0.855) < 1e-9, py
        # YES price 0.60 (below band) -> no match
        assert longshot._promoted_win_yes("match o/u 2.5", 0.60) is None
        # the NO matcher must NOT fire on this YES cell
        assert longshot._promoted_win("match o/u 2.5", 0.15) is None
    finally:
        si.STATE_PATH = orig
        os.remove(tmp)
    print("PASS test_yes_promoted_edge_matches")


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1; print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} engine tests passed.")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
