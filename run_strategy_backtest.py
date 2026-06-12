"""
Backtest the longshot-fade strategy on the last N days of resolved markets.

    python run_strategy_backtest.py [days]

Replays our EXACT betting logic on markets that already resolved, so we know the
win rate / P&L the strategy WOULD have produced — confidence before risking money.
Also writes the result to backtest_result.json for the dashboard to display.
"""
import sys
import json
from polybot.strategy_backtest import backtest_longshot


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"Backtesting longshot-fade on the last {days} days of resolved markets...")
    print("(sampling price history — this takes a few minutes)\n")

    res = backtest_longshot(days=days)

    o = res["overall"]
    if not o:
        print("No qualifying historical bets found in this window.")
        return

    print("=" * 72)
    print(f"  LONGSHOT-FADE BACKTEST — last {days} days")
    print("=" * 72)
    print(f"  Simulated bets : {res['total_bets']}")
    print(f"  Win rate       : {o['win_rate']*100:.1f}%   "
          f"(95% CI {o['win_ci'][0]*100:.0f}-{o['win_ci'][1]*100:.0f}%)")
    print(f"  Predicted win  : {o['predicted_win']*100:.1f}%   "
          f"(our model's claim — compare to actual above)")
    print(f"  P&L            : ${o['pnl']:+.2f} on ${o['staked']:.2f} staked")
    print(f"  ROI            : {o['roi']*100:+.1f}%")
    print(f"  VERDICT        : {o['verdict']}")

    print("\n  By sub-type:")
    print(f"    {'subtype':16} {'n':>4} {'win%':>6} {'pred%':>6} {'ROI':>7} {'verdict'}")
    for sub, s in sorted(res["by_subtype"].items(), key=lambda x: -(x[1] or {}).get("n", 0)):
        if not s:
            continue
        print(f"    {sub:16} {s['n']:>4} {s['win_rate']*100:>5.0f}% "
              f"{s['predicted_win']*100:>5.0f}% {s['roi']*100:>+6.0f}% {s['verdict']}")

    # Persist for the dashboard
    import datetime
    res["generated"] = datetime.datetime.utcnow().isoformat()
    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    print("\n  (saved to backtest_result.json for the dashboard)")

    print("\n  HOW TO READ: if actual win% >= predicted win% AND ROI is positive")
    print("  AND the CI lower bound is > 50%, the edge is real and we can trust it.")
    print("  If actual << predicted, our calibration table was over-optimistic.")


if __name__ == "__main__":
    main()
