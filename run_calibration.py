"""
Run the calibration study: where is Polymarket systematically mispriced?

    python run_calibration.py

Honest research tool. It samples real resolved markets, looks at the price
mid-life, and checks whether the actual outcomes beat or lag the price. The
output tells you which categories / price ranges have a real, measurable edge
(and which don't).
"""
import math
from polybot.calibration import run_calibration


def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    m = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / d
    return (max(0, c-m), min(1, c+m))


def summarize(label_rows, title):
    print("=" * 86)
    print(f"  {title}")
    print("=" * 86)
    print(f"  {'bucket/cat':16} {'n':>4} {'avg_price':>9} {'actual_win':>11} "
          f"{'edge':>7} {'95% CI on edge':>18}")
    print("  " + "-" * 80)
    for label, rows in label_rows:
        n = len(rows)
        if n == 0:
            continue
        avg_price = sum(p for p, _ in rows) / n
        wins = sum(w for _, w in rows)
        actual = wins / n
        edge = actual - avg_price          # >0 means YES underpriced here
        lo, hi = wilson_ci(wins, n)
        # CI on edge = CI on actual win rate, shifted by avg implied price
        ci_lo, ci_hi = lo - avg_price, hi - avg_price
        flag = ""
        if n >= 30:
            if ci_lo > 0:
                flag = "  <-- YES underpriced (+EV buy YES)"
            elif ci_hi < 0:
                flag = "  <-- YES overpriced (+EV buy NO)"
        print(f"  {label:16} {n:>4} {avg_price:>9.3f} {actual:>10.1%} "
              f"{edge:>+7.3f} [{ci_lo:>+5.2f},{ci_hi:>+5.2f}]{flag}")
    print()


def main():
    by_cat, by_bucket, checked = run_calibration(pages=8, sample_cap=400)
    print(f"\nSampled {checked} resolved markets with usable mid-life prices.\n")

    # Global price-bucket calibration (favorite-longshot bias check)
    bucket_rows = sorted(by_bucket.items(), key=lambda x: x[0])
    summarize([(f"price~{b:.1f}", rows) for b, rows in bucket_rows],
              "GLOBAL CALIBRATION by price bucket (is a 0.7 market right 70% of the time?)")

    # Per-category
    cat_rows = sorted(by_cat.items(), key=lambda x: -len(x[1]))
    summarize([(c, rows) for c, rows in cat_rows],
              "PER-CATEGORY edge (actual win rate vs price paid, YES side)")

    print("HOW TO READ:")
    print("  'edge' = actual_win% - avg_price. Positive = YES side underpriced")
    print("  (buying YES there made money). Negative = YES overpriced (buy NO).")
    print("  Only trust rows with n>=30 AND a CI that does NOT include 0.")
    print("  Everything else is too small a sample to act on.")
    print("  NOTE: even a real +3% edge needs to beat Polymarket's spread to net profit.")


if __name__ == "__main__":
    main()
