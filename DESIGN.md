# DESIGN.md — csstats

Dated record of non-obvious decisions and measured findings, and why.

**Conventions**

- Entries are chronological. Append new ones at the bottom.
- Every entry is tagged `[settled]`, `[provisional]` (working assumption, revisit
  if it bites) or `[open]` (unresolved, blocking or not).
- "Verified" means observed in output, not inferred. Inferences are labelled as
  such — see 2026-08-10-J for why that distinction earned its own entry.
- Findings that came from measurement are separated from decisions that came
  from judgement. Both matter; conflating them is how a guess becomes a fact.

---

## Carried over from the build plan (pre-dating this log)

These were decided during planning. Recorded here so the log is self-contained.

| Decision | Reason |
|---|---|
| gevent end to end, never asyncio | Forced by ValvePython/steam. One process, one concurrency model, no subprocess boundary. `requests` not httpx; `gevent.pywsgi` not aiohttp in Phase 8. |
| SQLite is the single source of truth | No export layer in v1. CSV cut. |
| Five-second trade window | Documented default; raw timestamps preserved so it can be recomputed. |
| `expired` is a status distinct from `permanent_failure` | Valve deletes replays after ~1–2 weeks. Expiry is a normal outcome, not a bug, and must not trigger retry storms. |
| GSI is a latency optimisation, never a data source | History polling alone must produce a complete dataset. GSI must not be able to create a match record. |
| Positions table deferred to v2 | Nothing in Phase 5 stats needs it; largest table by an order of magnitude. |
| No CSV export in v1 | SQL views plus a committed `queries/` directory instead. |

---

## 2026-08-10-A — Fork SyberiaK/csgo, not ValvePython/csgo `[settled]`

Upstream is abandoned and does not connect to the CS2 GC. The SyberiaK fork
exists specifically to fix that. Forking upstream would mean re-solving the
connection problem from scratch, blind, with no working reference to compare
against — unpaid work for no additional credit.

Fork the fork. The contribution then becomes the thing actually missing:
CS2-era protobufs verified against a live GC.

**Rejected alternatives**

- `ValvePython/csgo` — abandoned, known not to connect.
- `Gobot1234/steam.py` + `steam-ext-csgo` — asyncio-native (would avoid gevent
  entirely), but CS:GO-era and unmaintained. Held as second Python attempt if
  the fork fails, before reaching for Node.
- `AlisaLC/CSUtils` — uses Boiler, which drives the local client. Excluded, see
  entry B.
- Node `steam-user` + `globaloffensive` (per `claabs/cs-demo-downloader`) — the
  documented fallback only. Its config surface was used to sanity-check the
  `.env` list in Phase 1.

## 2026-08-10-B — The Boiler objection is architectural, not absolute `[settled]`

boiler-writter works through the local logged-in Steam client and briefly
launches CS2 in the background. That conflicts with a service running *while I
play*, so it cannot be a production dependency.

It does not conflict with sitting at the desk once, by hand, to pull fixtures.
CS Demo Manager was therefore acceptable for the one-off fixture grab. Keep it
away from the repo.

## 2026-08-10-C — Fork staleness measured; protos deliberately not refreshed yet `[provisional]`

```
last commit overall : 2025-03-01  "fix circular import"
last commit protos  : 2021-02-01  "update protobufs"
```

The 2025 commit is packaging hygiene, so the "fixed connecting to GC" claim
pre-dates it. The protobufs are CS:GO-era — two and a half years before CS2
shipped, five and a half years old at time of writing.

Judged **not** disqualifying: protobuf tolerates added fields (unknown fields
are skipped, not fatal), `MatchListRequestFullGameInfo` dates to ~2016, and the
pipeline needs exactly one field out of the GC — the replay URL. Everything
else comes from demoparser2.

**Sequencing decision:** do not refresh protos before 0b. Refreshing first means
two suspects and no way to separate them if the spike fails. Run stale, then
decide with evidence.

Refresh is cheaper than the 15–30h in the plan notes — that estimate was for
reverse-engineering against a live service. Current protos are published and
auto-tracked (`SteamDatabase/Protobufs` under `csgo/`;
`SteamTracking/GameTracking-CS2` under `Protobufs/`), and the fork ships
`protobuf_list.txt` and a Makefile. Expected friction is `protoc`/`grpcio-tools`
output versus whatever `protobuf` runtime `steam` pins, not the protos.

