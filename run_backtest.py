"""
Backtest "loophole" hypotheses for short-window crypto Up/Down markets.

    python run_backtest.py                 # BTC 5m, 10000 candles
    python run_backtest.py ETHUSDT 1m 20000

This is the honest test: does ANY simple pattern predict the next short
candle's direction better than a coin flip — and does it SURVIVE on data
it wasn't tuned on?
"""
import sys
from polybot.backtest import (
    fetch_klines, backtest_pattern,
    momentum, mean_reversion, streak_reversal, streak_continuation, big_move_reversal,
)


def main():
    symbol   = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "5m"
    total    = int(sys.argv[3]) if len(sys.argv) > 3 else 10000

    print(f"Fetching {total} {interval} candles for {symbol}...")
    candles = fetch_klines(symbol, interval, total)
    print(f"Got {len(candles)} candles "
          f"({candles[0].open_time} .. {candles[-1].open_time})\n")

    # Baseline: how often does it just go UP? (the naive 'always UP' rate)
    ups = sum(1 for c in candles if c.went_up)
    print(f"Base rate: {ups}/{len(candles)} candles went UP "
          f"= {ups/len(candles)*100:.2f}%  (this is your real 'coin' — note it's "
          f"close to 50%)\n")

    # Define the hypotheses to test
    patterns = [
        ("momentum (follow last candle)",      lambda h, i: momentum(h, i)),
        ("mean-reversion (fade last candle)",  lambda h, i: mean_reversion(h, i)),
        ("3-streak reversal",                  lambda h, i: streak_reversal(h, i, 3)),
        ("3-streak continuation",              lambda h, i: streak_continuation(h, i, 3)),
        ("4-streak reversal",                  lambda h, i: streak_reversal(h, i, 4)),
        ("big-move reversal (>0.3%)",          lambda h, i: big_move_reversal(h, i, 0.003)),
    ]

    # --- Split for out-of-sample validation ---
    split = int(len(candles) * 0.6)
    in_sample  = candles[:split]
    out_sample = candles[split:]
    print(f"Split: {len(in_sample)} in-sample (tune) | {len(out_sample)} out-of-sample (test)\n")

    print("=" * 92)
    print(f"{'pattern':34} {'sample':5} {'signals':>8} {'win%':>7} {'95% CI':>16} {'verdict'}")
    print("=" * 92)

    for name, fn in patterns:
        r_in  = backtest_pattern(in_sample,  fn, name)
        r_out = backtest_pattern(out_sample, fn, name)
        for tag, r in [("IN", r_in), ("OUT", r_out)]:
            ci = f"[{r.ci_low*100:.1f},{r.ci_high*100:.1f}]"
            verdict = r.verdict.split(" (")[0]
            print(f"{name:34} {tag:5} {r.n_signals:>8} "
                  f"{r.win_rate*100:>6.2f}% {ci:>16}  {verdict}")
        print("-" * 92)

    print("\nHOW TO READ THIS:")
    print("  - 'win%' near 50 with a CI that straddles 50 = NO EDGE (coin flip).")
    print("  - A pattern only counts if its CI lower bound is > 50 in BOTH")
    print("    in-sample AND out-of-sample. If it works IN but not OUT, it was")
    print("    overfitting — random noise that won't make money.")
    print("  - On Polymarket you also pay spread, so you need ~53%+ just to break")
    print("    even. A 50.8% 'edge' is not tradeable even if it were real.")


if __name__ == "__main__":
    main()
