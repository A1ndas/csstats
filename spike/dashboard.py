r"""Browse extracted CS2 matches from SQLite.

    python spike/dashboard.py            # pick a match from a list
    python spike/dashboard.py 1          # jump straight to list entry 1
    python spike/dashboard.py --sql      # print the metric SQL and exit

Reads only. Run spike/extract.py first to populate the database.

Every metric here is computed from raw event rows at query time, never read
from a stored aggregate. That is deliberate: the trade window, the KAST
definition and the ADR clamping are all tunable, so they live in SQL where
changing them costs a re-run rather than a re-parse.

ADR is reported BOTH ways -- raw dmg_health, and clamped to the victim's
remaining health -- because which one matches the in-game scoreboard is still
an open question. 146 of 596 hits on demo 1315 exceeded victim health, so the
two differ by roughly a quarter. Verify against a real scoreboard and then
collapse this to one column.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "csstats.db"


# ------------------------------------------------------------------- queries

MATCH_LIST = """
SELECT m.match_id, m.map_name, m.rounds, m.demo_filename
FROM matches m
ORDER BY CAST(m.match_id AS INTEGER) DESC
"""

# Round wins per roster. round_end.winner is a SIDE, so it must be joined
# against each player's side in that round to attribute a win to a team.
TEAM_SCORE = """
WITH round_sides AS (
    SELECT r.round_number, r.winner_side, mp.team
    FROM rounds r
    JOIN player_rounds pr
      ON pr.match_id = r.match_id AND pr.round_number = r.round_number
     AND pr.side = r.winner_side
    JOIN match_players mp
      ON mp.match_id = pr.match_id AND mp.steamid = pr.steamid
    WHERE r.match_id = :mid
)
SELECT team, COUNT(DISTINCT round_number) AS wins
FROM round_sides
GROUP BY team
"""

SCOREBOARD = """
WITH me AS (
    SELECT steamid, name, team, started_side, mvps, rounds_present, left_early
    FROM match_players WHERE match_id = :mid
),
k AS (
    SELECT attacker_steamid AS steamid,
           COUNT(*) AS kills,
           SUM(headshot) AS hs,
           SUM(CASE WHEN penetrated > 0 THEN 1 ELSE 0 END) AS wallbang,
           SUM(thrusmoke) AS smoke_kills,
           SUM(noscope) AS noscope,
           SUM(attackerblind) AS blind_kills
    FROM kills
    WHERE match_id = :mid AND exclusion IS NULL
    GROUP BY attacker_steamid
),
d AS (
    SELECT victim_steamid AS steamid, COUNT(*) AS deaths
    FROM kills WHERE match_id = :mid
    GROUP BY victim_steamid
),
a AS (
    SELECT assister_steamid AS steamid,
           COUNT(*) AS assists,
           SUM(assistedflash) AS flash_assists
    FROM kills
    WHERE match_id = :mid AND assister_steamid IS NOT NULL
      AND assister_side <> victim_side
    GROUP BY assister_steamid
),
dmg AS (
    SELECT attacker_steamid AS steamid,
           SUM(dmg_health) AS raw_dmg,
           SUM(MIN(dmg_health, victim_health)) AS clamped_dmg,
           SUM(dmg_armor) AS armor_dmg,
           COUNT(*) AS hits
    FROM damage
    WHERE match_id = :mid AND exclusion IS NULL
    GROUP BY attacker_steamid
),
taken AS (
    SELECT victim_steamid AS steamid,
           SUM(MIN(dmg_health, victim_health)) AS dmg_taken
    FROM damage WHERE match_id = :mid AND exclusion IS NULL
    GROUP BY victim_steamid
),
util AS (
    SELECT attacker_steamid AS steamid,
           SUM(MIN(dmg_health, victim_health)) AS util_dmg
    FROM damage
    WHERE match_id = :mid AND exclusion IS NULL
      AND weapon IN ('hegrenade', 'molotov', 'incgrenade', 'inferno',
                     'flashbang', 'decoy')
    GROUP BY attacker_steamid
)
SELECT me.steamid, me.name, me.team, me.started_side, me.mvps,
       me.rounds_present, me.left_early,
       COALESCE(k.kills, 0)         AS kills,
       COALESCE(d.deaths, 0)        AS deaths,
       COALESCE(a.assists, 0)       AS assists,
       COALESCE(a.flash_assists, 0) AS flash_assists,
       COALESCE(k.hs, 0)            AS hs,
       COALESCE(k.wallbang, 0)      AS wallbang,
       COALESCE(k.smoke_kills, 0)   AS smoke_kills,
       COALESCE(k.blind_kills, 0)   AS blind_kills,
       COALESCE(dmg.raw_dmg, 0)     AS raw_dmg,
       COALESCE(dmg.clamped_dmg, 0) AS clamped_dmg,
       COALESCE(dmg.hits, 0)        AS hits,
       COALESCE(taken.dmg_taken, 0) AS dmg_taken,
       COALESCE(util.util_dmg, 0)   AS util_dmg