## 2026-08-10-D — Persist raw GC bytes, not the parsed dict `[settled]`

Amends Phase 3. With 2021-era protos, a parsed dict is a *lossy* view: anything
Valve added since is silently dropped. Store the serialised protobuf bytes as a
BLOB.

Consequence: refreshing protos later re-parses every payload ever stored,
without a second GC call. Turns proto staleness from data loss into deferred
decoding.

Also: inspect `UnknownFields()` on the first real `full_match_info` to quantify
the gap. Empty means the 2021 definitions cover everything CS2 sends and the
refresh question closes. `[open]` until 0b runs.

## 2026-08-10-E — Two-gate split for the 0b spike `[settled]`

Amends Phase 0b, which treated the spike as one pass/fail. It is two, failing
for unrelated reasons:

- **0b-i** — does the GC accept us? Login, `launch()`, wait for ready, dump
  `MatchmakingGC2ClientHello`. Arrival means the fork's connection fix survived.
  That message carries `required_appid_version`, so Valve states its expected
  client version on the wire — log it verbatim.
- **0b-ii** — does `request_full_match_info` return? Separate failure. Precedent
  exists: ValvePython/csgo issue #39 reports `request_live_game_for_user`
  silently ceasing to emit its event after a CS:GO update, with the GC
  connection itself healthy. Ready fires, request goes out, nothing comes back.

Pre-flight, free: grep the fork for a hardcoded version/protocol constant sent
in `EMsgGCClientHello`. If present it is 17+ months stale and
node-globaloffensive has a current value. If absent, the hello is
version-agnostic — one fewer failure mode.

Enable `verbose_debug` on the CSGOClient from the first run. It logs every GC
message with enum name and ID, which is what distinguishes "rejected" from
"connected but confused".

## 2026-08-10-F — Layout: demos live outside the repository `[settled]`

```
Projects\
├── csgo\           # the fork — dependency, keep pristine, no fixtures here
├── csstats\        # this project
│   ├── docs\plan.md
│   ├── fixtures\   # committed: schema JSON + per-demo cache
│   ├── spike\      # gitignored (except 0a, see entry G)
│   └── .venv\      # gitignored
└── csstats-data\   # demos, outside the repo entirely
    ├── demos\      # .dem.bz2 as downloaded
    └── extracted\  # decompressed
```

Outside, not merely gitignored: three demos are ~490 MB extracted and Phase 5
wants twenty-plus. A gitignore does not stop IDE indexing, antivirus, or a
forced add at 1am. It also makes the demo directory *configuration* from day
one, which is where Phase 1's cache-retention setting was always going.

