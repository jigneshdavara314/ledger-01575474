"""
User-adjustable runtime settings, stored in settings.json at the repo root.

The ONE knob that matters day-to-day is the daily investment budget. Storing it
in a committed JSON file (not just an env var) means:
  - you can change it from the dashboard / a single edit,
  - the cloud bot (GitHub Actions) reads the same value on its next run,
  - it persists across runs and machines.

Env var DAILY_BUDGET still overrides the file if set (useful for one-off runs).
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "settings.json")

DEFAULTS = {
    "daily_budget_usd": 500.0,   # how much to deploy per day from the bankroll
}


def _read() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {**DEFAULTS, **data}
    except Exception:
        pass
    return dict(DEFAULTS)


def get(key: str):
    return _read().get(key, DEFAULTS.get(key))


def daily_budget() -> float:
    """Daily investment budget. Env var wins (one-off override), else file, else default."""
    env = os.getenv("DAILY_BUDGET")
    if env:
        try:
            # Clamp the env override to a sane range so a fat-fingered value
            # (e.g. "500000") can't blow past the relative risk caps.
            return max(1.0, min(float(env), 100_000.0))
        except ValueError:
            pass
    try:
        return float(get("daily_budget_usd"))
    except (TypeError, ValueError):
        return DEFAULTS["daily_budget_usd"]


def set_daily_budget(value: float) -> float:
    """Persist a new daily budget (clamped to a sane range). Returns the value set."""
    value = max(1.0, min(float(value), 1_000_000.0))
    data = _read()
    data["daily_budget_usd"] = round(value, 2)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data["daily_budget_usd"]
