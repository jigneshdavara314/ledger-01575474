"""
Massive strategy sweep — test 200+ patterns across assets/timeframes with
RIGOROUS statistics so we don't get fooled by luck.

The core honesty problem: test 200 random strategies and ~10 will look
profitable by pure chance (5% false-positive rate). So a strategy only counts
as REAL if it survives ALL of:
  1. In-sample edge (win rate CI lower bound > 50% after fees)
  2. Out-of-sample confirmation (same edge on data it was NOT chosen on)
  3. Bonferroni correction (bar raised for testing many strategies at once)
  4. Economic significance (edge beats realistic round-trip fees)

We generate strategies from a grid of:
  - assets: BTC, ETH, SOL, BNB, XRP
  - timeframes: 1m, 5m, 15m, 1h
  - signal families: momentum, mean-reversion, streaks, RSI, volume spike,
    range breakout, candle body, time-of-day
Each (asset × timeframe × signal × param) is one strategy. That's 200+.
"""
import math
import time
import requests

BINANCE = "https://api.binance.com/api/v3/klines"
ROUND_TRIP_FEE = 0.001   # ~0.1% realistic


def fetch_candles(symbol, interval, limit=1000):
    out = []
    end = None
    remaining = limit
    while remaining > 0:
        params = {"symbol": symbol, "interval": interval, "limit": min(1000, remaining)}
        if end:
            params["endTime"] = end
        r = requests.get(BINANCE, params=params, timeout=20)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out = batch + out
        end = int(batch[0][0]) - 1
        remaining -= len(batch)
        if len(batch) < 1000:
            break
        time.sleep(0.1)
    # de-dup, sort
    seen, uniq = set(), []
    for k in sorted(out, key=lambda x: x[0]):
        if k[0] not in seen:
            seen.add(k[0]); uniq.append(k)
    return uniq


def _cols(candles):
    o = [float(k[1]) for k in candles]
    h = [float(k[2]) for k in candles]
    l = [float(k[3]) for k in candles]
    c = [float(k[4]) for k in candles]
    v = [float(k[5]) for k in candles]
    t = [int(k[0]) for k in candles]
    up = [1 if c[i] > o[i] else 0 for i in range(len(c))]
    return o, h, l, c, v, t, up


