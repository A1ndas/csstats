r"""Extract CS2 demo data into SQLite.

    python spike/extract.py                 # every demo it can find
    python spike/extract.py 0144            # one demo, by name fragment
    python spike/extract.py --discover      # print columns, write nothing
    python spike/extract.py --where         # print the paths it will use

Paths are relative to the repository, so this works from any working
directory and on any OS. Demos are large and live outside the repo; the
script looks in several likely places and can be pointed anywhere with the
CSSTATS_DEMOS environment variable.

Stores RAW event rows, not derived statistics. Every metric with a tunable
constant in it (trade window, KAST definition, ADR clamping) stays a query
over these rows so it can be redefined without re-parsing. Valve deletes
replays after 1-2 weeks, so a parse captures everything now or never.

Facts encoded here, all measured rather than assumed:

  * `user_*` means the SUBJECT of the event, i.e. the victim. Stored as
    `victim_*` so the SQL reads sensibly.
  * SteamIDs arrive as str from parse_event but uint64 from parse_ticks.
    Everything is normalised to str and stored as TEXT.
  * total_rounds_played counts COMPLETED rounds, so it is 0 during round 1.
    round_number is 1-based; provenance is recorded on the match row.
  * dmg_health is RAW and can exceed the victim's remaining health (146 of
    596 hits on demo 1315). Both it and victim_health are stored so clamped
    damage is computable in SQL. The choice is deliberately not baked in.
  * team_num 2 = T, 3 = CT. 1 is spectator and is never a side.
  * round_end.winner is a SIDE, not a team.

Not yet done, by request: validation, quarantine, full idempotency. Primary
keys plus INSERT OR REPLACE make re-runs non-duplicating, which is not the
same thing as safe interruption.
"""
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from demoparser2 import DemoParser

# ------------------------------------------------------------------- paths

ROOT = Path(__file__).resolve().parents[1]          # .../csstats
DATA = ROOT / "data"                                # gitignored
DB_PATH = DATA / "csstats.db"

# Candidate demo locations, first match wins. CSSTATS_DEMOS overrides all.
DEMO_CANDIDATES = [
    ROOT.parent / "csstats-data" / "extracted",
    ROOT.parent / "csstats-data" / "demos",
    ROOT.parent / "csstats-data",
    DATA / "demos",
    ROOT / "demos",
]


def demo_dir() -> Path | None:
    override = os.environ.get("CSSTATS_DEMOS")
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    for p in DEMO_CANDIDATES:
        if p.is_dir() and any(p.glob("*.dem")):
            return p
    for p in DEMO_CANDIDATES:
        if p.is_dir():
            return p
    return None


# ------------------------------------------------------------------ config

SIDE = {2: "T", 3: "CT"}
PLAYING = (2, 3)

PLAYER_PROPS = ["team_num", "team_name", "health", "armor_value"]
OTHER_PROPS = ["total_rounds_played", "is_warmup_period", "game_time",
               "round_start_time"]

# Events with a schema verified against the corpus get proper tables.
# Everything else is captured as JSON so it is not lost, and can be promoted
# to a real table later without another parse.
CAPTURE_JSON = [
    "player_blind", "flashbang_detonate", "item_purchase",
    "bomb_planted", "bomb_defused", "bomb_exploded", "bomb_begindefuse",
    "hegrenade_detonate", "smokegrenade_detonate", "molotov_detonate",
    "inferno_startburn", "player_disconnect", "round_start", "round_mvp",
]

# High volume (thousands of rows per match). Off by default.
CAPTURE_WEAPON_FIRE = False


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS matches (
    match_id        TEXT PRIMARY KEY,
    outcome_field   TEXT,          -- 2nd filename field; meaning unresolved
    demo_filename   TEXT NOT NULL,
    demo_sha256     TEXT NOT NULL,
    map_name        TEXT,
    patch_version   TEXT,
    server_name     TEXT,
    rounds          INTEGER NOT NULL,
    round_source    TEXT NOT NULL, -- 'round_prop' | 'total_rounds_played+1'
    parsed_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id       TEXT NOT NULL REFERENCES matches(match_id),
    steamid        TEXT NOT NULL,
    name           TEXT,
    team           TEXT,           -- 'A' | 'B' (roster, not side)
    started_side   TEXT,
    final_side     TEXT,
    mvps           INTEGER,
    rounds_present INTEGER,
    left_early     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, steamid)
);

