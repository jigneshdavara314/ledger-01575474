"""
DISABLED — do not use.

This module used to write a 30-day BACKTEST REPLAY into the live trades table as
if it were real paper trades. That contaminated the public dashboard: ~80% of
displayed bets and ~92% of displayed profit became simulation physically mixed
into the live ledger, with the bankroll no longer reconciling. A full-system
audit (2026-06) flagged this as a REGRESSION of an already-fixed dishonesty
(the earlier +$2,036 / +407% fake headline). See [[polymarket-audit-findings]].

THE RULE (do not break again): NEVER write backtest/simulated rows into the live
trades table or the bankroll. Backtests live in their OWN files
(edge_scan15_result.json, backtest_result.json) and are shown only on clearly
separate, labelled surfaces — never merged into the real ledger.

Calling run() raises, on purpose, so this can't silently re-contaminate.
"""


def run(*args, **kwargs):
    raise RuntimeError(
        "backfill_record is DISABLED: writing simulated bets into the live ledger "
        "is forbidden (it caused an audited honesty regression). Use the edge-scan "
        "/ backtest tools, which keep simulated results in their own files."
    )


if __name__ == "__main__":
    run()
