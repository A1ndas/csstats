/* csstats — fetches from the local JSON API and renders a match.
   No framework, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

let state = { matches: [], current: null, adrMode: "clamped" };

/* ------------------------------------------------------------- utilities */

// Thresholds are rough CS2 norms, used only to colour a number, never to
// compute one. Adjust once there is enough personal history to calibrate.
const band = (v, lo, hi) =>
  v == null ? "dim" : v >= hi ? "good" : v >= lo ? "mid" : "bad";

function fmt(v, digits = 0) {
  if (v == null) return "—";
  return digits ? v.toFixed(digits) : String(v);
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* --------------------------------------------------------------- sidebar */

function renderSidebar() {
  const list = $("#match-list");
  list.innerHTML = "";

  state.matches.forEach((m, i) => {
    const [hi, lo] = m.score;
    const li = el("li", "match-item");
    li.tabIndex = 0;
    li.dataset.id = m.match_id;

    const outcome = hi === lo ? "tie" : "win";
    li.append(el("span", `match-bar ${outcome}`));

    const mid = el("div");
    mid.append(el("div", "match-map", m.map_name || "unknown"));
    mid.append(el("div", "match-sub", `${m.rounds} rounds · #${i + 1}`));
    li.append(mid);

    li.append(el("div", "match-score", `${hi}–${lo}`));

    li.addEventListener("click", () => select(m.match_id));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(m.match_id); }
    });
    list.append(li);
  });

  $("#sidebar-foot").textContent =
    `${state.matches.length} match${state.matches.length === 1 ? "" : "es"} in the database`;
}

function markActive(id) {
  document.querySelectorAll(".match-item").forEach((n) =>
    n.classList.toggle("is-active", n.dataset.id === id));
}

/* ----------------------------------------------------------------- match */

async function select(id) {
  markActive(id);
  const main = $("#main");
  main.innerHTML = "";
  main.append(el("div", "empty", "Loading match…"));
  try {
    state.current = await api(`/api/match/${id}`);
    renderMatch(state.current);
  } catch (err) {
    main.innerHTML = "";
    main.append(el("div", "empty", `Could not load that match: ${err.message}`));
  }
}

function renderMatch(d) {
  const main = $("#main");
  main.innerHTML = "";
  const frag = $("#tpl-match").content.cloneNode(true);
  const F = (name) => frag.querySelector(`[data-f="${name}"]`);

  const teams = Object.keys(d.wins).sort((a, b) => d.wins[b] - d.wins[a]);
  const [winner, loser] = teams;

  F("eyebrow").textContent =
    `match ${d.match_id} · patch ${d.patch_version || "?"}`;
  F("map").textContent = (d.map_name || "unknown").replace(/^de_/, "");

  F("team-a").textContent = `team ${winner}`;
  F("team-b").textContent = loser ? `team ${loser}` : "";
  const sa = F("score-a"), sb = F("score-b");
  sa.textContent = d.wins[winner] ?? 0;
  sb.textContent = loser ? d.wins[loser] ?? 0 : 0;
  sa.classList.add("win");
  sb.classList.add("loss");

  renderTimeline(F("timeline"), d, winner);
  renderTiles(F("tiles"), d);
  renderTeams(F("teams"), d, teams);
  renderDetail(F("detail"), d);
  renderRounds(F("rounds"), d, winner);

  F("adr-note").textContent =
    "ADR clamped counts only health actually removed; raw counts full weapon " +
    "damage including overkill. On this corpus they differ by roughly a " +
    "quarter — clamped is the one expected to match the in-game scoreboard.";

  const ex = Object.entries(d.exclusions || {});
  F("meta").textContent = [
    `rounds numbered via ${d.round_source}`,
    ex.length ? `excluded kills: ${ex.map(([k, v]) => `${v} ${k}`).join(", ")}` : null,
    d.server_name,
    `parsed ${d.parsed_at?.slice(0, 19).replace("T", " ")} UTC`,
  ].filter(Boolean).join("  ·  ");

  main.append(frag);

  main.querySelectorAll(".toggle-btn").forEach((b) =>
    b.addEventListener("click", () => {
      state.adrMode = b.dataset.adr;
      renderMatch(d);
    }));
  main.querySelectorAll(".toggle-btn").forEach((b) =>
    b.classList.toggle("is-on", b.dataset.adr === state.adrMode));
}

