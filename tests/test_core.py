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


def test_resolution_void_50_50():
    """A closed market with NO winner and a 50-50 flag is a VOID (cancelled/
    refunded), not 'unresolved'. Returning None here was the bug that froze
    positions OPEN for days."""
    from polybot import market_data
    class FlagResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"closed": True, "is_50_50_outcome": True,
                    "tokens": [{"winner": False, "price": "0.5"},
                               {"winner": False, "price": "0.5"}]}
    class PriceResp:  # no flag, but both prices parked at ~0.5
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"closed": True,
                    "tokens": [{"winner": False, "price": "0.5"},
                               {"winner": False, "price": "0.5"}]}
    orig = market_data.requests.get
    try:
        market_data.requests.get = lambda *a, **k: FlagResp()
        assert market_data.fetch_resolution("0xV1") == "VOID"
        market_data.requests.get = lambda *a, **k: PriceResp()
        assert market_data.fetch_resolution("0xV2") == "VOID"
    finally:
        market_data.requests.get = orig
    print("PASS test_resolution_void_50_50")


def test_settle_void_refunds_stake():
    """A VOID settlement refunds the FULL stake, books 0 P&L, and marks the
    trade VOID (not WON/LOST) so win-rate stats aren't polluted."""
    from polybot import config, store, bankroll
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = path
    config.PAPER_FEE_FRAC = 0.0
    store.init_db(); bankroll.init_bankroll()
    from polybot.strategy import Signal
    from polybot.market_data import Market
    m = Market(condition_id="0xV", question="Q", token_id_yes="y", token_id_no="n",
               price_yes=0.5, liquidity=1e5, volume=0, volume_24h=0, spread=0,
               end_date="", hours_to_resolution=1, category="esports", event_title="")
    sig = Signal(market=m, side="NO", fair_prob=0.8, market_prob=0.5,
                 edge=0.3, size_usd=12.0, reason="t", estimator="test")
    store.record_trade(sig, {"mode": "PAPER", "status": "simulated", "price": 0.5})
    bankroll.deduct_stake(12.0, note="bet")          # stake leaves cash -> 488
    tid = store.open_positions()[0][0]
    refund = store.settle_void(tid, size_usd=12.0)
    assert abs(refund - 12.0) < 1e-6, refund
    bk = bankroll.summary()
    assert abs(bk["balance"] - 500.0) < 1e-6, bk["balance"]   # full refund
    s = store.performance_summary()
    # a void is neither a win nor a loss
    assert s.get("won", 0) == 0 and s.get("lost", 0) == 0, s
    assert store.open_positions() == []                       # no longer OPEN
    os.remove(path)
    print("PASS test_settle_void_refunds_stake")


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
    # soccer player props (discovered edge, now a named bettable family)
    assert _longshot_tier("Kylian Mbappe: 2+ goals") == "exploratory"
    assert _longshot_tier("Bradley Barcola: 3+ shots") == "exploratory"
    print("PASS test_longshot_tiers")


def test_player_prop_classification_and_band_gate():
    """The discovered 'other | pay NO 0.55-0.75' edge is soccer player props.
    They must (1) classify as player_prop (out of 'other', so promotable +
    keyword-bridged) and (2) the calibration must claim the fade edge ONLY in the
    archive-validated NO 0.55-0.75 band and VETO the catastrophic high-NO band
    (NO<0.45 won just 4.3% -> -0.89 EV)."""
    from polybot.taxonomy import family_of, FAMILY_KEYWORDS
    from polybot.calib_table import measured_no_win

    # (1) classification — soccer props leave 'other'; team totals/halftime do not.
    assert family_of("Kylian Mbappe: 2+ goals") == "player_prop"
    assert family_of("Marcus Thuram: 1+ assists") == "player_prop"
    assert family_of("Bradley Barcola: 3+ shots") == "player_prop"
    assert family_of("France leading at halftime?") != "player_prop"
    assert family_of("Belgium vs Egypt: O/U 2.5") == "over_under"
    # keyword bridge exists so a promoted player_prop cell can match live markets
    assert any("+ goals" in k for k in FAMILY_KEYWORDS["player_prop"])

    # (2) band gating. YES 0.30 -> NO 0.70 is IN the measured band -> claims edge.
    w_good = measured_no_win("Mbappe: 2+ goals", 0.30, 0.70)
    assert w_good["est"] > 0.70, w_good            # edge claimed (NO undervalued)
    assert w_good["source"] in ("measured", "blended")
    # YES 0.70 -> NO 0.30 is the CATASTROPHIC band -> NO edge (defers to market).
    w_bad = measured_no_win("Mbappe: 2+ goals", 0.70, 0.30)
    assert w_bad["source"] == "market" and abs(w_bad["est"] - 0.30) < 1e-9, w_bad
    # YES 0.50 -> NO 0.50 (between bands) -> also no edge claimed.
    w_mid = measured_no_win("Mbappe: 2+ goals", 0.50, 0.50)
    assert w_mid["source"] == "market", w_mid
    print("PASS test_player_prop_classification_and_band_gate")


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
    orig = market_data.fetch_quote                 # restore: leaking this stub
    market_data.fetch_quote = lambda t: {"best_bid": 0.90, "best_ask": 0.96,
                                          "mid": 0.93, "spread": 0.06}
    try:
        q = market_data.limit_bid_price("tok", aggression=0.5)
        assert q["bid"] < q["price"] <= q["ask"], "bid price must be within the book"
        assert 0.93 <= q["price"] <= 0.96
    finally:
        market_data.fetch_quote = orig
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


