r"""Render a CS2 scoreboard from a parsed demo.

Run:
    .\.venv\Scripts\python.exe spike\scoreboard.py
    .\.venv\Scripts\python.exe spike\scoreboard.py 003835503793596793281_0144217731

Plumbing (roster, per-round sides, team resolution, score, filtering, display)
is complete. The statistics themselves live in compute_player_stats() and are
yours to write -- see the docstring there.

Key facts this encodes, all established by measurement rather than assumption:

  * round_end.winner is a SIDE ('CT'/'T'), not a team. Sides swap, so counting
    winners directly yields a side split, never a match score.
  * total_rounds_played counts COMPLETED rounds (0 during round 1), so activity
    at >= len(rounds) is post-match and must be dropped.
  * team_num 2 = T, 3 = CT. 1 is spectator and must never be treated as a side.
  * Side swap points are DERIVED, never hardcoded. MR12 swaps once at 12, but
    overtime swaps every three rounds and the format has changed before.
  * SteamIDs are str, so store as TEXT and never compare against ints.
"""
import sys
from pathlib import Path

import pandas as pd
from demoparser2 import DemoParser

EXTRACTED = Path(r"C:\Users\willi\Projects\csstats-data\extracted")
DEFAULT_DEMO = "003836072496658907557_1315824633"

PLAYER_PROPS = ["team_num", "team_name", "health", "armor_value"]
OTHER_PROPS = ["total_rounds_played", "is_warmup_period", "game_time",
               "round_start_time"]

SIDE = {2: "T", 3: "CT"}
PLAYING = (2, 3)     # anything else (0 unassigned, 1 spectator) is not a side


# ---------------------------------------------------------------- loading

def load(path: Path) -> dict:
    p = DemoParser(str(path))
    return {
        "parser": p,
        "kills": p.parse_event("player_death", player=PLAYER_PROPS,
                               other=OTHER_PROPS),
        "hurt": p.parse_event("player_hurt", player=PLAYER_PROPS,
                              other=OTHER_PROPS),
        "rounds": p.parse_event("round_end", other=OTHER_PROPS),
    }


def side_table(p: DemoParser, rounds: pd.DataFrame) -> pd.DataFrame:
    """Every player's side and MVP count at the end of every round.

    One tick query serves three purposes: per-round side resolution, the
    player roster (as a union across all rounds, so anyone who disconnects
    before the end is not lost), and MVP counts.
    """
    ticks = sorted(int(t) for t in rounds["tick"].unique())

    last = None
    for props in (["team_num", "mvps"], ["team_num"]):
        try:
            df = p.parse_ticks(props, ticks=ticks)
            if "mvps" not in df.columns:
                df["mvps"] = pd.NA
            return df
        except Exception as err:
            last = err
    raise RuntimeError(f"could not read tick data: {last!r}")


# ------------------------------------------------------- team resolution

def _team_a_side(grp: pd.DataFrame, team_of: dict) -> str | None:
    """Which side team A held in this round, by majority vote.

    Votes from team B count in reverse, so a round in which only B players
    remain is still resolvable. Returns None only when no already-assigned
    player was present at all.
    """
    votes = []
    for r in grp.itertuples():
        team = team_of.get(r.steamid)
        side = SIDE.get(r.team_num)
        if team is None or side is None:
            continue
        if team == "A":
            votes.append(side)
        else:
            votes.append("T" if side == "CT" else "CT")
    if not votes:
        return None
    return max(set(votes), key=votes.count)


def resolve_teams(sides: pd.DataFrame) -> dict:
    """Assign every player to team 'A' or 'B'.

    Teams are rosters; sides are positions that swap. Assignment propagates
    outward from the best-populated round, so it tolerates players who
    disconnect early, join late, or sit out part of the match. A player is
    only ever assigned from a round in which they were actually playing.
    """
    live = sides[sides["team_num"].isin(PLAYING)]
    if live.empty:
        return {}

    by_tick = {t: g for t, g in live.groupby("tick")}

    # anchor on the round with the most players present
    anchor = max(by_tick, key=lambda t: by_tick[t]["steamid"].nunique())
    team_of = {r.steamid: ("A" if r.team_num == 2 else "B")
               for r in by_tick[anchor].itertuples()}

    # propagate: once team A's side is known at a tick, anyone else present
    # in that tick can be assigned from whether they match it
    for _ in range(4):
        added = 0
        for grp in by_tick.values():
            a_side = _team_a_side(grp, team_of)
            if a_side is None:
                continue
            for r in grp.itertuples():
                if r.steamid in team_of:
                    continue
                mine = SIDE.get(r.team_num)
                team_of[r.steamid] = "A" if mine == a_side else "B"
                added += 1
        if not added:
            break

    return team_of


