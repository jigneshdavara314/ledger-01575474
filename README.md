# Polymarket Paper-Trading Bot

A **paper-trading** (simulated, no real money) bot for Polymarket prediction
markets. It finds short-term markets, places simulated bets using several
strategies, resolves them against real outcomes, and tracks win rate + P&L.

> **PAPER MODE ONLY.** No wallet key is configured and no real money can move.
> Going live would require a funded Polygon wallet and an explicit code change.

## Strategies

- **`longshot`** — fades systematically overpriced longshots (exact-score /
  spread markets) by buying NO. Based on a calibration study of ~770 resolved
  markets showing favorite-longshot bias. Spreads many small bets.
- **`lagwatch`** — resolution-lag arbitrage: buys a known winner (from ESPN
  finished-game data) while the Polymarket market still prices it below 1.00.
- **`short`** — AI/manual fair-value estimates vs market price.

## Commands

```bash
python run.py scout      # preview short-term markets by category
python run.py longshot   # place longshot-fade NO bets (paper)
python run.py lagwatch   # resolution-lag arbitrage (paper)
python run.py resolve    # settle open positions against real outcomes
python run.py report     # win rate + P&L by category
python dashboard.py      # regenerate dashboard.html
```

## Running free in the cloud (GitHub Actions)

The workflow in `.github/workflows/bot.yml` runs the bot every 15 minutes,
commits the updated `trades.db` back to the repo (so history persists), and
publishes the dashboard to GitHub Pages.

**One-time setup after pushing to GitHub:**
1. Repo **Settings → Actions → General → Workflow permissions** →
   select **Read and write permissions** (lets the bot commit the DB back).
2. Repo **Settings → Pages → Build and deployment → Source** → **GitHub Actions**
   (publishes the dashboard at `https://<you>.github.io/<repo>/`).
3. The bot runs automatically. Trigger a first run from the **Actions** tab
   (**Run workflow**) if you don't want to wait for the schedule.

No secrets are required — it runs in PAPER mode with no API keys.

## Research tools

```bash
python run_backtest.py       # backtest crypto up/down patterns (proves no edge)
python run_calibration.py    # find where Polymarket is systematically mispriced
```
