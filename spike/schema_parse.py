"""Parse demos and cache per-demo schema metadata. Slow; run rarely."""
import hashlib
import json
import sys
import traceback
from importlib.metadata import version
from pathlib import Path

from demoparser2 import DemoParser

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "fixtures" / "raw"
EXTRACTED = Path(r"C:\Users\willi\Projects\csstats-data\extracted")

PARSE_ALL_EVENTS = False   # True = all observed events (slow, one-off)

WANTED_EVENTS = [
    "player_death", "player_hurt", "weapon_fire", "player_blind",
    "bomb_planted", "bomb_defused", "bomb_exploded", "bomb_begindefuse",
    "item_purchase", "round_start", "round_end", "flashbang_detonate",
]

PLAYER_PROPS = ["X", "Y", "Z", "team_num", "team_name",
                "health", "armor_value"]

OTHER_PROPS = ["total_rounds_played", "is_warmup_period",
               "game_time", "round_start_time"]

TICK_PROPS = ["X", "Y", "Z", "health", "armor_value", "team_num",
              "is_warmup_period", "total_rounds_played"]


def cache_key() -> dict:
    """Anything that changes the shape of the output invalidates the cache."""
    return {
        "demoparser2_version": version("demoparser2"),
        "parse_all_events": PARSE_ALL_EVENTS,
        "player_props": PLAYER_PROPS,
        "other_props": OTHER_PROPS,
        "tick_props": TICK_PROPS,
        "wanted_events": WANTED_EVENTS,
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def describe(path: Path) -> dict:
    parser = DemoParser(str(path))
    out = {
        "file": path.name,
        "sha256": sha256(path),
        "size_bytes": path.stat().st_size,
        "cache_key": cache_key(),
        "demoparser2_version": version("demoparser2"),
        "events": {},
        "errors": {},
    }

    try:
        out["header"] = parser.parse_header()
    except Exception as e:
        out["errors"]["header"] = repr(e)

    try:
        out["available_events"] = sorted(parser.list_game_events())
    except Exception as e:
        out["errors"]["list_game_events"] = repr(e)
        out["available_events"] = []

    targets = out["available_events"] if PARSE_ALL_EVENTS else WANTED_EVENTS
    for name in targets:
        entry = {}
        try:
            df = parser.parse_event(name, player=PLAYER_PROPS,
                                    other=OTHER_PROPS)
            entry["props"] = "requested"
        except Exception as e:
            # some props may not apply to this event — retry bare
            entry["props_error"] = repr(e)
            try:
                df = parser.parse_event(name)
                entry["props"] = "default_only"
            except Exception as e2:
                out["errors"][name] = repr(e2)
                continue

        if not hasattr(df, "columns"):
            # empty list => event never fired in this demo. Not an error.
            entry["empty"] = True
            entry["returned_type"] = type(df).__name__
            entry["rows"] = 0
            entry["columns"] = []
        else:
            entry["columns"] = sorted(df.columns.tolist())
            entry["rows"] = len(df)

        out["events"][name] = entry

    try:
        ticks = parser.parse_ticks(TICK_PROPS)
        out["tick_columns"] = sorted(ticks.columns.tolist())
        out["tick_rows"] = len(ticks)
    except Exception as e:
        out["errors"]["parse_ticks"] = repr(e)

    return out


def main() -> int:
    demos = sorted(EXTRACTED.glob("*.dem"))
    if not demos:
        print(f"No .dem files in {EXTRACTED}")
        return 1

    CACHE.mkdir(parents=True, exist_ok=True)
    key = cache_key()

    for path in demos:
        dest = CACHE / f"{path.stem}.json"
        if dest.exists():
            try:
                cached = json.loads(dest.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cached = {}
            if cached.get("cache_key") == key:
                n = len(cached.get("events", {}))
                print(f"cached  {path.name}  ({n} events)")
                continue
            print(f"stale   {path.name}  (config or version changed)")

        print(f"parsing {path.name} ...")
        try:
            data = describe(path)
        except Exception:
            traceback.print_exc()
            continue
        dest.write_text(json.dumps(data, indent=2, default=str),
                        encoding="utf-8")

        errs = data.get("errors", {})
        props_errs = [n for n, e in data.get("events", {}).items()
                      if "props_error" in e]
        empties = [n for n, e in data.get("events", {}).items()
                   if e.get("empty")]
        print(f"  wrote {dest.relative_to(ROOT)}")
        if props_errs:
            print(f"  props rejected on: {props_errs}")
        if empties:
            print(f"  no occurrences of: {empties}")
        if errs:
            print(f"  errors: {sorted(errs)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())