def match_score(rounds: pd.DataFrame, sides: pd.DataFrame,
                team_of: dict) -> dict:
    """Round wins per roster, with derived swap points."""
    live = sides[sides["team_num"].isin(PLAYING)]
    by_tick = {t: g for t, g in live.groupby("tick")}

    wins = {"A": 0, "B": 0}
    a_side_by_round = {}
    flips, prev, unresolved = [], None, []

    for r in rounds.sort_values("tick").itertuples():
        rno = int(getattr(r, "round", r.total_rounds_played + 1))
        grp = by_tick.get(int(r.tick))
        a_side = _team_a_side(grp, team_of) if grp is not None else None

        if a_side is None:
            unresolved.append(rno)
            continue

        a_side_by_round[rno] = a_side
        if prev is not None and a_side != prev:
            flips.append(rno)
        prev = a_side

        wins["A" if r.winner == a_side else "B"] += 1

    print(f"  rosters: A={sum(v == 'A' for v in team_of.values())} "
          f"B={sum(v == 'B' for v in team_of.values())}")
    print(f"  side flips at round: {flips or 'none'}")
    if unresolved:
        print(f"  unresolved rounds: {unresolved}")
    print(f"  SCORE  A {wins['A']} - {wins['B']} B")

    return {"wins": wins, "a_side_by_round": a_side_by_round}


def roster(sides: pd.DataFrame, team_of: dict) -> pd.DataFrame:
    """One row per player ever seen, with final side and MVP count.

    Built from the union across all rounds so that anyone who disconnected
    mid-match still appears -- they played, they have statistics, and the
    in-game scoreboard would show them too.
    """
    live = sides[sides["team_num"].isin(PLAYING)].sort_values("tick")
    last = live.groupby("steamid").last()

    out = pd.DataFrame({
        "steamid": last.index,
        "name": last["name"].values,
        "final_side": [SIDE.get(t) for t in last["team_num"].values],
        "team": [team_of.get(s) for s in last.index],
    })

    if "mvps" in live.columns and live["mvps"].notna().any():
        peak = live.groupby("steamid")["mvps"].max()
        out["mvps"] = [peak.get(s) for s in out["steamid"]]
    else:
        out["mvps"] = pd.NA

    last_tick = int(sides["tick"].max())
    present = set(sides[sides["tick"] == last_tick]["steamid"])
    out["left_early"] = [s not in present for s in out["steamid"]]

    return out.reset_index(drop=True)


# ---------------------------------------------------------------- filtering

def clean(df: pd.DataFrame, n_rounds: int, label: str) -> pd.DataFrame:
    """Drop warmup and post-match activity."""
    before = len(df)
    out = df[~df["is_warmup_period"].astype(bool)]
    out = out[out["total_rounds_played"] < n_rounds]
    dropped = before - len(out)
    if dropped:
        print(f"  {label}: filtered {dropped} of {before} "
              f"(warmup / post-match)")
    return out.copy()


# ---------------------------------------------------------------- statistics

def compute_player_stats(kills: pd.DataFrame,
                         hurt: pd.DataFrame,
                         players: pd.DataFrame,
                         n_rounds: int) -> pd.DataFrame:
    """Return one row per player, indexed by steamid (str).

    YOURS TO WRITE. Required columns: kills, deaths, assists, damage, adr,
    hs_pct.

        kills     Credited to attacker_steamid. EXCLUDE suicides
                  (attacker == user), world deaths (attacker_steamid is NaN)
                  and team kills (same team_num). A team kill still counts as
                  a DEATH for the victim.

        deaths    Every row per user_steamid, however caused.

        assists   assister_steamid.notna() -- missing assists are NaN despite
                  the str dtype, so never test == "". Flash assists are a
                  SUBSET, flagged by assistedflash, not a separate field.

        damage    Sum dmg_health from `hurt`, NEVER from `kills`
                  (kills.dmg_health is the killing blow only). Exclude self
                  and team damage.
                  OPEN: is dmg_health clamped to remaining health, or raw
                  including overkill? Different ADR either way. Settle by hand
                  against one round of the in-game scoreboard.

        adr       damage / rounds. A player who joined late or left early has
                  a smaller denominator than n_rounds -- see the left_early
                  column on `players`. This is a common source of
                  disagreement with the in-game numbers.

        hs_pct    Headshot kills / kills, same exclusions as kills.

    Reindex on players['steamid'] so that zero-kill, zero-death and
    disconnected players all still appear. groupby alone silently drops them.
    """
    # --- worked example, to show the expected shape ---------------------
    deaths = kills.groupby("user_steamid").size().rename("deaths")
    stats = deaths.reindex(players["steamid"]).fillna(0).to_frame()

    # --- your work starts here -----------------------------------------
    raise NotImplementedError(
        "compute_player_stats: see docstring. Delete this raise as you go."
    )
    return stats

# ---------------------------------------------------------------- display

def team_label(team: str, score: dict) -> str:
    """'team A (started T, 7 rounds)' -- more use than a bare letter."""
    wins = score.get("wins", {}).get(team, "?")
    by_round = score.get("a_side_by_round", {})
    first = by_round.get(min(by_round)) if by_round else None
    if first:
        started = first if team == "A" else ("CT" if first == "T" else "T")
        return f"team {team}  (started {started}, {wins} rounds)"
    return f"team {team}  ({wins} rounds)"


