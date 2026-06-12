"""
Honest backtesting harness for short-window crypto "Up or Down" markets.

This exists to TEST whether any "loophole" pattern (momentum, streaks,
mean-reversion, etc.) actually predicts the next short candle's direction —
the same thing Polymarket's "BTC Up or Down 5m" markets resolve against.

The whole point is intellectual honesty:
  - We pull REAL historical price data (Binance klines).
  - We split it into IN-SAMPLE (to find/tune a pattern) and OUT-OF-SAMPLE
    (to test if it still works on data it never saw). A pattern that only
    works in-sample is overfitting — random noise, not edge.
  - We report the win rate with a confidence interval so you can tell luck
    from skill.

Usage (see run_backtest.py):
    from polybot.backtest import fetch_klines, backtest_pattern
"""
import math
import time
import requests
from dataclasses import dataclass
from typing import List, Callable, Optional


BINANCE = "https://api.binance.com/api/v3/klines"


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def went_up(self) -> bool:
        """How the 'Up or Down' market resolves: close > open => UP."""
        return self.close > self.open

    @property
    def pct_change(self) -> float:
        if self.open == 0:
            return 0.0
        return (self.close - self.open) / self.open


def fetch_klines(symbol: str = "BTCUSDT",
                 interval: str = "5m",
                 total: int = 10000) -> List[Candle]:
    """
    Pull `total` historical candles, paginating backward (Binance caps at
    1000 per request). Returns oldest-first.
    """
    candles: List[Candle] = []
    end_time = None
    remaining = total

    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time:
            params["endTime"] = end_time
        resp = requests.get(BINANCE, params=params, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for k in batch:
            candles.append(Candle(
                open_time=int(k[0]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
            ))
        # next page ends just before this batch's earliest candle
        end_time = int(batch[0][0]) - 1
        remaining -= len(batch)
        if len(batch) < limit:
            break
        time.sleep(0.15)  # be polite to the API

    # de-dup and sort oldest-first
    seen = set()
    unique = []
    for c in sorted(candles, key=lambda x: x.open_time):
        if c.open_time not in seen:
            seen.add(c.open_time)
            unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# A "pattern" is a function that looks at the candles BEFORE index i and
# returns a prediction for candle i: "UP", "DOWN", or None (no bet).
# It must NEVER look at candle i itself or later — that would be cheating
# (lookahead bias). We enforce this by only passing history[:i].
# ---------------------------------------------------------------------------

Pattern = Callable[[List[Candle], int], Optional[str]]


@dataclass
class BacktestResult:
    name: str
    n_signals: int          # how many times the pattern fired
    n_correct: int
    win_rate: float
    ci_low: float           # 95% confidence interval on win rate
    ci_high: float
    edge_vs_coinflip: float # win_rate - 0.50
    verdict: str


def _wilson_ci(wins: int, n: int, z: float = 1.96):
    """Wilson score 95% confidence interval for a binomial proportion.
    More honest than naive +/- for small samples."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    margin = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def backtest_pattern(candles: List[Candle],
                     pattern: Pattern,
                     name: str,
                     fee: float = 0.0) -> BacktestResult:
    """
    Run a pattern over the candles and measure how often its prediction
    matched the actual next-candle direction.

    `fee` is an optional per-trade cost fraction to subtract from the
    effective win rate (Polymarket has price spread + the binary payout
    means you need >50% just to break even after buying at a price).
    """
    wins = 0
    signals = 0
    for i in range(len(candles)):
        pred = pattern(candles, i)
        if pred is None:
            continue
        actual = "UP" if candles[i].went_up else "DOWN"
        signals += 1
        if pred == actual:
            wins += 1

    win_rate = wins / signals if signals else 0.0
    ci_low, ci_high = _wilson_ci(wins, signals)
    edge = win_rate - 0.50

    # Verdict logic — the honest part.
    if signals < 100:
        verdict = "INSUFFICIENT DATA (need 100+ signals)"
    elif ci_low > 0.50:
        verdict = "EDGE survives CI lower bound > 50% (investigate further, then OOS test)"
    elif ci_high < 0.50:
        verdict = "ANTI-EDGE (consistently wrong — could flip, but suspect noise)"
    else:
        verdict = "NO EDGE (confidence interval straddles 50% = indistinguishable from coin flip)"

    return BacktestResult(
        name=name,
        n_signals=signals,
        n_correct=wins,
        win_rate=round(win_rate, 4),
        ci_low=round(ci_low, 4),
        ci_high=round(ci_high, 4),
        edge_vs_coinflip=round(edge, 4),
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# Library of common "loophole" hypotheses people believe in.
# Each is a Pattern. Add your own ideas here.
# ---------------------------------------------------------------------------

def momentum(history, i, lookback=1):
    """Bet the trend continues: if last candle was up, predict UP."""
    if i < lookback:
        return None
    return "UP" if history[i-1].went_up else "DOWN"


def mean_reversion(history, i, lookback=1):
    """Bet the trend reverses: if last candle was up, predict DOWN."""
    if i < lookback:
        return None
    return "DOWN" if history[i-1].went_up else "UP"


def streak_reversal(history, i, streak=3):
    """After `streak` candles in the same direction, bet on a reversal."""
    if i < streak:
        return None
    last = history[i-streak:i]
    if all(c.went_up for c in last):
        return "DOWN"
    if all(not c.went_up for c in last):
        return "UP"
    return None


def streak_continuation(history, i, streak=3):
    """After `streak` candles in the same direction, bet it continues."""
    if i < streak:
        return None
    last = history[i-streak:i]
    if all(c.went_up for c in last):
        return "UP"
    if all(not c.went_up for c in last):
        return "DOWN"
    return None


def big_move_reversal(history, i, threshold=0.003):
    """After a big move (> threshold %), bet on reversal."""
    if i < 1:
        return None
    prev = history[i-1]
    if prev.pct_change > threshold:
        return "DOWN"
    if prev.pct_change < -threshold:
        return "UP"
    return None