CREATE TABLE IF NOT EXISTS rounds (
    match_id      TEXT NOT NULL REFERENCES matches(match_id),
    round_number  INTEGER NOT NULL,
    winner_side   TEXT,
    reason        TEXT,
    end_tick      INTEGER,
    PRIMARY KEY (match_id, round_number)
);

-- per-round side for every player: powers all CT/T splits
CREATE TABLE IF NOT EXISTS player_rounds (
    match_id      TEXT NOT NULL REFERENCES matches(match_id),
    round_number  INTEGER NOT NULL,
    steamid       TEXT NOT NULL,
    side          TEXT NOT NULL,
    PRIMARY KEY (match_id, round_number, steamid)
);

CREATE TABLE IF NOT EXISTS kills (
    match_id          TEXT NOT NULL REFERENCES matches(match_id),
    tick              INTEGER NOT NULL,
    victim_steamid    TEXT NOT NULL,
    round_number      INTEGER,
    attacker_steamid  TEXT,
    assister_steamid  TEXT,
    weapon            TEXT,
    hitgroup          TEXT,
    headshot          INTEGER,
    penetrated        INTEGER,
    thrusmoke         INTEGER,
    noscope           INTEGER,
    attackerblind     INTEGER,
    attackerinair     INTEGER,
    assistedflash     INTEGER,
    distance          REAL,
    attacker_side     TEXT,
    victim_side       TEXT,
    assister_side     TEXT,
    weapon_prev_owner TEXT,
    exclusion         TEXT,        -- NULL | world | suicide | teamkill
    game_time         REAL,
    round_start_time  REAL,
    PRIMARY KEY (match_id, tick, victim_steamid)
);

CREATE TABLE IF NOT EXISTS damage (
    match_id          TEXT NOT NULL REFERENCES matches(match_id),
    seq               INTEGER NOT NULL,
    tick              INTEGER NOT NULL,
    attacker_steamid  TEXT,
    victim_steamid    TEXT NOT NULL,
    round_number      INTEGER,
    weapon            TEXT,
    hitgroup          TEXT,
    dmg_health        INTEGER,     -- RAW: may exceed victim_health
    dmg_armor         INTEGER,
    victim_health     INTEGER,
    victim_armor      INTEGER,
    attacker_side     TEXT,
    victim_side       TEXT,
    exclusion         TEXT,
    game_time         REAL,
    PRIMARY KEY (match_id, seq)
);

-- events whose column schema is not yet modelled: kept verbatim so a future
-- promotion to a real table needs no re-parse
CREATE TABLE IF NOT EXISTS raw_events (
    match_id      TEXT NOT NULL REFERENCES matches(match_id),
    event_name    TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    tick          INTEGER,
    round_number  INTEGER,
    payload       TEXT NOT NULL,   -- JSON of the whole row
    PRIMARY KEY (match_id, event_name, seq)
);

CREATE INDEX IF NOT EXISTS ix_kills_attacker  ON kills(attacker_steamid);
CREATE INDEX IF NOT EXISTS ix_kills_victim    ON kills(victim_steamid);
CREATE INDEX IF NOT EXISTS ix_kills_round     ON kills(match_id, round_number);
CREATE INDEX IF NOT EXISTS ix_damage_attacker ON damage(attacker_steamid);
CREATE INDEX IF NOT EXISTS ix_damage_round    ON damage(match_id, round_number);
CREATE INDEX IF NOT EXISTS ix_raw_events      ON raw_events(event_name);
"""


# ------------------------------------------------------------------ helpers

def sid(value) -> str | None:
    """Normalise a SteamID to str, or None. Never let a float near this."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        return str(int(value))
    return str(value)


def flag(value) -> int | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return int(bool(value))