def render_roster(players: pd.DataFrame, score: dict) -> None:
    """Teams and names only. Used when statistics are unavailable."""
    for team in ("A", "B"):
        block = players[players["team"] == team]
        if block.empty:
            continue
        print(f"\n{team_label(team, score)}")
        print(f"  {'player':<20s} {'ended':>5s} {'MVP':>4s}")
        print("  " + "-" * 31)
        for _, r in block.iterrows():
            mvp = "" if pd.isna(r["mvps"]) else f"{int(r['mvps']):d}"
            flag = " *" if r["left_early"] else ""
            print(f"  {str(r['name'])[:20]:<20s} "
                  f"{str(r['final_side'] or '?'):>5s} {mvp:>4s}{flag}")

    _render_notes(players)


def render(stats: pd.DataFrame, players: pd.DataFrame, score: dict) -> None:
    merged = players.merge(stats, left_on="steamid", right_index=True,
                           how="left")

    needed = ["kills", "deaths", "assists", "damage", "adr", "hs_pct"]
    missing = [c for c in needed if c not in merged.columns]
    if missing:
        print(f"\ncompute_player_stats did not return: {missing}")
        print("falling back to roster view")
        render_roster(players, score)
        return

    for col in needed:
        merged[col] = merged[col].fillna(0)

    header = (f"  {'player':<20s} {'K':>4s} {'D':>4s} {'A':>4s} "
              f"{'ADR':>7s} {'HS%':>6s} {'MVP':>4s}")

    for team in ("A", "B"):
        block = merged[merged["team"] == team]
        if block.empty:
            continue
        print(f"\n{team_label(team, score)}")
        print(header)
        print("  " + "-" * (len(header) - 2))

        block = block.sort_values(["kills", "damage"], ascending=False)
        for _, r in block.iterrows():
            mvp = "" if pd.isna(r["mvps"]) else f"{int(r['mvps']):d}"
            flag = " *" if r["left_early"] else ""
            print(f"  {str(r['name'])[:20]:<20s} "
                  f"{int(r['kills']):>4d} {int(r['deaths']):>4d} "
                  f"{int(r['assists']):>4d} {r['adr']:>7.1f} "
                  f"{r['hs_pct']:>5.1f}% {mvp:>4s}{flag}")

    _render_notes(merged)


def _render_notes(df: pd.DataFrame) -> None:
    unassigned = df[df["team"].isna()]
    if not unassigned.empty:
        print(f"\nunassigned to a team: {list(unassigned['name'])}")
    if df["left_early"].any():
        print("\n* not present at the final round")


# ---------------------------------------------------------------- main

def find_demo(stem: str) -> Path | None:
    exact = EXTRACTED / f"{stem}.dem"
    if exact.exists():
        return exact
    matches = [d for d in sorted(EXTRACTED.glob("*.dem")) if stem in d.stem]
    if len(matches) == 1:
        return matches[0]
    print(f"{stem!r} is ambiguous" if matches else f"no demo matching {stem!r}")
    print("available:")
    for d in sorted(EXTRACTED.glob("*.dem")):
        print(f"  {d.stem}")
    return None


def main() -> int:
    path = find_demo(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEMO)
    if path is None:
        return 1

    print(f"{path.name}")

    try:
        data = load(path)
    except Exception as err:
        print(f"  parse failed: {err!r}")
        return 1

    rounds = data["rounds"]
    if not hasattr(rounds, "columns") or rounds.empty:
        print("  no round_end data -- cannot resolve rounds or score")
        return 1

    n_rounds = len(rounds)
    print(f"  rounds played: {n_rounds}")

    if "reason" in rounds.columns:
        print(f"  round_end reasons: "
              f"{rounds['reason'].value_counts().to_dict()}")
    if "winner" in rounds.columns:
        print(f"  wins by SIDE (not a score): "
              f"{rounds['winner'].value_counts().to_dict()}")

    try:
        sides = side_table(data["parser"], rounds)
        team_of = resolve_teams(sides)
        score = match_score(rounds, sides, team_of) if team_of else {}
        players = roster(sides, team_of)
    except Exception as err:
        print(f"  team resolution failed: {err!r}")
        return 1

    if players.empty:
        print("  no players found")
        return 1

    kills = clean(data["kills"], n_rounds, "kills")
    hurt = clean(data["hurt"], n_rounds, "hurt")

    try:
        stats = compute_player_stats(kills, hurt, players, n_rounds)
    except NotImplementedError as err:
        print(f"\n[stats not implemented: {err}]")
        render_roster(players, score)
        return 0
    except Exception as err:
        print(f"\ncompute_player_stats raised {err!r}")
        render_roster(players, score)
        return 1

    render(stats, players, score)
    return 0


if __name__ == "__main__":
    sys.exit(main())