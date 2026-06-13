"""
Automated tests for the correctness-critical logic — the parts where a bug
would cost real money: P&L math, resolution settlement, calibration sizing,
longshot classification, and the lagwatch tournament guard.

Run:  python -m pytest tests/ -q      (or)   python tests/test_core.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# P&L + settlement math (store.settle_position)
# ---------------------------------------------------------------------------
def test_pnl_win_and_loss():
    from polybot import config, store
    # isolate DB
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    config.DB_PATH = path
    store.init_db()

    from polybot.strategy import Signal
    from polybot.market_data import Market
    m = Market(condition_id="0xT", question="Q", token_id_yes="y", token_id_no="n",
               price_yes=0.6, liquidity=1e5, volume=0, volume_24h=0, spread=0,
               end_date="", hours_to_resolution=1, category="soccer", event_title="")
    # Isolate the friction model for deterministic math: zero fee/slippage here.
    config.PAPER_FEE_FRAC = 0.0
    config.PAPER_SLIPPAGE = 0.0
    # bet NO at 0.80 with $2 -> 2.5 shares (fill price passed explicitly)
    sig = Signal(market=m, side="NO", fair_prob=0.9, market_prob=0.80,
                 edge=0.1, size_usd=2.0, reason="t", estimator="test")
    store.record_trade(sig, {"mode": "PAPER", "status": "simulated", "price": 0.80})
    pos = store.open_positions()[0]
    trade_id, _, _, side, size, mktp, shares = pos
    assert abs(shares - 2.5) < 1e-6, f"shares should be 2.5, got {shares}"

    # WIN (no fee): payout = shares*1 = 2.5, profit = 0.5
    pnl, fee = store.settle_position(trade_id, won=True, size_usd=size, shares=shares)
    assert abs(pnl - 0.5) < 1e-6, f"win pnl should be +0.5, got {pnl}"
    assert fee == 0.0, f"fee should be 0 here, got {fee}"

    # LOSS case on a fresh bet
    store.record_trade(sig, {"mode": "PAPER", "status": "simulated", "price": 0.80})
    pos2 = [p for p in store.open_positions()][0]
    pnl2, _ = store.settle_position(pos2[0], won=False, size_usd=2.0, shares=2.5)
    assert abs(pnl2 - (-2.0)) < 1e-6, f"loss pnl should be -2.0, got {pnl2}"

    # Now verify the FRICTION model bites: 1% fee on a $2 stake = $0.02 drag.
    config.PAPER_FEE_FRAC = 0.01
    store.record_trade(sig, {"mode": "PAPER", "status": "simulated", "price": 0.80})
    p3 = [p for p in store.open_positions()][0]
    pnl3, fee3 = store.settle_position(p3[0], won=True, size_usd=2.0, shares=2.5)
    assert abs(pnl3 - (0.5 - 0.02)) < 1e-6, f"win pnl net of fee should be 0.48, got {pnl3}"
    assert abs(fee3 - 0.02) < 1e-6, f"fee should be 0.02, got {fee3}"
    config.PAPER_FEE_FRAC = 0.0
    os.remove(path)
    print("PASS test_pnl_win_and_loss")


# ---------------------------------------------------------------------------
# Resolution decides by TOKEN INDEX, not label (the critical correctness point)
# ---------------------------------------------------------------------------
def test_resolution_by_index(monkeypatch=None):
    from polybot import market_data
    # Simulate the CLOB response: YES token wins
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"closed": True,
                    "tokens": [{"winner": True, "price": "1"},
                               {"winner": False, "price": "0"}]}
    orig = market_data.requests.get
    market_data.requests.get = lambda *a, **k: FakeResp()
    try:
        assert market_data.fetch_resolution("0xX") == "YES"
    finally:
        market_data.requests.get = orig
    print("PASS test_resolution_by_index")


def test_resolution_price_fallback():
    from polybot import market_data
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            # no winner flag, but closed and price collapsed -> NO won
            return {"closed": True,
                    "tokens": [{"price": "0.005"}, {"price": "0.995"}]}
    orig = market_data.requests.get
    market_data.requests.get = lambda *a, **k: FakeResp()
    try:
        assert market_data.fetch_resolution("0xX") == "NO"
    finally:
        market_data.requests.get = orig
    print("PASS test_resolution_price_fallback")


# ---------------------------------------------------------------------------
# Calibration table: measured data used when n large, market when no data
# ---------------------------------------------------------------------------
def test_calib_table():
    from polybot.calib_table import measured_no_win
    # confirmed exact-score bucket -> measured, above market-implied
    r = measured_no_win("Exact Score: A 1-0 B?", 0.39, 0.61)
    assert r["source"] == "measured" and r["n"] >= 25
    assert r["est"] > 0.61, "measured NO-win should exceed market-implied"
    assert r["est"] <= 0.97, "must respect hard cap"

    # non-longshot -> defers to market, no edge claimed
    r2 = measured_no_win("Will Brazil win?", 0.45, 0.55)
    assert r2["source"] == "market" and abs(r2["est"] - 0.55) < 1e-9
    print("PASS test_calib_table")


# ---------------------------------------------------------------------------
# Longshot classification + tier
# ---------------------------------------------------------------------------
def test_longshot_tiers():
    from polybot.longshot import _longshot_tier
    assert _longshot_tier("Exact Score: Canada 1-0 Bosnia?") == "confirmed"
    assert _longshot_tier("Spread: Brazil (-1.5)") == "exploratory"
    assert _longshot_tier("Map Handicap: VIT (-1.5)") == "exploratory"
    assert _longshot_tier("Will Brazil win?") is None
    print("PASS test_longshot_tiers")


# ---------------------------------------------------------------------------
# Lagwatch guard: tournament outrights must NOT match a finished single game
# ---------------------------------------------------------------------------
def test_lagwatch_single_match_guard():
    from polybot.lagwatch import _is_single_match
    assert _is_single_match("Will Brazil win on 2026-06-13?") is True
    assert _is_single_match("Brazil vs. Morocco") is True
    assert _is_single_match("Will Brazil win the 2026 FIFA World Cup?") is False
    assert _is_single_match("Will Spain win the World Cup?") is False
    print("PASS test_lagwatch_single_match_guard")


# ---------------------------------------------------------------------------
# limit_bid_price stays inside the book and below/at ask
# ---------------------------------------------------------------------------
def test_limit_bid_price_bounds():
    from polybot import market_data
    market_data.fetch_quote = lambda t: {"best_bid": 0.90, "best_ask": 0.96,
                                          "mid": 0.93, "spread": 0.06}
    q = market_data.limit_bid_price("tok", aggression=0.5)
    assert q["bid"] < q["price"] <= q["ask"], "bid price must be within the book"
    assert 0.93 <= q["price"] <= 0.96
    print("PASS test_limit_bid_price_bounds")


def test_fillable_depth():
    """Depth caps at prices <= max_price, summing USD correctly."""
    from polybot import market_data
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"asks": [
                {"price": "0.61", "size": "10"},   # $6.10  (<=0.62 ok)
                {"price": "0.62", "size": "20"},   # $12.40 (<=0.62 ok)
                {"price": "0.87", "size": "100"},  # excluded (> 0.62)
            ]}
    orig = market_data.requests.get
    market_data.requests.get = lambda *a, **k: FakeResp()
    try:
        d = market_data.fillable_depth("tok", max_price=0.62)
        assert d["levels"] == 2, d
        # 0.61*10 + 0.62*20 = 6.1 + 12.4 = 18.5
        assert abs(d["usd"] - 18.5) < 1e-6, d
        assert abs(d["shares"] - 30) < 1e-6, d
    finally:
        market_data.requests.get = orig
    print("PASS test_fillable_depth")


def test_stable_unit_reproducible():
    """The paper fill roll must be reproducible across processes (not salted)."""
    from polybot.market_data import stable_unit
    a = stable_unit("0xabc")
    b = stable_unit("0xabc")
    assert a == b, "stable_unit must be deterministic for the same key"
    assert 0.0 <= a < 1.0, a
    assert stable_unit("0xabc") != stable_unit("0xdef"), "different keys must differ"
    print("PASS test_stable_unit_reproducible")


def test_fill_prob_monotonic_in_band():
    """fill_prob: ~certain at the ask, low near the bid, penalized on wide spread."""
    import polybot.market_data as md
    orig = md.fetch_quote
    try:
        # tight spread 0.40/0.44
        def q_at(price):
            md.fetch_quote = lambda t: {"mid": 0.42, "best_ask": 0.44,
                                        "best_bid": 0.40, "spread": 0.04}
            agg = (price - 0.42) / (0.44 - 0.42)
            return md.limit_bid_price("t", aggression=agg)["fill_prob_estimate"]
        at_ask = q_at(0.44)
        at_mid = q_at(0.42)
        assert at_ask > at_mid, (at_ask, at_mid)
        assert at_ask > 0.8, at_ask          # crossing the ask ~ near-certain
        assert at_mid < 0.7, at_mid          # mid is genuinely uncertain
    finally:
        md.fetch_quote = orig
    print("PASS test_fill_prob_monotonic_in_band")


def test_open_exposure_is_true_open_stake():
    """bankroll.summary open_exposure must equal SUM(size_usd) of OPEN trades,
    not a cash-log derivation that drifts by realized P&L."""
    from polybot import config, store, bankroll
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = path
    store.init_db(); bankroll.init_bankroll()
    from polybot.strategy import Signal
    from polybot.market_data import Market
    m = Market(condition_id="0xE", question="Q", token_id_yes="y", token_id_no="n",
               price_yes=0.6, liquidity=1e5, volume=0, volume_24h=0, spread=0,
               end_date="", hours_to_resolution=1, category="soccer", event_title="")
    sig = Signal(market=m, side="NO", fair_prob=0.8, market_prob=0.4,
                 edge=0.4, size_usd=25.0, reason="t", estimator="test")
    store.record_trade(sig, {"mode": "PAPER", "status": "simulated"})
    assert abs(bankroll.summary()["open_exposure"] - 25.0) < 1e-6, bankroll.summary()
    print("PASS test_open_exposure_is_true_open_stake")


def test_shrinkage_is_two_sided():
    """A measured rate at/below the market NO price must NOT be floored at market
    — the data has to be able to veto a bet (produce <= market estimate)."""
    from polybot import calib_table as ct
    # Temporarily inject a deliberately pessimistic bucket.
    saved = ct.CALIB.get("exact_score")
    try:
        ct.CALIB["exact_score"] = [(0.99, 0.40, 100)]  # measured 0.40, big n
        # market NO = 0.60; with strong n the estimate must pull toward 0.40,
        # i.e. BELOW the market -> negative edge -> bet vetoed downstream.
        r = ct.measured_no_win("Exact Score: 2-1?", yes_price=0.40, implied_no=0.60)
        assert r["est"] < 0.60, f"two-sided shrinkage broken: est={r['est']} not < market 0.60"
    finally:
        if saved is not None:
            ct.CALIB["exact_score"] = saved
    print("PASS test_shrinkage_is_two_sided")


def test_home_runs_falls_back_to_market():
    """The unsupported home-runs bucket was removed; sizing must default to the
    market price (no fabricated edge)."""
    from polybot.calib_table import measured_no_win
    r = measured_no_win("Aaron Judge: Home Runs O/U 1.5", yes_price=0.45, implied_no=0.55)
    assert r["source"] == "market", r
    assert abs(r["est"] - 0.55) < 1e-9, r
    print("PASS test_home_runs_falls_back_to_market")


def test_drawdown_halt():
    """Circuit breaker trips when equity falls below the ruin floor."""
    from polybot import config, store, bankroll
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = path
    store.init_db(); bankroll.init_bankroll()
    assert not bankroll.drawdown_halted(), "fresh $500 account should not be halted"
    # Drain the bankroll below the floor (0.70 * 500 = 350).
    bankroll.deduct_stake(200.0, note="big loss sim")  # balance 300, no open bets
    assert bankroll.drawdown_halted(), "equity $300 < $350 floor should halt"
    print("PASS test_drawdown_halt")


def test_exposure_ceiling():
    """Aggregate open-exposure ceiling rejects bets that would exceed the cap."""
    from polybot import config, store, bankroll
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = path
    store.init_db(); bankroll.init_bankroll()
    # equity $500, cap 60% = $300 open exposure allowed
    assert bankroll.exposure_ok(100.0), "small bet within ceiling should be ok"
    assert not bankroll.exposure_ok(400.0), "bet exceeding 60% ceiling should be rejected"
    os.remove(path)
    print("PASS test_exposure_ceiling")


def test_fill_prob_rises_with_time():
    """A resting sub-ask limit should have higher fill prob with more time."""
    import polybot.market_data as md
    orig = md.fetch_quote
    try:
        md.fetch_quote = lambda t: {"mid": 0.42, "best_ask": 0.44,
                                    "best_bid": 0.40, "spread": 0.04}
        short = md.limit_bid_price("t", aggression=0.5, hours_to_res=2)["fill_prob_estimate"]
        long = md.limit_bid_price("t", aggression=0.5, hours_to_res=72)["fill_prob_estimate"]
        assert long > short, f"more time should raise fill prob: {long} !> {short}"
    finally:
        md.fetch_quote = orig
    print("PASS test_fill_prob_rises_with_time")


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} tests passed.")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
