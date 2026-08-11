"""Build the schema fixture from cached per-demo metadata. Fast; run often."""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "fixtures" / "raw"
FIXTURE = ROOT / "fixtures" / "demoparser_schema.json"

EXPECTED_BUT_UNOBSERVED = [
    "bomb_beginplant", "bomb_abortplant", "bomb_abortdefuse",
    "bomb_dropped", "bomb_pickup",
]


def main() -> int:
    files = sorted(CACHE.glob("*.json"))
    if not files:
        print(f"No cache in {CACHE} — run schema_parse.py first")
        return 1

    demos = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    print(f"{len(demos)} demo(s) from cache\n")

    # --- which events were observed, and in how many demos
    observed = defaultdict(list)
    for d in demos:
        for name in d.get("available_events", []):
            observed[name].append(d["file"])

    union = sorted(observed)
    common = sorted(n for n, f in observed.items() if len(f) == len(demos))
    single = sorted(n for n, f in observed.items() if len(f) == 1)

    print(f"events observed anywhere : {len(union)}")
    print(f"events in every demo     : {len(common)}")
    if single:
        print(f"single-observation (weak evidence): {single}")

    # --- column drift for the parsed subset
    cols = defaultdict(dict)
    for d in demos:
        for name, info in d.get("events", {}).items():
            cols[name][d["file"]] = tuple(info["columns"])

    drift = False
    for name, per_file in sorted(cols.items()):
        variants = set(per_file.values())
        if len(variants) > 1:
            drift = True
            base = set.intersection(*(set(v) for v in variants))
            print(f"\n{name}: {len(variants)} column variants")
            for f, c in per_file.items():
                print(f"  {f}: +{sorted(set(c) - base)}")
    if not drift and cols:
        print(f"\nno column drift across {len(cols)} parsed events")

    # --- tick props
    tick_sets = {d["file"]: set(d.get("tick_columns", [])) for d in demos}
    all_ticks = set.union(*tick_sets.values()) if tick_sets else set()
    print(f"\ntick columns: {sorted(all_ticks)}")
    for prop in ("is_warmup_period", "total_rounds_played", "team_num"):
        where = [f for f, s in tick_sets.items() if prop in s]
        print(f"  {prop}: {len(where)}/{len(demos)} demos")

    payload = {
        "demo_count": len(demos),
        "demos": demos,
        "events_observed": {
            n: {"count": len(f), "demos": sorted(f)}
            for n, f in sorted(observed.items())
        },
        "events_in_all_demos": common,
        "events_single_observation": single,
        "events_expected_but_unobserved": EXPECTED_BUT_UNOBSERVED,
        "tick_columns_union": sorted(all_ticks),
    }
    FIXTURE.write_text(json.dumps(payload, indent=2, default=str),
                       encoding="utf-8")
    print(f"\nwrote {FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())