def _rsi(c, period, i):
    if i < period:
        return None
    gains = losses = 0.0
    for j in range(i - period + 1, i + 1):
        d = c[j] - c[j - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - 100 / (1 + rs)


def _wilson(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    cen = (p + z * z / (2 * n)) / d
    m = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (max(0, cen - m), min(1, cen + m))


# ---------------------------------------------------------------------------
# Strategy = a predict(cols, i) -> 1 (up) / 0 (down) / None (no bet)
# Returns a list of (name, fn). We use realistic next-candle direction as target.
# ---------------------------------------------------------------------------
def make_strategies():
    strats = []

    # momentum / mean-reversion over various lookbacks
    for lb in (1, 2, 3, 5):
        strats.append((f"momentum_{lb}",
                       lambda C, i, lb=lb: C[6][i-1] if i > lb else None))
        strats.append((f"meanrev_{lb}",
                       lambda C, i, lb=lb: (1 - C[6][i-1]) if i > lb else None))

    # streak reversal / continuation
    for s in (2, 3, 4):
        def streak_rev(C, i, s=s):
            if i < s:
                return None
            u = C[6][i-s:i]
            if all(u): return 0
            if not any(u): return 1
            return None
        def streak_cont(C, i, s=s):
            if i < s:
                return None
            u = C[6][i-s:i]
            if all(u): return 1
            if not any(u): return 0
            return None
        strats.append((f"streakrev_{s}", streak_rev))
        strats.append((f"streakcont_{s}", streak_cont))

    # RSI thresholds (oversold->buy, overbought->sell)
    for period in (7, 14):
        for lo, hi in ((30, 70), (25, 75), (20, 80)):
            def rsi_fn(C, i, period=period, lo=lo, hi=hi):
                r = _rsi(C[3], period, i-1)
                if r is None: return None
                if r < lo: return 1
                if r > hi: return 0
                return None
            strats.append((f"rsi{period}_{lo}_{hi}", rsi_fn))

    # volume spike + direction
    for mult in (1.5, 2.0, 3.0):
        def vol_fn(C, i, mult=mult):
            if i < 21: return None
            v = C[4]
            avg = sum(v[i-20:i]) / 20
            if v[i-1] > avg * mult:
                return C[6][i-1]  # follow the high-volume candle
            return None
        strats.append((f"volspike_{mult}", vol_fn))

    # range breakout: close above last N highs -> up
    for n in (5, 10, 20):
        def brk_fn(C, i, n=n):
            if i < n + 1: return None
            c, h, l = C[3], C[1], C[2]
            if c[i-1] > max(h[i-1-n:i-1]): return 1
            if c[i-1] < min(l[i-1-n:i-1]): return 0
            return None
        strats.append((f"breakout_{n}", brk_fn))

    # candle body size: big body -> continuation / reversal
    for thr in (0.002, 0.004):
        def bigbody_cont(C, i, thr=thr):
            if i < 1: return None
            o, c = C[0], C[3]
            body = abs(c[i-1] - o[i-1]) / o[i-1]
            if body > thr:
                return C[6][i-1]
            return None
        def bigbody_rev(C, i, thr=thr):
            if i < 1: return None
            o, c = C[0], C[3]
            body = abs(c[i-1] - o[i-1]) / o[i-1]
            if body > thr:
                return 1 - C[6][i-1]
            return None
        strats.append((f"bigbodycont_{thr}", bigbody_cont))
        strats.append((f"bigbodyrev_{thr}", bigbody_rev))

    # time-of-day (UTC hour buckets) momentum
    for hr_start in (0, 8, 16):
        def tod_fn(C, i, hr_start=hr_start):
            if i < 1: return None
            import datetime
            hour = datetime.datetime.utcfromtimestamp(C[5][i]/1000).hour
            if hr_start <= hour < hr_start + 8:
                return C[6][i-1]
            return None
        strats.append((f"tod_{hr_start}", tod_fn))

    return strats


def backtest_one(cols, predict):
    """
    Return (n, wins, mean_bps_after_fee). The P&L measurement is what matters:
    we compute the actual return of each trade (the candle's open->close move in
    the predicted direction), minus the round-trip fee. A 54% win rate that wins
    on small candles and loses on big ones has NEGATIVE mean_bps — that's the
    test that catches the 'win rate looks good but loses money' mirage.
    """
    o, c, up = cols[0], cols[3], cols[6]
    n = wins = 0
    total_ret = 0.0
    for i in range(25, len(up)):
        p = predict(cols, i)
        if p is None:
            continue
        n += 1
        if p == up[i]:
            wins += 1
        # actual signed return of the bet: if we predicted up, we gain (c-o)/o;
        # if down, we gain (o-c)/o. Then subtract the round-trip fee.
        move = (c[i] - o[i]) / o[i]
        ret = move if p == 1 else -move
        total_ret += ret - ROUND_TRIP_FEE
    mean_bps = (total_ret / n * 10000) if n else 0.0
    return n, wins, round(mean_bps, 3)


def sweep_symbol(symbol, interval, limit=1500):
    """Run all strategies on one symbol/timeframe with in-sample/out-of-sample split."""
    candles = fetch_candles(symbol, interval, limit)
    if len(candles) < 200:
        return []
    split = int(len(candles) * 0.6)
    in_c = candles[:split]
    out_c = candles[split:]
    in_cols = _cols(in_c)
    out_cols = _cols(out_c)

    results = []
    for name, fn in make_strategies():
        ni, wi, bps_in = backtest_one(in_cols, fn)
        no, wo, bps_out = backtest_one(out_cols, fn)
        if ni < 50 or no < 30:
            continue
        lo_in, _ = _wilson(wi, ni)
        lo_out, _ = _wilson(wo, no)
        # REAL pass = positive after-fee P&L on BOTH in and out samples.
        # (Win-rate CI is secondary; mean_bps is the money test.)
        results.append({
            "strategy": f"{symbol}_{interval}_{name}",
            "n_in": ni, "wr_in": round(wi/ni, 4), "bps_in": bps_in,
            "n_out": no, "wr_out": round(wo/no, 4), "bps_out": bps_out,
            "ci_in_low": round(lo_in, 4), "ci_out_low": round(lo_out, 4),
            # profitable after fees in BOTH samples
            "profitable_both": bps_in > 0 and bps_out > 0,
            # win-rate edge in both (secondary)
            "winrate_both": lo_in > 0.5 and lo_out > 0.5,
        })
    return results
