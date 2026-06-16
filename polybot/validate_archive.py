"""
Validate our longshot-fade edge against the FULL Polymarket history (markets.parquet,
268k resolved markets) — ground truth outcomes, not our thin live sample.

markets.parquet has: question, outcome_prices (final resolution), volume, closed.
We can't get the *mid-life entry price* from this file alone, but we CAN measure
the resolved OUTCOME distribution per family — i.e. for "exact score" markets,
how often did the longshot YES actually win? If YES-win is rare, fading (buy NO)
is the edge, confirmed at full scale.
"""
import sys, json
import duckdb

# run with PYTHONPATH=. from repo root
from polybot.taxonomy import family_of

URL = "https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main/markets.parquet"

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")

# Pull resolved binary markets: question, outcome_prices, volume.
# outcome_prices is a JSON-ish string like '["1","0"]' (YES won) or '["0","1"]'.
print("Querying full markets history (this reads ~68MB)...")
rows = con.execute(f"""
    SELECT question, outcome_prices, volume
    FROM read_parquet('{URL}')
    WHERE closed = 1 AND outcome_prices IS NOT NULL
""").fetchall()
print(f"resolved markets: {len(rows)}\n")

# classify + measure YES-win rate per family
from collections import defaultdict
fam = defaultdict(lambda: [0, 0, 0.0])   # family -> [yes_wins, total, volume]
for q, op, vol in rows:
    if not q or not op:
        continue
    # format is "['1', '0']" (YES won) or "['0', '1']" (NO won)
    norm = op.replace(" ", "").replace('"', "'")
    if norm.startswith("['1','0']"):
        yes_won = True
    elif norm.startswith("['0','1']"):
        yes_won = False
    else:
        continue
    f = family_of(q)
    fam[f][0] += 1 if yes_won else 0
    fam[f][1] += 1
    fam[f][2] += (vol or 0)

print(f"{'family':22} {'n':>7} {'YES-won%':>9} {'NO-won%':>8}  (NO-won high = fade YES works)")
print("-" * 70)
out = []
for f, (yw, n, vol) in sorted(fam.items(), key=lambda kv: -kv[1][1]):
    if n < 50:
        continue
    yes_rate = yw / n * 100
    no_rate = 100 - yes_rate
    out.append({"family": f, "n": n, "yes_won_pct": round(yes_rate, 1),
                "no_won_pct": round(no_rate, 1)})
    print(f"{f:22} {n:>7} {yes_rate:>8.1f}% {no_rate:>7.1f}%")
json.dump(out, open(r"c:/tmp/archive_validation.json", "w"), indent=2)
print("\nNOTE: this is the FINAL-outcome distribution across all history. A family")
print("where NO wins far more than YES = the favorite-longshot fade is real at scale.")
print("(Full edge math needs mid-life entry price from trades.parquet — next step.)")
