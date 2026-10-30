"""Aggregate voice latency and outcomes from the calls saved in outcomes/.

    python -m scripts.latency_summary
"""

import json
import statistics
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "outcomes"


def main():
    files = sorted(OUT.glob("*.json"))
    if not files:
        print("No calls saved yet. Make a call with main.py first.")
        return
    lat, outcomes = [], Counter()
    for f in files:
        d = json.loads(f.read_text())
        outcomes[d["record"]["outcome"]] += 1
        lat += d.get("latency", {}).get("all_secs", [])
    print(f"{len(files)} calls")
    for k, v in outcomes.most_common():
        print(f"  {k}: {v}")
    if lat:
        s = sorted(lat)
        print(f"\nUser stops speaking -> bot starts speaking, over {len(s)} turns:")
        print(f"  median {statistics.median(s):.2f}s   p90 {s[int(0.9 * (len(s) - 1))]:.2f}s   max {s[-1]:.2f}s")


if __name__ == "__main__":
    main()
