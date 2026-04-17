#!/usr/bin/env python3
"""Probe all custom scrapers for each username (with spelling variants)
and print a table of which sites return hits."""
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_scrapers import load_scrapers, username_variants

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test")

# Default test usernames. Override via CLI args, e.g.:
#   python test_scrapers.py alice_example bob_example
USERS = sys.argv[1:] or ["example_user_1", "example_user_2"]

scrapers = load_scrapers(log)
print(f"Loaded {len(scrapers)} scrapers: {[s.NAME for s in scrapers]}")

def probe_variant(scraper, user, variant):
    try:
        hit = scraper.probe(variant)
        return (user, scraper.NAME, variant, hit)
    except Exception as e:
        return (user, scraper.NAME, variant, f"ERR: {type(e).__name__}: {e}")

# For each user, try all variants against all scrapers.
# Stop trying variants for a (user, scraper) pair once one succeeds.
results = {u: {} for u in USERS}
with ThreadPoolExecutor(max_workers=16) as pool:
    futs = []
    for u in USERS:
        for variant in username_variants(u):
            for s in scrapers:
                futs.append(pool.submit(probe_variant, s, u, variant))
    for f in as_completed(futs, timeout=300):
        try:
            user, name, variant, hit = f.result()
        except Exception as e:
            log.warning(f"probe future failed: {e}")
            continue
        if isinstance(hit, str):
            # error string; only store if nothing better found
            if name not in results[user]:
                results[user][name] = (variant, hit)
        elif hit is not None:
            prev = results[user].get(name)
            if prev is None or (isinstance(prev[1], str)) or hit.entry_count > prev[1].entry_count:
                results[user][name] = (variant, hit)

print()
print("=" * 80)
for u in USERS:
    print(f"\n{u}:")
    if not results[u]:
        print("  (no hits on any site)")
    else:
        for site, (variant, info) in sorted(results[u].items(), key=lambda kv: -(getattr(kv[1][1], 'entry_count', 0) or 0)):
            if isinstance(info, str):
                print(f"  {site}: {info} (variant {variant!r})")
            else:
                tag = f" [variant: {variant!r}]" if variant != u else ""
                print(f"  {site}: {info.entry_count} videos @ {info.url}{tag}")
print()
