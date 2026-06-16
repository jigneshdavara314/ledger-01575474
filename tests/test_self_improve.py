"""
Tests for the self-improvement composition loop (promote/trial/graduate) and the
single-source taxonomy. These cover the previously-untested logic that actually
moves money over time.
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _isolated_si():
    """Point self_improve at temp state/log/history files so tests never touch
    the real git-tracked strategy_state.json."""
    from polybot import self_improve as si
    d = tempfile.mkdtemp()
    si.STATE_PATH = os.path.join(d, "state.json")
    si.LOG_PATH = os.path.join(d, "log.jsonl")
    si.HISTORY_PATH = os.path.join(d, "hist.jsonl")
    return si, d


def _write_hist(si, cell, days, **stats):
    rows = []
    for dd in range(days):
        day = f"2026-06-{10+dd:02d}"
        rows.append(json.dumps({"ts": day + "T06:00:00Z",
                                "passed": [{"cell": cell, "n": stats.get("n", 200),
                                            "win_rate": stats.get("win_rate", 0.76),
                                            "ev": 0.2,
                                            "wilson_lower": stats.get("wl", 0.67)}]}))
    open(si.HISTORY_PATH, "w").write("\n".join(rows) + "\n")


def test_trial_needs_two_days():
    si, _ = _isolated_si()
    cell = "over_under | pay NO 0.55-0.75"
    # 1 day -> no promotion
    _write_hist(si, cell, 1)
    st = {"tiers": {}, "disabled": [], "warnings": {}}
    si.promote(st)
    assert cell not in st["tiers"], "1 day must NOT promote"
    # 2 days -> trial
    _write_hist(si, cell, 2)
    st = {"tiers": {}, "disabled": [], "warnings": {}}
    si.promote(st)
    assert st["tiers"].get(cell, {}).get("tier") == "trial", "2 days -> trial"
    print("PASS test_trial_needs_two_days")


def test_graduate_at_five_days():
    si, _ = _isolated_si()
    cell = "over_under | pay NO 0.55-0.75"
    _write_hist(si, cell, 5)
    st = {"tiers": {}, "disabled": [], "warnings": {}}
    si.promote(st)
    assert st["tiers"][cell]["tier"] == "exploratory", "5 days -> exploratory"
    print("PASS test_graduate_at_five_days")


def test_promote_carries_measured_stats():
    """Promotion must persist the scan-measured Wilson LB + direction/band so the
    live sizing bridge can actually use the rigorous number."""
    si, _ = _isolated_si()
    cell = "over_under | pay NO 0.55-0.75"
    _write_hist(si, cell, 2, wl=0.67, n=250)
    st = {"tiers": {}, "disabled": [], "warnings": {}}
    si.promote(st)
    cfg = st["tiers"][cell]
    assert cfg["direction"] == "NO"
    assert abs(cfg["band_lo"] - 0.55) < 1e-9 and abs(cfg["band_hi"] - 0.75) < 1e-9
    assert abs(cfg["measured_wilson_lower"] - 0.67) < 1e-9
    print("PASS test_promote_carries_measured_stats")


def test_atomic_save_load_roundtrip():
    si, _ = _isolated_si()
    si.save_state({"tiers": {"x": {"mult": 0.5}}, "disabled": [], "warnings": {}})
    assert si.load_state()["tiers"]["x"]["mult"] == 0.5
    print("PASS test_atomic_save_load_roundtrip")


def test_taxonomy_single_source_agreement():
    """family_of must be identical across discovery modules (no drift) and the
    scanner keyword map must cover every bettable family it returns."""
    from polybot import taxonomy, edge_scan15, edge_hunt
    samples = ["Exact Score: 2-1?", "Spread: Germany (-1.5)", "Match O/U 2.5",
               "Aaron Judge: Home Runs O/U 1.5", "Map 3 Rounds Handicap: NAVI",
               "Bitcoin Up or Down?", "Elon posts from 100-120"]
    for q in samples:
        a = taxonomy.family_of(q)
        assert edge_scan15.family_of(q) == a == edge_hunt.family_of(q), f"drift on {q!r}"
    # crypto is quarantined and must never be a bettable (keyword-mapped) family
    assert "crypto_pricetail" not in taxonomy.FAMILY_KEYWORDS
    print("PASS test_taxonomy_single_source_agreement")


def test_stats_numeric():
    """Direct numeric checks on the centralized stats primitives."""
    from polybot import stats
    # Wilson LB sits below the observed rate and widens as n shrinks
    assert stats.wilson_lower(90, 100) < 0.90
    assert stats.wilson_lower(9, 10) < stats.wilson_lower(90, 100)  # thinner -> lower
    # known value: 28/29 -> ~0.83 LB
    assert 0.80 < stats.wilson_lower(28, 29) < 0.86
    # Bonferroni z grows with the number of tests; 1 test = one-sided 95% ~1.645
    assert stats.bonferroni_z(1) < stats.bonferroni_z(70) < 4.0
    assert abs(stats.bonferroni_z(1) - 1.645) < 0.02   # one-sided 95%
    # two-sided CI brackets the rate
    lo, hi = stats.wilson_ci(50, 100)
    assert lo < 0.5 < hi
    print("PASS test_stats_numeric")


def test_grab_bag_and_crypto_never_promote():
    """The 'other' catch-all and quarantined crypto must NEVER auto-promote,
    even if they recur past the day threshold."""
    si, _ = _isolated_si()
    for fam in ("other", "crypto_pricetail"):
        cell = f"{fam} | pay NO 0.55-0.75"
        _write_hist(si, cell, 9)               # way over threshold
        st = {"tiers": {}, "disabled": [], "warnings": {}}
        si.promote(st)
        assert cell not in st["tiers"], f"{fam} must never promote"
    print("PASS test_grab_bag_and_crypto_never_promote")


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
    print(f"\n{len(fns)-failed}/{len(fns)} self-improve tests passed.")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