function renderTimeline(node, d, winner) {
  d.round_log.forEach((r) => {
    const cell = el("div", "tl");
    cell.classList.add(r.winner_team === winner ? "a" : "b");
    if (r.winner_side) cell.classList.add(r.winner_side.toLowerCase());
    cell.append(el("span", "tl-n", r.round_number));
    cell.title = `round ${r.round_number} — ${r.winner_side} won (${r.reason || "?"})`;
    node.append(cell);
  });
}

function renderTiles(node, d) {
  const ps = d.players;
  const top = [...ps].sort((a, b) => b.kills - a.kills)[0];
  const bestAdr = [...ps].sort((a, b) => b.adr - a.adr)[0];
  const bestOpen = [...ps].filter((p) => p.open_kills)
    .sort((a, b) => b.open_kills - a.open_kills)[0];
  const totalKills = ps.reduce((s, p) => s + p.kills, 0);

  const tiles = [
    ["Rounds", d.rounds, `${totalKills} kills total`],
    ["Top fragger", top?.name ?? "—", `${top?.kills ?? 0} kills`],
    ["Highest ADR", bestAdr?.adr ?? "—", bestAdr?.name ?? ""],
    ["Most entries", bestOpen?.open_kills ?? "—", bestOpen?.name ?? "no opening kills"],
    ["Kills per round", (totalKills / Math.max(d.rounds, 1)).toFixed(1), "both teams"],
  ];

  tiles.forEach(([label, value, sub]) => {
    const t = el("div", "tile");
    t.append(el("div", "tile-label", label));
    t.append(el("div", "tile-value", String(value)));
    if (sub) t.append(el("div", "tile-sub", sub));
    node.append(t);
  });
}

function renderTeams(node, d, teams) {
  const adrKey = state.adrMode === "raw" ? "adr_raw" : "adr";

  teams.forEach((team) => {
    const members = d.players
      .filter((p) => p.team === team)
      .sort((a, b) => b.kills - a.kills || b[adrKey] - a[adrKey]);
    if (!members.length) return;

    const head = el("div", "team-head");
    head.append(el("span", `team-dot ${team.toLowerCase()}`));
    head.append(el("span", null,
      `Team ${team} · started ${members[0].started_side || "?"}`));
    head.append(el("span", "team-wins", `${d.wins[team] ?? 0} rounds`));
    node.append(head);

    const table = el("table");
    table.innerHTML = `<thead><tr>
      <th>Player</th><th>K</th><th>D</th><th>A</th><th>+/−</th>
      <th>ADR</th><th>KAST</th><th>HS%</th><th>K/D</th><th>MVP</th>
      <th>Rounds</th></tr></thead>`;
    const tb = el("tbody");

    members.forEach((p) => {
      const tr = el("tr");

      const nameCell = el("td");
      const wrap = el("div", "pname");
      wrap.append(el("span", null, p.name));
      if (p.final_side) wrap.append(el("span", `pside ${p.final_side}`, p.final_side));
      if (p.left_early) {
        const f = el("span", "pflag", "left");
        f.title = "not present at the final round";
        wrap.append(f);
      }
      nameCell.append(wrap);
      tr.append(nameCell);

      const cell = (v, cls) => {
        const td = el("td", `num ${cls || ""}`.trim(), v);
        return td;
      };

      tr.append(cell(p.kills));
      tr.append(cell(p.deaths));
      tr.append(cell(p.assists));
      tr.append(cell((p.kills - p.deaths > 0 ? "+" : "") + (p.kills - p.deaths),
        p.kills - p.deaths > 0 ? "good" : p.kills - p.deaths < 0 ? "bad" : "dim"));
      tr.append(cell(fmt(p[adrKey], 1), band(p[adrKey], 65, 90)));
      tr.append(cell(`${p.kast}%`, band(p.kast, 60, 72)));
      tr.append(cell(`${p.hs_pct}%`, p.kills < 5 ? "dim" : band(p.hs_pct, 35, 50)));
      tr.append(cell(fmt(p.kd, 2), band(p.kd, 0.9, 1.15)));
      tr.append(cell(p.mvps || "—", p.mvps ? "" : "dim"));

      const shapeCell = el("td");
      shapeCell.append(shape(p.per_round));
      tr.append(shapeCell);

      tb.append(tr);
    });

    table.append(tb);
    node.append(table);
  });
}

