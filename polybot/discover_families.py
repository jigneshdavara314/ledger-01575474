"""
Auto-discover NEW market families — so the hunt's breadth grows on its own,
without a human hand-adding families. This is a SUGGESTION engine, never a betting
engine: it only proposes candidate patterns; the bulletproof edge-scan gate still
has to validate them before a single bet is placed.

Flow:
  1. Pull resolved markets, take the ones that fall in the 'other' grab-bag
     (un-classified -> currently invisible to the hunt).
  2. Group them into recurring structural patterns (>= MIN_OCCURRENCES).
  3. HARD-EXCLUDE anything that hits the crypto guard or novelty patterns.
  4. Optionally refine grouping with Claude (if ANTHROPIC_API_KEY is set) — AI is
     used ONLY to name/group, never to estimate a probability or decide a bet.
  5. Write candidates to discovered_families.json (a queue of suggestions).

The edge-scan reads that queue and tests each candidate pattern through the SAME
Bonferroni + Wilson-LB + OOS gate as every other family. So auto-discovery widens
WHAT GETS TESTED, never WHAT GETS BET without proof.
"""
import json
import os
import re
from collections import Counter

from .calibration import fetch_resolved_markets
from .taxonomy import family_of, CRYPTO_HINTS

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_PATH = os.path.join(BASE, "discovered_families.json")

MIN_OCCURRENCES = 8          # a pattern must recur this many times to be a candidate
_NOVELTY = ("will ", "say ", "said ", "tweet")   # likely-noise markers


def _pattern_key(q: str) -> str:
    """Crude structural key: text before the first colon, else first 3 words,
    with digits masked so '... O/U 2.5' and '... O/U 1.5' group together."""
    ql = q.lower().strip()
    key = ql.split(":")[0].strip() if ":" in ql else " ".join(ql.split()[:3])
    return re.sub(r"[0-9]", "#", key)


def _is_excluded(q: str) -> bool:
    ql = q.lower()
    if any(h in ql for h in CRYPTO_HINTS):
        return True                      # crypto coin-flips: never
    if ql.startswith("will ") and (" say" in ql or " tweet" in ql):
        return True                      # novelty: likely noise
    return False


def discover(pages: int = 20) -> list:
    """Return candidate family patterns found in the 'other' bucket."""
    ms = fetch_resolved_markets(pages=pages)
    others = [m["question"] for m in ms
              if family_of(m["question"]) == "other" and not _is_excluded(m["question"])]
    groups = Counter(_pattern_key(q) for q in others)
    candidates = []
    for key, n in groups.most_common():
        if n < MIN_OCCURRENCES or len(key) < 3:
            continue
        kw = key.replace("#", "").strip()
        if len(kw) < 4:
            continue
        # Skip groups that look like a PERSON NAME (two capitalized-ish words, no
        # structural keyword) — those are one player across many prop types, not a
        # real bet-TYPE family. Keyword grouping can't split them; AI refine can.
        words = kw.split()
        if (len(words) == 2 and all(w.isalpha() for w in words)
                and not any(t in kw for t in ("the", "team", "total", "winner",
                                              "match", "game", "over", "under"))):
            continue
        candidates.append({"pattern": key, "keyword": kw, "occurrences": n,
                           "family": "auto_" + re.sub(r"[^a-z]+", "_", kw)[:24].strip("_")})
    return candidates


def _ai_refine(candidates: list) -> list:
    """If an Anthropic key is present, ask Claude to name/merge the candidate
    groups more sensibly. AI ONLY names/groups — it never scores or bets. Falls
    back to the keyword grouping if no key or any error."""
    from . import config
    if not getattr(config, "ANTHROPIC_API_KEY", ""):
        return candidates
    try:
        import anthropic
        # Point at the omeecron/cloudeapi gateway if configured (Anthropic-
        # compatible drop-in); otherwise the real Anthropic API.
        _kwargs = {"api_key": config.ANTHROPIC_API_KEY}
        if getattr(config, "ANTHROPIC_BASE_URL", ""):
            _kwargs["base_url"] = config.ANTHROPIC_BASE_URL
            # The omeecron gateway authenticates via 'Authorization: Bearer <key>',
            # but the Anthropic SDK sends 'x-api-key'. Send BOTH so the gateway
            # gets the Bearer header it expects (this was the 401 'Missing API key').
            _kwargs["default_headers"] = {
                "Authorization": f"Bearer {config.ANTHROPIC_API_KEY}"}
        client = anthropic.Anthropic(**_kwargs)
        listing = "\n".join(f"- {c['keyword']} (x{c['occurrences']})" for c in candidates)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role": "user", "content":
                "These are recurring Polymarket question patterns not yet classified. "
                "Group/name them into clean betting families (e.g. weather_temp, "
                "shots_on_target). Reply ONLY JSON list of {family, keyword}. Do NOT "
                "estimate probabilities or say anything about betting.\n\n" + listing}])
        text = msg.content[0].text
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            refined = json.loads(text[start:end + 1])
            # keep occurrences by matching keywords back
            occ = {c["keyword"]: c["occurrences"] for c in candidates}
            for r in refined:
                r["occurrences"] = occ.get(r.get("keyword", ""), MIN_OCCURRENCES)
            return refined
    except Exception as e:
        print(f"[discover] AI refine skipped: {e}")
    return candidates


def run(pages: int = 20):
    cands = discover(pages=pages)
    cands = _ai_refine(cands)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump({"candidates": cands}, f, indent=2)
    print(f"[discover] {len(cands)} candidate families queued "
          f"(AI={'on' if os.getenv('ANTHROPIC_API_KEY') else 'off'}):")
    for c in cands[:12]:
        print(f"  {c['occurrences']:>3}x  {c.get('family','?'):24} kw='{c.get('keyword','')}'")
    print("These are SUGGESTIONS — each must still pass the full edge-scan gate to bet.")
    return cands


if __name__ == "__main__":
    run()
