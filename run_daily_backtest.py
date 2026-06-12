"""
Day-by-day backtest of the "$500 daily float, skim profit" model over the
last N days of resolved markets.

    python run_daily_backtest.py [days]

Shows a daily P&L table and the total profit you'd have skimmed. Saves
daily_backtest.json for the dashboard.
"""
import sys
import json
from polybot.daily_backtest import run_daily_backtest


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"Day-by-day backtest of the $500-float model, last {days} days...")
    print("(sampling resolved-market price history — a few minutes)\n")

    res = run_daily_backtest(days=days)

    print("=" * 64)
    print(f"  MODEL: {res['model']}")
    print("=" * 64)
    print(f"  {'day':12} {'bets':>5} {'staked':>9} {'end bal':>9} {'profit':>9}")
    print("  " + "-" * 50)
    for d in res["daily"]:
        print(f"  {d['day']:12} {d['bets']:>5} ${d['staked']:>7.2f} "
              f"${d['end_balance']:>7.2f} ${d['profit']:>+7.2f}")
    print("  " + "-" * 50)
    print(f"  Days with action  : {res['days_with_action']} of {res['window_days']}")
    print(f"  Total bets        : {res['total_bets']}  (win rate {res['win_rate']*100:.0f}%)")
    print(f"  Total staked      : ${res['total_staked']:.2f}")
    print(f"  TOTAL PROFIT       : ${res['total_profit']:+.2f}")
    print(f"  Avg profit / active day : ${res['avg_daily_profit']:+.2f}")

    import datetime
    res["generated"] = datetime.datetime.utcnow().isoformat()
    with open("daily_backtest.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    print("\n  (saved to daily_backtest.json for the dashboard)")
    print("\n  NOTE: profit is the amount swept above $500 each day. Real live")
    print("  fills on thin markets may be smaller (no historical depth data),")
    print("  so treat this as the budget-scaled estimate, not a guarantee.")


if __name__ == "__main__":
    main()