FROM me
LEFT JOIN k     ON k.steamid     = me.steamid
LEFT JOIN d     ON d.steamid     = me.steamid
LEFT JOIN a     ON a.steamid     = me.steamid
LEFT JOIN dmg   ON dmg.steamid   = me.steamid
LEFT JOIN taken ON taken.steamid = me.steamid
LEFT JOIN util  ON util.steamid  = me.steamid
"""

# First kill and first death of each round -> opening duels.
OPENING = """
WITH firsts AS (
    SELECT round_number, MIN(tick) AS tick
    FROM kills WHERE match_id = :mid AND round_number IS NOT NULL
    GROUP BY round_number
)
SELECT k.attacker_steamid, k.victim_steamid, k.round_number,
       r.winner_side, k.attacker_side
FROM kills k
JOIN firsts f ON f.round_number = k.round_number AND f.tick = k.tick
LEFT JOIN rounds r ON r.match_id = k.match_id
                  AND r.round_number = k.round_number
WHERE k.match_id = :mid AND k.exclusion IS NULL
"""

# Per-round kill counts, for multikills and a sparkline.
PER_ROUND = """
SELECT attacker_steamid, round_number, COUNT(*) AS kills
FROM kills
WHERE match_id = :mid AND exclusion IS NULL AND round_number IS NOT NULL
GROUP BY attacker_steamid, round_number
"""

SIDE_SPLIT = """
SELECT k.attacker_steamid, k.attacker_side, COUNT(*) AS kills
FROM kills k
WHERE k.match_id = :mid AND k.exclusion IS NULL
  AND k.attacker_side IS NOT NULL