def test_fade_sizing_not_zero_at_sub_ask_bid():
    """REGRESSION: a fade bid rests BELOW the ask, but depth almost always sits
    AT the ask. Sizing on depth<=bid returned $0 for ~every market and silently
    zeroed every stake (the bug that froze longshot betting after 2026-06-13).
    Sizing must use depth up to the ASK so a genuine, liquid +edge market is
    actually bettable. We build a market with a real edge and a normal book
    (size resting at the best ask, none below it) and assert a non-zero stake."""
    import polybot.longshot as L
    from polybot import config, market_data, store, bankroll
    import tempfile, os
    # Isolate from any DB/fee state leaked by earlier tests (which mutate
    # config.DB_PATH and then delete the file) — _self_improve_mult reads the DB.
    fd, _p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = _p
    config.PAPER_FEE_FRAC = 0.0
    store.init_db(); bankroll.init_bankroll()
    from polybot.market_data import Market

    # One liquid, in-band, confirmed-tier market with a real measured edge.
    mkt = Market(condition_id="0xEDGE",
                 question="Exact Score: Any Other Score?",
                 token_id_yes="y", token_id_no="n",
                 price_yes=0.445, liquidity=73000.0, volume=0, volume_24h=0,
                 spread=0.01, end_date="", hours_to_resolution=24.0,
                 category="soccer", event_title="")

    # Patch the exact functions find_longshot_fades calls (robust to any leaked
    # fetch_quote/requests stub from other tests). A NORMAL book: bid 0.55,
    # ask 0.56, depth (12k) resting AT the ask, NOTHING below it.
    orig_fetch = L.fetch_short_term_markets
    orig_bid = L.limit_bid_price
    orig_depth = L.fillable_depth
    L.fetch_short_term_markets = lambda *a, **k: [mkt]
    L.limit_bid_price = lambda tok, aggression=0.5, hours_to_res=None: {
        "price": 0.558, "mid": 0.555, "ask": 0.56, "bid": 0.55,
        "spread": 0.01, "fill_prob_estimate": 0.7}
    # depth ONLY at/below max_price: empty below the ask, full at the ask.
    L.fillable_depth = lambda tok, max_price: (
        {"usd": 6720.0, "shares": 12000.0, "levels": 1, "best_ask": 0.56}
        if max_price >= 0.56 else
        {"usd": 0.0, "shares": 0.0, "levels": 0, "best_ask": 0.56})
    try:
        sigs = L.find_longshot_fades()
        assert len(sigs) == 1, f"expected 1 bettable signal, got {len(sigs)}"
        s = sigs[0]
        assert s.size_usd >= config.LONGSHOT_MIN_STAKE, s.size_usd
        assert s.fillable_usd > 0, ("sizing collapsed to $0 — the sub-ask-depth "
                                    "bug is back")
        assert s.est_win_prob - s.bid_price >= config.LONGSHOT_MIN_EDGE
    finally:
        L.fetch_short_term_markets = orig_fetch
        L.limit_bid_price = orig_bid
        L.fillable_depth = orig_depth
        try:
            os.remove(_p)
        except OSError:
            pass
    print("PASS test_fade_sizing_not_zero_at_sub_ask_bid")


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