Small derived artifacts stay committed in `fixtures\`. Large opaque binaries do
not. A 500 MB demo is not a fixture; it is the input a fixture was derived from.

Nothing of mine goes in `csgo\` — a future PR should diff proto regeneration and
nothing else.

## 2026-08-10-G — `spike/` is gitignored except the 0a decoder `[settled]`

Phase 3 reuses the 0a share-code implementation, so it is the one spike artifact
that survives into production — and the piece most worth having authored. Losing
its history would be a shame. Either un-ignore that file or write it directly at
`src/csstats/sharecode.py` with a thin spike wrapper.

## 2026-08-10-H — `src/csstats/__init__.py` must stay empty `[settled]`

Under `python -m csstats`, `__init__.py` executes **before** `__main__.py`. Any
import in `__init__.py` therefore runs before `monkey.patch_all()`, and the
patching is silently too late — `ssl`/`socket` end up unpatched and the symptom
appears much later as a blocked greenlet or a hanging Steam client, miles from
the cause.

```python
# __main__.py
from gevent import monkey; monkey.patch_all()   # MUST be first
from csstats.cli import main
main()
```

Recorded because a future me, or an agent doing Phase 1 config plumbing, will
helpfully "tidy up" by adding a convenient re-export.

Also adopting a `src/` layout: with a flat layout, running from the repo root
imports the local directory whether or not the install is correct, so packaging
mistakes stay hidden until you are somewhere else.

---

# Verified findings — demoparser2 and the demo corpus

Measured from three demos, not inferred. Cached per-demo in `fixtures/raw/`,
summarised into `fixtures/demoparser_schema.json`.

## Corpus

| file (match ID) | map | size | rounds | note |
|---|---|---|---|---|
| `…3835503793596793281_0144217731` | de_inferno | 138 MB | 13 | 13-round stomp with a side switch |
| `…3836068263968636986_1097876871` | — | 159 MB | — | |
| `…3836072496658907557_1315824633` | — | 192 MB | — | newest; **Phase 5 ground truth** |

Provenance confirmed from the header: `server_name` "Valve Counter-Strike 2
london Server", `client_name` "SourceTV Demo", `game_directory`
`/opt/srcds/cs2/csgo_v2000880/csgo`. Official Valve matchmaking, server-side
GOTV — correct shape for the schema fixture, no third-party contamination.

bz2 ratios 1.44–1.51×. Demo payloads are already protobuf-packed and largely
incompressible. Phase 4 should reserve ~1.5× the download size before
decompressing.

`0144` is a **poor ground-truth choice** (13 rounds, lopsided) but a **good edge
case** — it exercises a one-sided result and a side switch together.

## 2026-08-10-I — `list_game_events()` reports occurrence, not capability `[settled]`

50 events observed across the corpus; 45 in all three. The variation is
*gameplay*, not version or source drift:

- `bomb_defused` present in one demo only — nobody defused in the other two.
- `bomb_exploded` absent from one — nothing detonated in that match.
- `other_death` in one — a chicken or breakable.
- `round_time_warning` absent from `0144` — rounds ended before the timer.

**Consequence for the Phase 4 rule.** "Use only field names present in the
fixture" was meant to prevent invention. But an occurrence-derived fixture makes
absence meaningless. Had the corpus been the first two demos only, the correct
conclusion would have been "`bomb_defused` does not exist" — and defuse handling
would have been silently absent until the first defused round in production.

Reframed: the fixture is a **whitelist of confirmed-real names and a lower bound
on the schema**. Present ⇒ safe to use. Absent ⇒ unobserved, *not* nonexistent.

Therefore:

1. Fixture stores the **union** with per-event demo counts, not the
   intersection. Single-observation events are flagged as weak evidence
   (currently `bomb_defused`, `other_death`).
2. Fixture carries an explicit `events_expected_but_unobserved` list — currently
   `bomb_beginplant`, `bomb_abortplant`, `bomb_abortdefuse`, `bomb_dropped`,
   `bomb_pickup` — so silence is not read as proof of absence.
3. **Phase 4 logs unknown events rather than ignoring them.** Turns fixture gaps
   into a visible signal instead of silent data loss, and is how the fixture
   grows correctly.

## 2026-08-10-J — Post-plant rounds have three outcomes; `round_end` reason is authoritative `[settled]`

I asserted an invariant — `planted == defused + exploded` — and the data
falsified it:

| demo | planted | defused | exploded | begindefuse |
|---|---|---|---|---|
| 0144 | 10 | 0 | 1 | 0 |
| 1097 | 7 | 0 | 1 | 1 |
| 1315 | 11 | 5 | 0 | 7 |

The missing case: **if the last CT dies after the plant, the round ends
immediately as a T win and the bomb never detonates.** Detonation actually
requires a CT still alive at the timer — alive but out of time, out of reach, or
without a kit, which is why it is the rarest outcome here. Nine of `0144`'s ten
plants were post-plant CT wipes.

Invariants that do hold across all three:

```
defused ≤ begindefuse ≤ planted
defused + exploded ≤ planted
```

`1315`: 7 begindefuse for 5 defuses — two interrupted attempts, directly useful
for clutch bomb state.

**The actual lesson, which is why this entry exists:** a plausible game rule was
inferred and treated as ground truth. Do not derive round classification from
inference. `round_end` carries the authoritative reason (already in the Phase 4
capture list) — read it, and treat bomb events as corroboration only.

## 2026-08-10-K — `parse_event` returns an empty list, not a DataFrame `[settled]`

When an event never fired, the return is `[]`, so `.columns` raises
`AttributeError`. Not a parse failure — a type mismatch. Confirmed by
`bomb_defused` succeeding on `1315` while `bomb_exploded` came back empty there.

Phase 4 normalises rather than catching:

```python
result = parser.parse_event(name)
if not isinstance(result, pd.DataFrame) or result.empty:
    continue          # event did not occur — normal, not an error
