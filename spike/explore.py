r"""Load one demo into DataFrames, then drop to an interactive prompt.
Run with:  python -i spike\explore.py
"""
import sys
from pathlib import Path

EXTRACTED = Path(r"C:\Users\willi\Projects\csstats-data\extracted")
DEFAULT = "003836072496658907557_1315824633"


def find_demo(stem: str) -> Path:
    exact = EXTRACTED / f"{stem}.dem"
    if exact.exists():
        return exact
    matches = [d for d in sorted(EXTRACTED.glob("*.dem")) if stem in d.stem]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"no demo matching {stem!r}. available:")
    else:
        print(f"{stem!r} is ambiguous:")
    for d in sorted(EXTRACTED.glob("*.dem")):
        print(f"  {d.stem}")
    sys.exit(1)


DEMO = find_demo(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)

import pandas as pd
from demoparser2 import DemoParser

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)

PLAYER = ["team_num", "team_name", "health", "armor_value"]
OTHER = ["total_rounds_played", "is_warmup_period", "game_time",
         "round_start_time"]

p = DemoParser(str(DEMO))

kills = p.parse_event("player_death", player=PLAYER, other=OTHER)
hurt = p.parse_event("player_hurt", player=PLAYER, other=OTHER)
rounds = p.parse_event("round_end", other=OTHER)

print(f"{DEMO.name}")
print(f"  kills  {len(kills):6d} rows")
print(f"  hurt   {len(hurt):6d} rows")
print(f"  rounds {len(rounds):6d} rows")
print("\nkills columns:")
for c in sorted(kills.columns):
    print(f"  {c:32s} {kills[c].dtype}")
print("\nnow interactive: try kills.head(), hurt.dtypes, etc.")