function shape(perRound) {
  const wrap = el("div", "shape");
  const max = Math.max(1, ...perRound);
  perRound.forEach((n, i) => {
    const bar = el("i", n === 0 ? "z" : null);
    if (n > 0) bar.style.height = `${Math.max(3, (n / max) * 18)}px`;
    bar.title = `round ${i + 1}: ${n} kill${n === 1 ? "" : "s"}`;
    wrap.append(bar);
  });
  return wrap;
}

function renderDetail(node, d) {
  const table = el("table");
  table.innerHTML = `<thead><tr>
    <th>Player</th><th>Entry K</th><th>Entry D</th><th>Entry win</th>
    <th>Traded</th><th>Trades</th><th>2K</th><th>3K</th><th>4K</th><th>5K</th>
    <th>Util dmg</th><th>Flash asst</th><th>Dmg taken</th>
    <th>CT / T</th></tr></thead>`;
  const tb = el("tbody");

  [...d.players].sort((a, b) => b.kills - a.kills).forEach((p) => {
    const tr = el("tr");
    tr.append(el("td", null, p.name));
    const c = (v, cls) => tr.append(el("td", `num ${cls || ""}`.trim(), v));

    c(p.open_kills || "—", p.open_kills ? "" : "dim");
    c(p.open_deaths || "—", p.open_deaths ? "" : "dim");
    c(p.open_win_pct == null ? "—" : `${p.open_win_pct}%`,
      p.open_win_pct == null ? "dim" : band(p.open_win_pct, 50, 70));
    c(p.traded_pct == null ? "—" : `${p.traded_pct}%`,
      p.traded_pct == null ? "dim" : band(p.traded_pct, 20, 35));
    c(p.trade_kills || "—", p.trade_kills ? "" : "dim");
    c(p.multi["2"] || "—", p.multi["2"] ? "" : "dim");
    c(p.multi["3"] || "—", p.multi["3"] ? "" : "dim");
    c(p.multi["4"] || "—", p.multi["4"] ? "" : "dim");
    c(p.multi["5"] || "—", p.multi["5"] ? "" : "dim");
    c(p.util_dmg || "—", p.util_dmg ? "" : "dim");
    c(p.flash_assists || "—", p.flash_assists ? "" : "dim");
    c(p.dmg_taken);
    c(`${p.kills_ct} / ${p.kills_t}`);

    tb.append(tr);
  });

  table.append(tb);
  node.append(table);
}

function renderRounds(node, d, winner) {
  const grid = el("div", "roundgrid");
  d.round_log.forEach((r) => {
    const cell = el("div", "rcell");
    cell.append(el("div", "rcell-n", `Round ${r.round_number}`));
    const w = el("div", "rcell-w",
      r.winner_team ? `Team ${r.winner_team}` : (r.winner_side || "?"));
    w.classList.add(r.winner_team === winner ? "good" : "bad");
    cell.append(w);
    cell.append(el("div", "rcell-r",
      `${r.winner_side || "?"} · ${(r.reason || "").replace(/_/g, " ")}`));
    grid.append(cell);
  });
  node.append(grid);
}

/* ------------------------------------------------------------------ boot */

(async function init() {
  try {
    state.matches = await api("/api/matches");
  } catch (err) {
    $("#main").innerHTML = "";
    $("#main").append(el("div", "empty",
      `Could not reach the API: ${err.message}. Is serve.py running?`));
    return;
  }

  if (!state.matches.length) {
    $("#main").innerHTML = "";
    $("#main").append(el("div", "empty",
      "No matches in the database yet. Run spike/extract.py to add some."));
    return;
  }

  renderSidebar();
  select(state.matches[0].match_id);
})();
