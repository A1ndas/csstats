r"""Serve the csstats website and a small read-only JSON API over SQLite.

    python website/serve.py
    -> http://127.0.0.1:8765

Stdlib only, no build step, no dependencies. The database is opened read-only
so the site can never modify it.

All metrics are computed here from raw event rows at request time, the same
way spike/dashboard.py does it -- nothing is read from a stored aggregate, so
changing a definition means editing a query rather than re-parsing demos.
"""
import json
import sqlite3
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB_PATH = ROOT / "data" / "csstats.db"
PORT = 8765

UTIL_WEAPONS = ("hegrenade", "molotov", "incgrenade", "inferno",
                "flashbang", "decoy")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ queries

def list_matches(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT match_id, map_name, rounds, patch_version, parsed_at
        FROM matches
        ORDER BY CAST(match_id AS INTEGER) DESC
    """).fetchall()

    out = []
    for r in rows:
        score = team_wins(conn, r["match_id"])
        ordered = sorted(score.values(), reverse=True)
        out.append({
            "match_id": r["match_id"],
            "map_name": r["map_name"],
            "rounds": r["rounds"],
            "patch_version": r["patch_version"],
            "score": (ordered + [0])[:2],
            "winner": max(score, key=score.get) if score else None,
            "wins_seq": round_winners(conn, r["match_id"]),
        })
    return out


def team_wins(conn, mid: str) -> dict:
    """Round wins per roster.

    round_end.winner is a SIDE, so it is joined through player_rounds to find
    which roster held that side in that round. Counting winners directly would
    give a side split, never a match score.
    """
    rows = conn.execute("""
        SELECT mp.team, COUNT(DISTINCT r.round_number) AS wins
        FROM rounds r
        JOIN player_rounds pr
          ON pr.match_id = r.match_id
         AND pr.round_number = r.round_number
         AND pr.side = r.winner_side
        JOIN match_players mp
          ON mp.match_id = pr.match_id AND mp.steamid = pr.steamid
        WHERE r.match_id = ?
        GROUP BY mp.team
    """, (mid,)).fetchall()
    return {r["team"]: r["wins"] for r in rows}


def round_winners(conn, mid: str) -> list[str]:
    """Winning roster ('A'/'B') per round, in round order, or '' when
    unassignable. Feeds the sidebar sparkline: a per-round record strip
    relative to the match winner."""
    rows = conn.execute("""
        SELECT r.round_number, mp.team
        FROM rounds r
        JOIN player_rounds pr
          ON pr.match_id = r.match_id
         AND pr.round_number = r.round_number
         AND pr.side = r.winner_side
        JOIN match_players mp
          ON mp.match_id = pr.match_id AND mp.steamid = pr.steamid
        WHERE r.match_id = ?
        GROUP BY r.round_number
    """, (mid,)).fetchall()
    by_round = {r["round_number"]: r["team"] for r in rows}
    if not by_round:
        return []
    return [by_round.get(n, "") for n in range(1, max(by_round) + 1)]


def match_detail(conn, mid: str) -> dict | None:
    m = conn.execute("SELECT * FROM matches WHERE match_id = ?",
                     (mid,)).fetchone()
    if m is None:
        return None

    n_rounds = m["rounds"]
    wins = team_wins(conn, mid)

    players = {}
    for r in conn.execute(
            "SELECT * FROM match_players WHERE match_id = ?", (mid,)):
        players[r["steamid"]] = {
            "steamid": r["steamid"],
            "name": r["name"] or "unknown",
            "team": r["team"],
            "started_side": r["started_side"],
            "final_side": r["final_side"],
            "mvps": r["mvps"] or 0,
            "rounds_present": r["rounds_present"] or n_rounds,
            "left_early": bool(r["left_early"]),
            "kills": 0, "deaths": 0, "assists": 0, "flash_assists": 0,
            "hs": 0, "wallbang": 0, "smoke_kills": 0, "blind_kills": 0,
            "raw_dmg": 0, "clamped_dmg": 0, "dmg_taken": 0, "util_dmg": 0,
            "hits": 0, "open_kills": 0, "open_deaths": 0, "open_wins": 0,
            "trade_kills": 0, "traded_deaths": 0,
            "kills_ct": 0, "kills_t": 0,
            "per_round": [0] * n_rounds,
            "multi": {"2": 0, "3": 0, "4": 0, "5": 0},
            "kast_rounds": 0,
        }

    def P(sid):
        return players.get(sid)

    # ---- kills
    kill_rows = conn.execute("""
        SELECT * FROM kills WHERE match_id = ? ORDER BY tick
    """, (mid,)).fetchall()

    for k in kill_rows:
        victim = P(k["victim_steamid"])
        if victim:
            victim["deaths"] += 1

        if k["exclusion"] is not None:
            continue

        att = P(k["attacker_steamid"])
        if att:
            att["kills"] += 1
            att["hs"] += k["headshot"] or 0
            att["wallbang"] += 1 if (k["penetrated"] or 0) > 0 else 0
            att["smoke_kills"] += k["thrusmoke"] or 0
            att["noscope"] = att.get("noscope", 0) + (k["noscope"] or 0)
            att["blind_kills"] += k["attackerblind"] or 0
            if k["attacker_side"] == "CT":
                att["kills_ct"] += 1
            elif k["attacker_side"] == "T":
                att["kills_t"] += 1
            rno = k["round_number"]
            if rno and 1 <= rno <= n_rounds:
                att["per_round"][rno - 1] += 1

        ass = P(k["assister_steamid"])
        if ass and k["assister_side"] != k["victim_side"]:
            ass["assists"] += 1
            ass["flash_assists"] += k["assistedflash"] or 0

    # ---- multikills, from the per-round vector
    for p in players.values():
        for n in p["per_round"]:
            if 2 <= n <= 5:
                p["multi"][str(n)] += 1

    # ---- opening duels: first kill of each round
    first_by_round = {}
    for k in kill_rows:
        rno = k["round_number"]
        if rno is None or k["exclusion"] is not None:
            continue
        if rno not in first_by_round:
            first_by_round[rno] = k

    round_winner = {r["round_number"]: r["winner_side"] for r in conn.execute(
        "SELECT round_number, winner_side FROM rounds WHERE match_id = ?",
        (mid,))}

    for rno, k in first_by_round.items():
        att, vic = P(k["attacker_steamid"]), P(k["victim_steamid"])
        if att:
            att["open_kills"] += 1
            if round_winner.get(rno) == k["attacker_side"]:
                att["open_wins"] += 1
        if vic:
            vic["open_deaths"] += 1

    # ---- trades and KAST. 5s window at 64 ticks/s; documented default.
    TRADE_TICKS = 5 * 64
    for i, k in enumerate(kill_rows):
        if k["exclusion"] is not None:
            continue
        # a trade: killing someone who killed a teammate within the window
        for j in range(i - 1, -1, -1):
            prev = kill_rows[j]
            if k["tick"] - prev["tick"] > TRADE_TICKS:
                break
            if prev["exclusion"] is not None:
                continue
            if (prev["attacker_steamid"] == k["victim_steamid"]
                    and prev["victim_side"] == k["attacker_side"]):
                att, avenged = P(k["attacker_steamid"]), P(prev["victim_steamid"])
                if att:
                    att["trade_kills"] += 1
                if avenged:
                    avenged["traded_deaths"] += 1
                break

    # KAST: rounds with a kill, assist, survival, or a traded death
    deaths_by_round = {}
    for k in kill_rows:
        deaths_by_round.setdefault(k["round_number"], set()).add(
            k["victim_steamid"])

    present = {}
    for r in conn.execute(
            "SELECT round_number, steamid FROM player_rounds WHERE match_id = ?",
            (mid,)):
        present.setdefault(r["round_number"], set()).add(r["steamid"])

    contributed = {}
    for k in kill_rows:
        rno = k["round_number"]
        if k["exclusion"] is None and k["attacker_steamid"]:
            contributed.setdefault(rno, set()).add(k["attacker_steamid"])
        if k["assister_steamid"]:
            contributed.setdefault(rno, set()).add(k["assister_steamid"])

    for rno, roster in present.items():
        died = deaths_by_round.get(rno, set())
        for sid in roster:
            p = P(sid)
            if not p:
                continue
            if sid in contributed.get(rno, set()) or sid not in died:
                p["kast_rounds"] += 1

    # ---- damage
    for r in conn.execute("""
        SELECT attacker_steamid, victim_steamid, dmg_health, victim_health,
               weapon, exclusion
        FROM damage WHERE match_id = ?
    """, (mid,)):
        if r["exclusion"] is not None:
            continue
        raw = r["dmg_health"] or 0
        clamped = min(raw, r["victim_health"] or 0)
        att, vic = P(r["attacker_steamid"]), P(r["victim_steamid"])
        if att:
            att["raw_dmg"] += raw
            att["clamped_dmg"] += clamped
            att["hits"] += 1
            if r["weapon"] in UTIL_WEAPONS:
                att["util_dmg"] += clamped
        if vic:
            vic["dmg_taken"] += clamped

    # ---- derived
    for p in players.values():
        rp = max(p["rounds_present"], 1)
        p["adr_raw"] = round(p["raw_dmg"] / rp, 1)
        p["adr"] = round(p["clamped_dmg"] / rp, 1)
        p["kpr"] = round(p["kills"] / rp, 2)
        p["dpr"] = round(p["deaths"] / rp, 2)
        p["kd"] = round(p["kills"] / p["deaths"], 2) if p["deaths"] else None
        p["hs_pct"] = round(p["hs"] / p["kills"] * 100) if p["kills"] else 0
        p["kast"] = round(p["kast_rounds"] / rp * 100)
        p["open_win_pct"] = (round(p["open_wins"] / p["open_kills"] * 100)
                             if p["open_kills"] else None)
        p["traded_pct"] = (round(p["traded_deaths"] / p["deaths"] * 100)
                           if p["deaths"] else None)

    rounds = [dict(r) for r in conn.execute("""
        SELECT round_number, winner_side, reason
        FROM rounds WHERE match_id = ? ORDER BY round_number
    """, (mid,))]

    # which roster held which side per round, for the timeline
    side_of_team = {}
    for r in conn.execute("""
        SELECT pr.round_number, pr.side, mp.team
        FROM player_rounds pr
        JOIN match_players mp ON mp.match_id = pr.match_id
                             AND mp.steamid = pr.steamid
        WHERE pr.match_id = ?
    """, (mid,)):
        side_of_team.setdefault(r["round_number"], {})[r["team"]] = r["side"]

    for r in rounds:
        sides = side_of_team.get(r["round_number"], {})
        r["winner_team"] = next(
            (t for t, s in sides.items() if s == r["winner_side"]), None)

    exclusions = {r[0]: r[1] for r in conn.execute("""
        SELECT exclusion, COUNT(*) FROM kills
        WHERE match_id = ? AND exclusion IS NOT NULL GROUP BY exclusion
    """, (mid,))}

    return {
        "match_id": mid,
        "map_name": m["map_name"],
        "rounds": n_rounds,
        "patch_version": m["patch_version"],
        "server_name": m["server_name"],
        "round_source": m["round_source"],
        "parsed_at": m["parsed_at"],
        "wins": wins,
        "players": sorted(players.values(), key=lambda p: -p["kills"]),
        "round_log": rounds,
        "exclusions": exclusions,
    }


# ------------------------------------------------------------------ handler

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path

        if path.startswith("/api/"):
            try:
                self.serve_api(path)
            except Exception as err:
                self.send_json({"error": repr(err)}, status=500)
            return

        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def serve_api(self, path: str):
        conn = db()
        try:
            if path == "/api/matches":
                return self.send_json(list_matches(conn))

            if path.startswith("/api/match/"):
                mid = unquote(path[len("/api/match/"):])
                data = match_detail(conn, mid)
                if data is None:
                    return self.send_json({"error": "no such match"}, 404)
                return self.send_json(data)

            self.send_json({"error": "unknown endpoint"}, 404)
        finally:
            conn.close()

    def send_json(self, payload, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
            line = " ".join(str(a) for a in args)
            if "/api/" in line:
                super().log_message(fmt, *args)


def main() -> int:
    if not DB_PATH.exists():
        print(f"no database at {DB_PATH}")
        print("run:  python spike/extract.py")
        return 1

    handler = partial(Handler, directory=str(HERE))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    print(f"db      {DB_PATH}")
    print(f"serving http://127.0.0.1:{PORT}   (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