GROUP BY k.attacker_steamid, k.attacker_side
"""

WEAPONS = """
SELECT weapon, COUNT(*) AS kills, SUM(headshot) AS hs
FROM kills
WHERE match_id = :mid AND exclusion IS NULL AND attacker_steamid = :sid
GROUP BY weapon ORDER BY kills DESC
"""

ROUND_LOG = """
SELECT round_number, winner_side, reason
FROM rounds WHERE match_id = :mid ORDER BY round_number
"""

EXCLUSIONS = """
SELECT exclusion, COUNT(*) FROM kills
WHERE match_id = :mid AND exclusion IS NOT NULL GROUP BY exclusion
"""


# ------------------------------------------------------------------- helpers

BLOCKS = " ▁▂▃▄▅▆▇█"


def spark(values: list[int]) -> str:
    if not values:
        return ""
    hi = max(values)
    if hi == 0:
        return BLOCKS[0] * len(values)
    return "".join(BLOCKS[min(8, round(v / hi * 8))] for v in values)


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"no database at {DB_PATH}")
        print("run spike/extract.py first")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------- views

def list_matches(conn) -> list[sqlite3.Row]:
    rows = conn.execute(MATCH_LIST).fetchall()
    if not rows:
        print("no matches in the database -- run spike/extract.py")
        sys.exit(1)

    print("\nmatches (newest first)")
    for i, r in enumerate(rows, start=1):
        print(f"  [{r['match_id']}] - {i}"
              f"    {r['map_name'] or '?':<12s} {r['rounds']:>2d} rounds")
    return rows


def show_match(conn, mid: str) -> None:
    args = {"mid": mid}
    m = conn.execute("SELECT * FROM matches WHERE match_id = ?",
                     (mid,)).fetchone()

    print(f"\n{'=' * 66}")
    print(f"match {mid}   {m['map_name']}   {m['rounds']} rounds")
    print(f"patch {m['patch_version']}   rounds numbered via "
          f"{m['round_source']}")

    # ---- score, per roster
    wins = {r["team"]: r["wins"] for r in conn.execute(TEAM_SCORE, args)}
    board = conn.execute(SCOREBOARD, args).fetchall()
    n_rounds = m["rounds"]

    # ---- opening duels
    open_k, open_d, open_k_won = {}, {}, {}
    for r in conn.execute(OPENING, args):
        open_k[r["attacker_steamid"]] = open_k.get(r["attacker_steamid"], 0) + 1
        open_d[r["victim_steamid"]] = open_d.get(r["victim_steamid"], 0) + 1
        if r["winner_side"] == r["attacker_side"]:
            open_k_won[r["attacker_steamid"]] = \
                open_k_won.get(r["attacker_steamid"], 0) + 1

    # ---- per-round kills: multikills and shape
    per_round = {}
    for r in conn.execute(PER_ROUND, args):
        per_round.setdefault(r["attacker_steamid"], {})[
            r["round_number"]] = r["kills"]

    # ---- side splits
    split = {}
    for r in conn.execute(SIDE_SPLIT, args):
        split.setdefault(r["attacker_steamid"], {})[
            r["attacker_side"]] = r["kills"]

    by_team = {}
    for row in board:
        by_team.setdefault(row["team"], []).append(row)

    for team in sorted(by_team, key=lambda t: -wins.get(t, 0)):
        players = sorted(by_team[team], key=lambda r: -r["kills"])
        started = players[0]["started_side"] if players else "?"
        print(f"\nteam {team}   {wins.get(team, 0)} rounds   "
              f"(started {started})")
        print(f"  {'player':<18s} {'K':>3s} {'D':>3s} {'A':>3s} "
              f"{'+/-':>4s} {'ADR':>6s} {'ADR*':>6s} {'HS%':>5s} "
              f"{'KPR':>5s} {'MVP':>3s}  {'rounds':<20s}")
        print("  " + "-" * 84)

        for r in players:
            k, d = r["kills"], r["deaths"]
            rounds = r["rounds_present"] or n_rounds
            adr_raw = r["raw_dmg"] / max(rounds, 1)
            adr_cl = r["clamped_dmg"] / max(rounds, 1)
            hs_pct = (r["hs"] / k * 100) if k else 0.0
            shape = [per_round.get(r["steamid"], {}).get(i, 0)
                     for i in range(1, n_rounds + 1)]
            flag = "*" if r["left_early"] else " "
            print(f"  {(r['name'] or '?')[:18]:<18s} "
                  f"{k:>3d} {d:>3d} {r['assists']:>3d} "
                  f"{k - d:>+4d} {adr_raw:>6.1f} {adr_cl:>6.1f} "
                  f"{hs_pct:>4.0f}% {k / max(rounds, 1):>5.2f} "
                  f"{(r['mvps'] or 0):>3d}  {spark(shape):<20s}{flag}")

    # ---- detail per player
    print(f"\n{'-' * 66}\ndetail")
    print(f"  {'player':<18s} {'open K':>6s} {'open D':>6s} {'oK win':>7s} "
          f"{'CT/T':>9s} {'2K':>3s} {'3K':>3s} {'4K':>3s} {'5K':>3s} "
          f"{'util':>5s} {'flash':>6s} {'taken':>6s}")
    print("  " + "-" * 92)
    for r in sorted(board, key=lambda r: -r["kills"]):
        sid_ = r["steamid"]
        rk = per_round.get(sid_, {})
        multi = {n: sum(1 for v in rk.values() if v == n) for n in (2, 3, 4, 5)}
        s = split.get(sid_, {})
        ok = open_k.get(sid_, 0)
        won = open_k_won.get(sid_, 0)
        print(f"  {(r['name'] or '?')[:18]:<18s} "
              f"{ok:>6d} {open_d.get(sid_, 0):>6d} "
              f"{(f'{won / ok * 100:.0f}%' if ok else '-'):>7s} "
              f"{f'{s.get(chr(67) + chr(84), 0)}/{s.get(chr(84), 0)}':>9s} "
              f"{multi[2]:>3d} {multi[3]:>3d} {multi[4]:>3d} {multi[5]:>3d} "
              f"{r['util_dmg']:>5d} {r['flash_assists']:>6d} "
              f"{r['dmg_taken']:>6d}")

    # ---- round log
    print(f"\n{'-' * 66}\nrounds")
    log = conn.execute(ROUND_LOG, args).fetchall()
    for i in range(0, len(log), 5):
        chunk = log[i:i + 5]
        print("  " + "   ".join(
            f"{r['round_number']:>2d} {r['winner_side']:<2s} "
            f"{(r['reason'] or '')[:14]:<14s}" for r in chunk))

    exc = conn.execute(EXCLUSIONS, args).fetchall()
    if exc:
        print(f"\nexcluded kills: "
              f"{ {r['exclusion']: r[1] for r in exc} }")

    print("\nADR  = raw dmg_health / rounds")
    print("ADR* = clamped to victim remaining health / rounds  <- likely correct")
    if any(r["left_early"] for r in board):
        print("*    = not present at the final round")


# ---------------------------------------------------------------------- main

def main() -> int:
    if "--sql" in sys.argv:
        for name, q in (("MATCH_LIST", MATCH_LIST), ("TEAM_SCORE", TEAM_SCORE),
                        ("SCOREBOARD", SCOREBOARD), ("OPENING", OPENING),
                        ("PER_ROUND", PER_ROUND), ("SIDE_SPLIT", SIDE_SPLIT)):
            print(f"\n-- {name}{q}")
        return 0

    conn = connect()
    rows = list_matches(conn)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        choice = args[0]
    else:
        try:
            choice = input("\nwhich match do you want to choose : ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

    if not choice:
        return 0

    match = None
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        match = rows[int(choice) - 1]
    else:
        for r in rows:
            if r["match_id"] == choice or choice in r["match_id"]:
                match = r
                break

    if match is None:
        print(f"no match for {choice!r}")
        return 1

    show_match(conn, match["match_id"])
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())