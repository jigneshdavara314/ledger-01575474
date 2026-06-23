# Live-Trading Trial — Safe Checklist

The bot is **paper-only by default**. The cloud (GitHub Actions) is hardcoded to
`MODE: PAPER` with no wallet, so **the cloud can never move real money**. A live
trial runs **locally on your PC**, deliberately, with tiny hard caps.

The goal of the trial is NOT profit — it is to **prove paper ≈ live**: do real
orders fill near the prices the paper model assumed? If yes, the paper track
record becomes trustworthy. Risk a few dollars to validate the whole system.

---

## Before you start — preconditions

- [ ] Paper results are genuinely positive over a meaningful sample. Check:
      `python run.py report` (want positive ROI) and `python run.py confidence`
      (at least one edge near/at `READY->UP`). Do NOT go live on a thin/negative sample.
- [ ] A **dedicated** Polygon wallet you control, funded with a SMALL amount of
      **USDC on Polygon** (e.g. $20–50) — never your main wallet. You also need a
      little **POL (MATIC)** for gas.
- [ ] One-time on Polymarket: complete the **USDC allowance/approval** for the
      exchange (done via the Polymarket UI when you first trade, or the CLOB
      client's allowance call). Without it, orders are rejected — the bot fails
      CLOSED (it reads `get_balance_allowance` and won't submit if it can't
      confirm funds), so a missing approval just means "no bets", not a loss.

---

## Configure (local `.env` only — never commit this file)

Create / edit `.env` in the repo root:

```
MODE=LIVE
POLYGON_WALLET_PRIVATE_KEY=0x<your_dedicated_trial_wallet_private_key>

# HARD safety caps (these are the rails — keep them tiny for the trial):
LIVE_MAX_BET_USD=5          # absolute max per single real order
LIVE_MAX_DAILY_USD=25       # max total real USDC staked per UTC day
LIVE_KILL_SWITCH=           # leave empty = armed; set to 1 to BLOCK all real orders
```

Notes:
- These caps are enforced in `executor._execute_live` **before** any order is
  built or the client is touched, and they **fail closed** (a breach returns a
  `blocked_*` result, $0 filled, no order). Defaults are already $5 / $25 even if
  you forget to set them.
- `.env` is git-ignored. NEVER paste a private key into chat, a commit, or the
  public repo. The trial wallet should hold only what you're willing to lose.

---

## Run the trial (manually, watching it)

1. [ ] Sanity-check config is what you expect (no real order placed by this):
       `python -c "from polybot import config as c; print(c.MODE, c.LIVE_MAX_BET_USD, c.LIVE_MAX_DAILY_USD, c.LIVE_KILL_SWITCH, bool(c.POLYGON_WALLET_PRIVATE_KEY))"`
       → expect: `LIVE 5.0 25.0 False True`
2. [ ] Place a few real bets, watching each line:
       `python run.py longshot`   (or `python run.py strategies`)
       Each placed bet prints its mode/status/price and order id. A `blocked_*`
       status means a rail stopped it (working as intended).
3. [ ] After games settle: `python run.py resolve` then `python run.py report`.
4. [ ] **The key comparison:** for each filled bet, did the real fill price match
       what paper would have assumed (the ask)? Big gaps = the edge erodes live.

---

## Emergency stop

- Set `LIVE_KILL_SWITCH=1` in `.env` (or `export LIVE_KILL_SWITCH=1`) → every real
  order is blocked instantly on the next run, regardless of MODE.
- Or just set `MODE=PAPER` and re-run — back to simulation immediately.
- The drawdown halt (`DRAWDOWN_HALT_FRAC=0.70`) also stops new bets if equity
  falls below 70% of the deposit.

---

## When to stop the trial / what success looks like

- **Success:** ~15–30 real fills whose prices track the paper model within a cent
  or two, and net result roughly in line with the paper expectation. → The paper
  record is now trustworthy; you can consider scaling caps slowly.
- **Stop & investigate** if: fills come in much worse than the ask (slippage the
  model misses), orders rarely fill (liquidity thinner than assumed), or live ROI
  diverges sharply from paper. Drop back to `MODE=PAPER` and fix the model first.

---

## Reverting to paper

Set `MODE=PAPER` in `.env` (and unset the wallet key if you like). The cloud was
never live, so nothing there changes. Real positions already placed still settle
via `python run.py resolve`.