def num(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value.item() if hasattr(value, "item") else value


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_demos(where: Path, fragment: str | None) -> list[Path]:
    demos = sorted(where.glob("*.dem"))
    if fragment is None:
        return demos
    return [d for d in demos if fragment in d.stem]


def parse_filename(stem: str) -> tuple[str, str]:
    """003836072496658907557_1315824633 -> ('3836072496658907557', '1315824633')."""
    left, _, right = stem.partition("_")
    return left.lstrip("0") or "0", right


def side_of(team_num) -> str | None:
    v = num(team_num)
    return SIDE.get(int(v)) if v is not None else None


# --------------------------------------------------------------- extraction

def round_of(row, source: str) -> int | None:
    getter = row.get if hasattr(row, "get") else (
        lambda k, d=None: getattr(row, k, d))
    if source == "round_prop":
        v = num(getter("round", None))
    else:
        v = num(getter("total_rounds_played", None))
        v = None if v is None else v + 1
    return None if v is None else int(v)


def detect_round_source(p: DemoParser) -> str:
    """Prefer a real `round` prop; fall back to the completed-round counter."""
    try:
        df = p.parse_event("player_death", other=["round"])
        if hasattr(df, "columns") and "round" in df.columns:
            return "round_prop"
    except Exception:
        pass
    return "total_rounds_played+1"


def build_sides(p: DemoParser, rounds: pd.DataFrame) -> pd.DataFrame:
    ticks = sorted(int(t) for t in rounds["tick"].unique())
    last = None
    for props in (["team_num", "mvps"], ["team_num"]):
        try:
            df = p.parse_ticks(props, ticks=ticks)
            if "mvps" not in df.columns:
                df["mvps"] = pd.NA
            df = df.copy()
            df["steamid"] = [sid(s) for s in df["steamid"]]
            return df
        except Exception as err:
            last = err
    raise RuntimeError(f"tick read failed: {last!r}")


def _team_a_side(grp: pd.DataFrame, team_of: dict) -> str | None:
    votes = []
    for r in grp.itertuples():
        team, side = team_of.get(r.steamid), side_of(r.team_num)
        if team is None or side is None:
            continue
        votes.append(side if team == "A" else ("T" if side == "CT" else "CT"))
    return max(set(votes), key=votes.count) if votes else None


def resolve_teams(sides: pd.DataFrame) -> dict:
    live = sides[sides["team_num"].isin(PLAYING)]
    if live.empty:
        return {}
    by_tick = {t: g for t, g in live.groupby("tick")}
    anchor = max(by_tick, key=lambda t: by_tick[t]["steamid"].nunique())
    team_of = {r.steamid: ("A" if r.team_num == 2 else "B")
               for r in by_tick[anchor].itertuples()}
    for _ in range(4):
        added = 0
        for grp in by_tick.values():
            a_side = _team_a_side(grp, team_of)
            if a_side is None:
                continue
            for r in grp.itertuples():
                if r.steamid in team_of:
                    continue
                team_of[r.steamid] = ("A" if side_of(r.team_num) == a_side
                                      else "B")
                added += 1
        if not added:
            break
    return team_of


def classify(attacker: str | None, victim: str | None,
             a_side: str | None, v_side: str | None) -> str | None:
    """Why this row should not count toward the attacker's credit."""
    if attacker is None:
        return "world"
    if attacker == victim:
        return "suicide"
    if a_side is not None and v_side is not None and a_side == v_side:
        return "teamkill"
    return None


def extract(path: Path, conn: sqlite3.Connection) -> None:
    match_id, outcome_field = parse_filename(path.stem)
    print(f"\n{path.name}  (match {match_id})")

    p = DemoParser(str(path))
    header = p.parse_header()
    rounds = p.parse_event("round_end", other=OTHER_PROPS)
    if not hasattr(rounds, "columns") or rounds.empty:
        print("  no round_end data -- skipping")
        return

    n_rounds = len(rounds)
    src = detect_round_source(p)
    print(f"  rounds {n_rounds}, round numbers from {src}")

    other = OTHER_PROPS + (["round"] if src == "round_prop" else [])
    kills = p.parse_event("player_death", player=PLAYER_PROPS, other=other)
    hurt = p.parse_event("player_hurt", player=PLAYER_PROPS, other=other)

    sides = build_sides(p, rounds)
    team_of = resolve_teams(sides)

    cur = conn.cursor()

    cur.execute(
        "INSERT OR REPLACE INTO matches (match_id, outcome_field, "
        "demo_filename, demo_sha256, map_name, patch_version, server_name, "
        "rounds, round_source, parsed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (match_id, outcome_field, path.name, sha256(path),
         header.get("map_name"), header.get("patch_version"),
         header.get("server_name"), n_rounds, src,
         datetime.now(timezone.utc).isoformat()))

    # ---- rounds, and team A's side per round (for started_side)
    live = sides[sides["team_num"].isin(PLAYING)]
    by_tick = {t: g for t, g in live.groupby("tick")}
    tick_to_round, a_side_by_round = {}, {}

    for r in rounds.sort_values("tick").itertuples():
        rno = round_of(r, src)
        tick = int(r.tick)
        tick_to_round[tick] = rno
        grp = by_tick.get(tick)
        if grp is not None:
            a_side_by_round[rno] = _team_a_side(grp, team_of)
        cur.execute(
            "INSERT OR REPLACE INTO rounds (match_id, round_number, "
            "winner_side, reason, end_tick) VALUES (?,?,?,?,?)",
            (match_id, rno, getattr(r, "winner", None),
             getattr(r, "reason", None), tick))

    # ---- player_rounds
    pr = 0
    for r in live.itertuples():
        rno = tick_to_round.get(int(r.tick))
        s = side_of(r.team_num)
        if rno is None or s is None:
            continue
        cur.execute(
            "INSERT OR REPLACE INTO player_rounds (match_id, round_number, "
            "steamid, side) VALUES (?,?,?,?)", (match_id, rno, r.steamid, s))
        pr += 1

    # ---- match_players
    ordered = live.sort_values("tick")
    last_seen = ordered.groupby("steamid").last()
    counts = ordered.groupby("steamid").size()
    peak_mvp = (ordered.groupby("steamid")["mvps"].max()
                if ordered["mvps"].notna().any() else None)
    last_tick = int(sides["tick"].max())
    present = {s for s in sides.loc[sides["tick"] == last_tick, "steamid"]}
    first_round = min(a_side_by_round) if a_side_by_round else None

    for steamid, row in last_seen.iterrows():
        team = team_of.get(steamid)
        started = None
        if team and first_round is not None:
            a = a_side_by_round.get(first_round)
            if a:
                started = a if team == "A" else ("CT" if a == "T" else "T")
        cur.execute(
            "INSERT OR REPLACE INTO match_players (match_id, steamid, name, "
            "team, started_side, final_side, mvps, rounds_present, "
            "left_early) VALUES (?,?,?,?,?,?,?,?,?)",
            (match_id, steamid, row.get("name"), team, started,
             side_of(row.get("team_num")),
             None if peak_mvp is None else num(peak_mvp.get(steamid)),
             int(counts.get(steamid, 0)),
             int(steamid not in present)))

    # ---- kills
    k_rows = 0
    if hasattr(kills, "columns"):
        for r in kills.itertuples():
            attacker = sid(getattr(r, "attacker_steamid", None))
            victim = sid(getattr(r, "user_steamid", None))
            a_side = side_of(getattr(r, "attacker_team_num", None))
            v_side = side_of(getattr(r, "user_team_num", None))
            cur.execute(
                "INSERT OR REPLACE INTO kills VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (match_id, int(r.tick), victim, round_of(r, src),
                 attacker, sid(getattr(r, "assister_steamid", None)),
                 getattr(r, "weapon", None), getattr(r, "hitgroup", None),
                 flag(getattr(r, "headshot", None)),
                 num(getattr(r, "penetrated", None)),
                 flag(getattr(r, "thrusmoke", None)),
                 flag(getattr(r, "noscope", None)),
                 flag(getattr(r, "attackerblind", None)),
                 flag(getattr(r, "attackerinair", None)),
                 flag(getattr(r, "assistedflash", None)),
                 num(getattr(r, "distance", None)),
                 a_side, v_side,
                 side_of(getattr(r, "assister_team_num", None)),
                 sid(getattr(r, "weapon_originalowner_xuid", None)),
                 classify(attacker, victim, a_side, v_side),
                 num(getattr(r, "game_time", None)),
                 num(getattr(r, "round_start_time", None))))
            k_rows += 1

    # ---- damage
    d_rows = 0
    if hasattr(hurt, "columns"):
        for seq, r in enumerate(hurt.itertuples()):
            attacker = sid(getattr(r, "attacker_steamid", None))
            victim = sid(getattr(r, "user_steamid", None))
            a_side = side_of(getattr(r, "attacker_team_num", None))
            v_side = side_of(getattr(r, "user_team_num", None))
            cur.execute(
                "INSERT OR REPLACE INTO damage VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (match_id, seq, int(r.tick), attacker, victim,
                 round_of(r, src),
                 getattr(r, "weapon", None), getattr(r, "hitgroup", None),
                 num(getattr(r, "dmg_health", None)),
                 num(getattr(r, "dmg_armor", None)),
                 num(getattr(r, "user_health", None)),
                 num(getattr(r, "user_armor_value", None)),
                 a_side, v_side,
                 classify(attacker, victim, a_side, v_side),
                 num(getattr(r, "game_time", None))))
            d_rows += 1

    # ---- raw_events: everything not yet modelled, kept verbatim
    captured = {}
    names = list(CAPTURE_JSON)
    if CAPTURE_WEAPON_FIRE:
        names.append("weapon_fire")

    for name in names:
        try:
            df = p.parse_event(name, player=PLAYER_PROPS, other=other)
        except Exception:
            continue
        if not hasattr(df, "columns") or df.empty:
            continue
        for seq, (_, row) in enumerate(df.iterrows()):
            payload = {k: num(v) for k, v in row.items()}
            tick = payload.get("tick")
            cur.execute(
                "INSERT OR REPLACE INTO raw_events (match_id, event_name, "
                "seq, tick, round_number, payload) VALUES (?,?,?,?,?,?)",
                (match_id, name, seq,
                 None if tick is None else int(tick),
                 round_of(row, src),
                 json.dumps(payload, default=str)))
        captured[name] = len(df)

    conn.commit()

    print(f"  players {len(last_seen)}  player_rounds {pr}")
    print(f"  kills {k_rows}  damage {d_rows}")
    if captured:
        print(f"  raw_events {captured}")
    skipped = [n for n in names if n not in captured]
    if skipped:
        print(f"  no rows for {skipped}")


