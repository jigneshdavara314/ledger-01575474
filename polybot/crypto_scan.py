"""
Crypto edge scanners — honest tests of whether ANY fast-crypto strategy has a
real, after-cost edge. Each function returns structured numbers, not hype.

Strategies probed:
  1. recent_pattern_backtest  — re-run pattern tests on the LAST FEW DAYS only
  2. cross_exchange_spread    — Binance vs Coinbase price gap (arb candidate)
  3. funding_rate_scan        — perp funding rates (cash-and-carry edge)
  4. triangular_scan          — BTC->ETH->USDT loop within Binance
  5. stablecoin_depeg         — USDC/USDT deviation from $1

Honest accounting: every "edge" is compared against realistic costs:
  - taker fee ~0.04-0.10% per side (so ~0.1-0.2% round trip)
  - your latency ~300ms (so anything that closes faster is unreachable)
"""
import time
import math
import statistics
import requests

BINANCE = "https://api.binance.com/api/v3"
BINANCE_F = "https://fapi.binance.com/fapi/v1"
COINBASE = "https://api.coinbase.com/v2"

ROUND_TRIP_FEE = 0.001   # ~0.1% realistic round-trip taker cost
LATENCY_MS = 300         # measured reaction time from this machine


def _get(url, params=None, timeout=10):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _wilson(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    m = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / d
    return (max(0, c-m), min(1, c+m))


# ---------------------------------------------------------------------------
# 1. Recent-data pattern backtest (the user's specific ask)
# ---------------------------------------------------------------------------
def recent_pattern_backtest(symbol="BTCUSDT", interval="1m", limit=1000):
    """
    Pull the most recent `limit` candles (last few days for 1m) and test simple
    direction patterns. Returns win rates + CIs. An edge counts only if the CI
    lower bound > 0.5 (and after-fee for a tradeable size).
    """
    kl = _get(f"{BINANCE}/klines",
              {"symbol": symbol, "interval": interval, "limit": limit})
    closes = [float(k[4]) for k in kl]
    opens = [float(k[1]) for k in kl]
    ups = [1 if closes[i] > opens[i] else 0 for i in range(len(kl))]

    def test(name, predict):
        wins = n = 0
        for i in range(2, len(ups)):
            p = predict(ups, closes, opens, i)
            if p is None:
                continue
            n += 1
            if p == ups[i]:
                wins += 1
        lo, hi = _wilson(wins, n)
        return {"pattern": name, "n": n,
                "win_rate": round(wins/n, 4) if n else 0,
                "ci": [round(lo, 4), round(hi, 4)],
                "edge_after_fee": round((wins/n if n else 0) - 0.5 - ROUND_TRIP_FEE, 4)}

    results = [
        test("momentum (follow last)", lambda u, c, o, i: u[i-1]),
        test("mean-reversion (fade last)", lambda u, c, o, i: 1 - u[i-1]),
        test("2-streak reversal",
             lambda u, c, o, i: (1 - u[i-1]) if u[i-1] == u[i-2] else None),
        test("2-streak continuation",
             lambda u, c, o, i: u[i-1] if u[i-1] == u[i-2] else None),
    ]
    base_up = round(sum(ups)/len(ups), 4)
    return {"symbol": symbol, "interval": interval, "candles": len(kl),
            "base_up_rate": base_up, "patterns": results}


# ---------------------------------------------------------------------------
# 2. Cross-exchange spread
# ---------------------------------------------------------------------------
def cross_exchange_spread(samples=10, gap_s=1.0):
    """
    Sample Binance vs Coinbase BTC price repeatedly. Report the spread
    distribution and whether it EVER exceeds the round-trip fee (i.e. an
    arb that would actually profit). Also note how fast it would need acting.
    """
    spreads = []
    profitable = 0
    for _ in range(samples):
        try:
            b = float(_get(f"{BINANCE}/ticker/price", {"symbol": "BTCUSDT"})["price"])
            c = float(_get(f"{COINBASE}/prices/BTC-USD/spot")["data"]["amount"])
            spread_pct = abs(b - c) / ((b + c) / 2)
            spreads.append(spread_pct)
            if spread_pct > ROUND_TRIP_FEE * 2:  # need to cross fees on BOTH venues
                profitable += 1
        except Exception:
            pass
        time.sleep(gap_s)
    if not spreads:
        return {"error": "no data"}
    return {
        "samples": len(spreads),
        "median_spread_pct": round(statistics.median(spreads) * 100, 4),
        "max_spread_pct": round(max(spreads) * 100, 4),
        "fee_threshold_pct": round(ROUND_TRIP_FEE * 2 * 100, 4),
        "pct_samples_profitable": round(profitable / len(spreads) * 100, 1),
        "note": "Spread must exceed fee_threshold AND persist > 300ms to be reachable.",
    }


# ---------------------------------------------------------------------------
# 3. Funding-rate scan (cash-and-carry)
# ---------------------------------------------------------------------------
def funding_rate_scan(symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT")):
    """
    Perp funding rates. A consistently high positive funding rate => longs pay
    shorts; you can be short-perp + long-spot (delta-neutral) and collect funding.
    This is a REAL edge but slow (paid every 8h) and needs capital on both legs.
    Reports annualized funding and whether it's meaningfully positive.
    """
    out = []
    for s in symbols:
        try:
            pi = _get(f"{BINANCE_F}/premiumIndex", {"symbol": s})
            last = float(pi.get("lastFundingRate", 0))
            hist = _get(f"{BINANCE_F}/fundingRate", {"symbol": s, "limit": 21})
            rates = [float(h["fundingRate"]) for h in hist]
            avg = statistics.mean(rates) if rates else 0
            # funding paid 3x/day -> annualized
            annual = avg * 3 * 365
            out.append({
                "symbol": s,
                "last_funding_pct": round(last * 100, 4),
                "avg_funding_pct": round(avg * 100, 4),
                "annualized_pct": round(annual * 100, 2),
                "positive_streak": all(r > 0 for r in rates[-7:]) if len(rates) >= 7 else False,
            })
        except Exception as e:
            out.append({"symbol": s, "error": str(e)[:40]})
    return {"funding": out,
            "note": "Cash-and-carry: short perp + long spot collects positive funding, "
                    "delta-neutral. Real but slow; needs 2x capital and is competed down."}


# ---------------------------------------------------------------------------
# 4. Triangular arbitrage within Binance
# ---------------------------------------------------------------------------
def triangular_scan(loops=(("BTCUSDT", "ETHBTC", "ETHUSDT"),
                           ("BTCUSDT", "BNBBTC", "BNBUSDT"))):
    """
    For loop (A=XUSDT, B=YX, C=YUSDT): buy X with USDT, buy Y with X, sell Y for
    USDT. Profit if the product of rates > 1 after fees. Uses live bookTicker
    (best bid/ask) so it's realistic, not midpoint fantasy.
    """
    results = []
    for a, b, c in loops:
        try:
            ta = _get(f"{BINANCE}/ticker/bookTicker", {"symbol": a})
            tb = _get(f"{BINANCE}/ticker/bookTicker", {"symbol": b})
            tc = _get(f"{BINANCE}/ticker/bookTicker", {"symbol": c})
            # USDT -> X: pay ask of A (X/USDT). get 1/askA  X per USDT
            ask_a = float(ta["askPrice"])
            # X -> Y: B is Y/X; pay ask of B. get 1/askB Y per X
            ask_b = float(tb["askPrice"])
            # Y -> USDT: C is Y/USDT; sell at bid of C
            bid_c = float(tc["bidPrice"])
            # start 1 USDT
            x = 1.0 / ask_a
            y = x / ask_b
            usdt_back = y * bid_c
            gross = usdt_back - 1.0
            fee = 3 * 0.001  # 3 trades * 0.1%
            net = gross - fee
            results.append({
                "loop": f"{a}->{b}->{c}",
                "gross_pct": round(gross * 100, 4),
                "net_after_fee_pct": round(net * 100, 4),
                "profitable": net > 0,
            })
        except Exception as e:
            results.append({"loop": f"{a}->{b}->{c}", "error": str(e)[:40]})
    return {"loops": results,
            "note": "Net must be > 0 after 3x 0.1% fees AND fillable before it moves."}


# ---------------------------------------------------------------------------
# 5. Stablecoin depeg
# ---------------------------------------------------------------------------
def stablecoin_depeg():
    """
    Check USDC/USDT/DAI vs $1. A meaningful depeg (> ~0.3%) that reliably
    reverts is a slow, low-risk edge. Report current deviations.
    """
    out = []
    for sym, url, parse in [
        ("USDT", f"{COINBASE}/prices/USDT-USD/spot", lambda j: float(j["data"]["amount"])),
        ("USDC", f"{COINBASE}/prices/USDC-USD/spot", lambda j: float(j["data"]["amount"])),
        ("DAI",  f"{COINBASE}/prices/DAI-USD/spot",  lambda j: float(j["data"]["amount"])),
    ]:
        try:
            price = parse(_get(url))
            dev = (price - 1.0) * 100
            out.append({"coin": sym, "price": round(price, 5),
                        "deviation_pct": round(dev, 4),
                        "tradeable": abs(dev) > 0.3})
        except Exception as e:
            out.append({"coin": sym, "error": str(e)[:40]})
    return {"stablecoins": out,
            "note": "Depeg > 0.3% that reverts is low-risk but rare and small."}