def test_promoted_edge_bridge():
    """A self-promoted NO-direction edge must feed its scan-measured Wilson LB
    into live sizing (only inside its band), and YES-direction cells must NOT bet.

    Uses a TEMP state file so the test never touches the real (git-tracked)
    strategy_state.json — a prior version reset it to {} and could wipe live tiers.
    """
    from polybot import self_improve as si, longshot
    orig_path = si.STATE_PATH
    fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
    si.STATE_PATH = tmp
    try:
        si.save_state({"tiers": {"over_under | pay NO 0.55-0.75": {
            "tier": "trial", "direction": "NO", "band_lo": 0.55, "band_hi": 0.75,
            "measured_win": 0.76, "measured_wilson_lower": 0.67, "measured_n": 250}},
            "disabled": [], "warnings": {}})
        pw = longshot._promoted_win("belgium vs egypt: o/u 2.5", 0.64)
        assert pw is not None and abs(pw["wl"] - 0.67) < 1e-9, pw
        assert longshot._promoted_win("belgium vs egypt: o/u 2.5", 0.40) is None
        si.save_state({"tiers": {"over_under | pay YES 0.75-0.95": {
            "tier": "trial", "direction": "YES", "band_lo": 0.75, "band_hi": 0.95,
            "measured_wilson_lower": 0.85, "measured_n": 90}},
            "disabled": [], "warnings": {}})
        assert longshot._promoted_win("match o/u 2.5", 0.80) is None
        # YES-direction promoted edge must be readable via the YES bridge (and NOT
        # leak into the NO bridge) — this is what promoted_only bets.
        py = longshot._promoted_win_yes("match o/u 2.5", 0.80)
        assert py is not None and abs(py["wl"] - 0.85) < 1e-9, py
        assert longshot._promoted_win("match o/u 2.5", 0.80) is None  # not NO
    finally:
        si.STATE_PATH = orig_path
        os.remove(tmp)
    print("PASS test_promoted_edge_bridge")


def test_promoted_only_keeps_yes_direction_edge():
    """REGRESSION (lever #1): promoted_only filters on win_source=='promoted'. A
    YES-direction promoted edge produces a BUY-YES signal tagged win_source=
    'promoted', so promoted_only must KEEP it (it was previously dropped only
    because the depth bug zeroed every signal, not a direction bug)."""
    import polybot.longshot as L
    from polybot import self_improve as si, config
    from polybot.strategies import get
    from polybot.market_data import Market
    import tempfile, os

    orig_path = si.STATE_PATH
    fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
    si.STATE_PATH = tmp
    # one liquid over_under market with YES priced inside a promoted YES band
    mkt = Market(condition_id="0xOU", question="Belgium vs Egypt: O/U 2.5",
                 token_id_yes="y", token_id_no="n", price_yes=0.80, liquidity=5e4,
                 volume=0, volume_24h=0, spread=0.01, end_date="",
                 hours_to_resolution=10.0, category="soccer", event_title="")
    orig_fetch = L.fetch_short_term_markets
    orig_bid = L.limit_bid_price
    orig_depth = L.fillable_depth
    try:
        si.save_state({"tiers": {"over_under | pay YES 0.75-0.95": {
            "tier": "trial", "direction": "YES", "band_lo": 0.75, "band_hi": 0.95,
            "measured_wilson_lower": 0.92, "measured_n": 120}},
            "disabled": [], "warnings": {}})
        L.fetch_short_term_markets = lambda *a, **k: [mkt]
        L.limit_bid_price = lambda tok, aggression=0.5, hours_to_res=None: {
            "price": 0.802, "mid": 0.80, "ask": 0.805, "bid": 0.795,
            "spread": 0.01, "fill_prob_estimate": 0.7}
        L.fillable_depth = lambda tok, max_price: (
            {"usd": 9000.0, "shares": 11000.0, "levels": 1, "best_ask": 0.805}
            if max_price >= 0.805 else
            {"usd": 0.0, "shares": 0.0, "levels": 0, "best_ask": 0.805})

        cfg = get("promoted_only")
        fades = L.find_longshot_fades(min_edge=cfg["min_edge"],
                                      band_lo=cfg["band_lo"], band_hi=cfg["band_hi"])
        if cfg.get("tiers"):
            fades = [f for f in fades if f.tier in cfg["tiers"]]
        if cfg.get("promoted_only"):
            fades = [f for f in fades if f.win_source == "promoted"]
        assert len(fades) == 1, f"promoted_only should keep the YES edge, got {len(fades)}"
        assert fades[0].side == "YES" and fades[0].win_source == "promoted"
    finally:
        si.STATE_PATH = orig_path
        L.fetch_short_term_markets = orig_fetch
        L.limit_bid_price = orig_bid
        L.fillable_depth = orig_depth
        os.remove(tmp)
    print("PASS test_promoted_only_keeps_yes_direction_edge")