def discover(paths: list[Path]) -> None:
    """Print columns for events not yet modelled, so they can be promoted."""
    for path in paths:
        print(f"\n{path.name}")
        p = DemoParser(str(path))
        for name in CAPTURE_JSON + ["weapon_fire"]:
            try:
                df = p.parse_event(name, player=PLAYER_PROPS,
                                   other=OTHER_PROPS)
            except Exception as err:
                print(f"  {name}: error {err!r}")
                continue
            if not hasattr(df, "columns"):
                print(f"  {name}: no occurrences")
                continue
            print(f"  {name}: {len(df)} rows")
            print(f"    {sorted(df.columns)}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    where = demo_dir()

    print(f"repo:   {ROOT}")
    print(f"db:     {DB_PATH}")
    print(f"demos:  {where if where else 'NOT FOUND'}")

    if "--where" in flags:
        print("\ncandidates checked:")
        for p in DEMO_CANDIDATES:
            print(f"  {'yes' if p.is_dir() else ' no'}  {p}")
        print("\nset CSSTATS_DEMOS to override")
        return 0

    if where is None:
        print("\nNo demo directory found. Either move your .dem files into")
        print(f"  {DATA / 'demos'}")
        print("or set CSSTATS_DEMOS to the folder containing them.")
        print("Run with --where to see everything that was checked.")
        return 1

    demos = find_demos(where, args[0] if args else None)
    if not demos:
        available = sorted(d.stem for d in where.glob("*.dem"))
        print(f"\nno .dem matching {args[0]!r}" if args
              else f"\nno .dem files in {where}")
        if available:
            print("available:")
            for name in available:
                print(f"  {name}")
        return 1

    print(f"        {len(demos)} demo(s) to process")

    if "--discover" in flags:
        discover(demos)
        return 0

    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    failures = 0
    for path in demos:
        try:
            extract(path, conn)
        except Exception as err:
            conn.rollback()
            failures += 1
            print(f"  FAILED {path.name}: {err!r}")

    print()
    for table in ("matches", "match_players", "rounds", "player_rounds",
                  "kills", "damage", "raw_events"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:16s} {n:7d}")

    conn.close()
    print(f"\nwrote {DB_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())