```

The spike records these as `empty: True` inside `events`, not in `errors`, so a
non-occurring event still contributes schema information.

## 2026-08-10-L — The fixture records a projection, not a schema `[settled]`

demoparser2 returns only what is requested via `player=` and `other=`. The
default `player_death` projection omits round number, team, side, warmup flag,
positions and match clock — those are absent from the *call*, not from the demo.

Two consequences. `is_warmup_period` on a *tick* query does not filter kills; it
must be an `other` prop on each event, or Phase 4 ends up joining events to
ticks to decide what was warmup. And the Phase 4 rule must read the fixture as
"names confirmed available under this call signature", not "all names that
exist".

Props confirmed accepted on all 12 events across all 3 demos:

```
player: X, Y, Z, team_num, team_name, health, armor_value
other : total_rounds_played, is_warmup_period, game_time, round_start_time
```

`game_time` and `round_start_time` were the two in doubt. Both accepted — match
clock is available per event, as Phase 4 requires. The bare-call fallback never
fired.

**No column drift** across all parsed events in all three demos, before or after
widening the projection. Strong evidence these demos share a schema.

`parse_ticks` returns `steamid` and `name` unrequested, satisfying "identify by
SteamID64" and "store names at match time" without reconstructing the mapping.

## 2026-08-10-M — Two Phase 5 formula traps `[settled]`

**Flash assists are not a separate field.** There is `assister_steamid` plus an
`assistedflash` boolean. A flash assist is an assist with the flag set, not a
distinct assister. The plan lists "assister, flash assister" as though they were
two columns.

**ADR must come from `player_hurt`, never from `player_death`.** `dmg_health` on
`player_death` is the killing blow only. Summing it would produce
plausible-looking numbers that are quietly wrong — precisely the Phase 5 failure
mode the plan is most worried about.

## 2026-08-10-N — Spike caching: parse once, derive many `[settled]`

Split into `schema_parse.py` (slow, caches per demo to `fixtures/raw/`) and
`schema_report.py` (fast, builds the fixture from cache). Re-parsing 490 MB for
millisecond analysis changes was the whole cost of iterating.

Cache key includes the demoparser2 version *and* the prop lists and event
selection — a version-only key silently served stale results when the projection
changed. Same principle as Phase 4 storing raw events and Phase 5 deriving
stats: the architecture in miniature.

---

# Open questions

| # | Question | Blocks | Notes |
|---|---|---|---|
| 1 | Share codes for the three corpus matches | 0a verification, all of 0b | Watch → Your Matches → copy share code. **Critical path.** |
| 2 | Second filename field — is it `outcomeId`? | 0a interpretation | All three are 10 digits (one zero-padded from 9) and under 2³², consistent with uint32 — but far too small for a typical uint64 reservation ID. Let the decode settle it, do not assume the mapping. |
| 3 | `round_end` reason column — values and integer→meaning map | Phase 4 round outcomes | Get distinct values from `1315`, write the mapping into the fixture. Opaque without notes. |
| 4 | Does the fork still reach the CS2 GC? | Everything downstream | 0b-i. The one genuine kill in this project. |
| 5 | How much does `UnknownFields()` show on real `full_match_info`? | Proto refresh decision | See entry D. |
| 6 | Does a hardcoded client version exist in the fork's hello? | 0b-i diagnosis | Free, offline grep. See entry E. |
| 7 | Bot account: does a limited (no-$5) account get GC access? | 0b | Cheap to test, expensive to discover at hour six. |
| 8 | Does `parse_events` (plural, list arg) exist in this version? | Spike efficiency only | Would parse all 50 events in roughly one pass. Not needed — the 12 cover Phase 5. |
| 9 | Does Steam's Personal Game Data page still yield `.dem` files? | Nothing | Would be a client-free route to own demos. Unverified. |

## Match index

Maintained alongside the demos. Per match: sha256 of the `.bz2`, share code,
map, date, final score, and a **scoreboard screenshot**.

The screenshot is not optional. Phase 5's acceptance criterion is defined
against the in-game scoreboard, and once a match ages out of client history that
ground truth is unrecoverable. Ten seconds per match now.

Replays are a wasting asset — Valve deletes them after roughly one to two weeks.
Anything currently in history should be pulled now, and each new match's demo
and share code captured while fresh. Phase 5 needs overtime, ties, and zero-kill
players; those cannot be summoned on demand, only waited for.