def test_crypto_quarantine_and_subfamilies():
    """Crypto up/down must be quarantined (never a bettable family); player/esports
    props must carve out of 'other'; crypto family must NOT be in the scanner."""
    from polybot.edge_scan15 import family_of
    from polybot.longshot import _FAMILY_KEYWORDS
    assert family_of("Bitcoin Up or Down today?") == "crypto_pricetail"
    assert family_of("Will ETH be above $4000?") == "crypto_pricetail"
    assert family_of("Aaron Judge: Home Runs O/U 1.5") == "player_prop"
    assert family_of("First Blood: Team Vitality") == "esports_prop"
    assert family_of("Belgium vs Egypt: O/U 2.5") == "over_under"
    # the live scanner must never recognize crypto as a bettable family
    assert "crypto_pricetail" not in _FAMILY_KEYWORDS
    print("PASS test_crypto_quarantine_and_subfamilies")


def test_headline_excludes_simulated_rows():
    """Honesty guard: performance_summary / real_daily_equity must NEVER count
    simulated backfill (bf-) rows, so the public dashboard can't overstate again."""
    from polybot import config, store
    import tempfile, os, datetime
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = path
    store.init_db()
    import sqlite3
    c = sqlite3.connect(path)
    now = datetime.datetime.utcnow().isoformat()
    # one REAL win (+small) and one fake simulated bf- win (+huge)
    c.execute("INSERT INTO trades (ts,mode,condition_id,question,side,market_prob,size_usd,shares,status,exec_status,pnl_usd,resolved_ts,category) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (now,'PAPER','real-1','Real bet','NO',0.6,10,16.6,'WON','simulated',6.6,now,'soccer'))
    c.execute("INSERT INTO trades (ts,mode,condition_id,question,side,market_prob,size_usd,shares,status,exec_status,pnl_usd,resolved_ts,category) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (now,'PAPER','bf-1','Sim bet','NO',0.5,999,1998,'WON','backfill',999,now,'soccer'))
    c.commit(); c.close()
    s = store.performance_summary()
    assert s['resolved'] == 1, f"only the 1 real trade should count, got {s['resolved']}"
    assert abs(s['pnl_usd'] - 6.6) < 1e-6, f"sim +999 must be excluded, pnl={s['pnl_usd']}"
    cats = store.category_summary()
    assert sum(r[4] for r in cats) < 100, "category pnl must exclude the sim row"
    os.remove(path)
    print("PASS test_headline_excludes_simulated_rows")


def test_settle_and_credit_atomic():
    """settle_and_credit must move the trade status AND the bankroll cash in one
    transaction, keeping ledger P&L and bankroll balance reconciled."""
    from polybot import config, store, bankroll
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    config.DB_PATH = path
    config.PAPER_FEE_FRAC = 0.0
    store.init_db(); bankroll.init_bankroll()
    from polybot.strategy import Signal
    from polybot.market_data import Market
    m = Market(condition_id="0xA", question="Q", token_id_yes="y", token_id_no="n",
               price_yes=0.5, liquidity=1e5, volume=0, volume_24h=0, spread=0,
               end_date="", hours_to_resolution=1, category="soccer", event_title="")
    sig = Signal(market=m, side="NO", fair_prob=0.8, market_prob=0.5,
                 edge=0.3, size_usd=10.0, reason="t", estimator="test")
    store.record_trade(sig, {"mode": "PAPER", "status": "simulated", "price": 0.5})
    bankroll.deduct_stake(10.0, note="bet")          # stake leaves cash
    tid = store.open_positions()[0][0]
    shares = 20.0  # $10 at 0.50
    pnl, fee = store.settle_and_credit(tid, won=True, size_usd=10.0, shares=shares)
    assert abs(pnl - 10.0) < 1e-6, pnl                # 20 payout - 10 stake
    # trade is now resolved AND the $20 payout is back in cash
    s = store.performance_summary()
    assert s["resolved"] == 1 and s["won"] == 1
    bk = bankroll.summary()
    # started 500, -10 stake, +20 payout = 510
    assert abs(bk["balance"] - 510.0) < 1e-6, bk["balance"]
    os.remove(path)
    print("PASS test_settle_and_credit_atomic")


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
