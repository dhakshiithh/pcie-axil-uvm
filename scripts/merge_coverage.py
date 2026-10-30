#!/usr/bin/env python3
"""Merge per-test functional coverage (*.cov.json) into one regression report."""
import glob, json, os, sys

d = sys.argv[1] if len(sys.argv) > 1 else "reports"
merged = {}
for f in sorted(glob.glob(os.path.join(d, "*.cov.json"))):
    for cp, bins in json.load(open(f)).items():
        m = merged.setdefault(cp, {})
        for b, v in bins.items():
            m[b] = m.get(b, 0) + v

tot = hit = 0
lines = []
for cp, bins in merged.items():
    h = sum(1 for v in bins.values() if v)
    hit += h
    tot += len(bins)
    missing = [b for b, v in bins.items() if not v]
    lines.append(f"  {cp:<15} {h:>3}/{len(bins):<3} ({100*h/len(bins):5.1f}%)" + (f"  missing: {missing}" if missing else ""))
print(f"Merged functional coverage ({d}): {100*hit/tot:.1f}% ({hit}/{tot} bins)")
print("\n".join(lines))
