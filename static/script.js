"use strict";

/* ==========================================================================
   Sudoku front end.

   The server owns the game (rules, mistakes, hints, clock). This page keeps
   one object, `S`, holding the latest state the server sent, and draws it.
   Nothing here can refund a mistake or a hint: it can only ask the server.
   ========================================================================== */

/* ---------- Small helpers ---------- */
const $ = (id) => document.getElementById(id);

const store = {                       // localStorage can be blocked: never trust it
  get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* ignore */ } },
};

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  Object.entries(props || {}).forEach(([k, v]) => node.setAttribute(k, v));
  node.append(...kids);
  return node;
}

const keySet = (cells) => new Set((cells || []).map(([r, c]) => r + "," + c));

function fmtTime(total) {
  const s = Math.max(0, Math.floor(total));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const two = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${two(m)}:${two(sec)}` : `${two(m)}:${two(sec)}`;
}

/* ---------- State ---------- */
let S = null;                         // latest server state: the single source of truth
let syncedAt = performance.now();     // when S arrived, for smooth local timing
let sel = null;                       // selected cell [row, col]
let hintBusy = false;                 // a hint request is in flight
let announced = "";                   // "<game>:<status>" whose end dialog was shown
let shownGame = "";                   // game whose tally is on screen
let prevMistakes = 0;
let chain = Promise.resolve();        // requests go out strictly in order

/* Tutor (Learn mode). The server keeps the conversation; the page keeps a copy and
   asks only for messages newer than the last id it has seen. */
let tutorLog = [];                    // messages in arrival order (server ones carry an id)
let tutorAfter = 0;                   // id of the newest server message we have
let tutorBusy = false;                // a question is in flight
let tutorOpen = false;                // drawer state (only used on screens narrower than 1240px)
let unread = 0;                       // tutor messages the player has not had a chance to see
let aiOn = false;                     // does the server have an AI model connected?
let tutorCells = new Set();           // cells the tutor is talking about, drawn on the board
let quizDone = {};                    // message id -> option the player chose
let renderedIds = [];                 // ids already drawn in the log
let lastActivity = performance.now(); // for the one-time "stuck?" suggestion
let stuckShown = false;
let localSeq = 0;
const WIDE = window.matchMedia("(min-width: 1240px)");
const STUCK_AFTER_MS = Number(window.STUCK_AFTER_MS) > 0 ? Number(window.STUCK_AFTER_MS) : 120000;
const AFTER_GAME = new Set(["review", "technique", "lesson", "lesson_answer", "help"]);
const ACTIVITY_PATHS = ["/api/move", "/api/undo", "/api/hint", "/api/tutor", "/api/new",
                        "/api/reset", "/api/resume", "/api/mode"];
const DEFAULT_CHIPS = [
  { label: "Help me find my next move (uses a hint)", intent: "next_move", params: {} },
  { label: "Explain this cell", intent: "explain_cell", params: {} },
  { label: "Teach me a technique", intent: "technique", params: {} },
];

const ui = {
  board: $("board"), frame: $("frame"), timer: $("timer"), level: $("level"),
  mistakesText: $("mistakes-text"), mistakeBoxes: $("mistake-boxes"), mistakeTally: $("mistake-tally"),
  hintsText: $("hints-text"), hintDots: $("hint-dots"),
  pause: $("pause-overlay"), pauseNote: $("pause-note"), resume: $("resume"),
  note: $("hint-note"), noteTitle: $("hint-title"), noteText: $("hint-text"),
  status: $("status"), numpad: $("numpad"),
  undo: $("undo"), erase: $("erase"), hint: $("hint"), pauseBtn: $("pause"),
  check: $("check"), reset: $("reset"), solve: $("solve"), newBtn: $("new"),
  difficulty: $("difficulty"), theme: $("theme"), dialog: $("dialog"),
  modeBtns: [...document.querySelectorAll("#mode .seg-btn")],
  tutor: $("tutor"), tutorLog: $("tutor-log"), tutorChips: $("tutor-chips"),
  tutorForm: $("tutor-form"), tutorInput: $("tutor-input"), tutorSend: $("tutor-send"),
  tutorFoot: $("tutor-foot"), tutorClear: $("tutor-clear"), tutorClose: $("tutor-close"),
  tutorOpenBtn: $("tutor-open"), badge: $("tutor-badge"),
};

const cells = [];                     // cells[r][c] -> element
const numButtons = {};                // digit -> button

/* ---------- Talking to the server ---------- */
const OFFLINE = { ok: false, status: 0,
  json: { error: "Cannot reach the game server. Is it still running?", code: "offline" } };

async function call(path, body, opts) {
  const { method = "POST", keepalive = false } = opts || {};
  if (ACTIVITY_PATHS.includes(path)) { lastActivity = performance.now(); stuckShown = false; }
  if (path === "/api/move" || path === "/api/undo") tutorCells = new Set();   // old pointers go stale
  let res;
  try {
    res = await fetch(path, {
      method, keepalive,
      headers: { "Content-Type": "application/json" },
      body: method === "GET" ? undefined : JSON.stringify({ tutor_after: tutorAfter, ...(body || {}) }),
    });
  } catch (e) {
    return OFFLINE;
  }
  const json = await res.json().catch(() => ({}));
  if (typeof json.ai === "boolean") aiOn = json.ai;
  ingestTutor(json.tutor);
  if (json.state) applyState(json.state); else renderTutor();
  return { ok: res.ok, status: res.status, json };
}

function send(path, body, opts) {     // queued: a fast typist's moves stay in order
  const run = () => call(path, body, opts);
  const p = chain.then(run, run);
  chain = p;
  return p;
}

function applyState(next) {
  if (S && S.game_id === next.game_id && next.rev < S.rev) return;   // stale reply
  S = next;
  syncedAt = performance.now();
  render();
  afterState();
}

/* ---------- Derived facts ---------- */
const canPlay = () => !!S && S.status === "playing";
const isOver = () => !!S && !["playing", "paused"].includes(S.status);
const isRevealed = (r, c) => S.revealed.some(([a, b]) => a === r && b === c);

function currentElapsed() {
  if (!S) return 0;
  return S.status === "playing"
    ? S.elapsed + (performance.now() - syncedAt) / 1000
    : S.elapsed;
}

/* ---------- Build the static parts once ---------- */
function buildBoard() {
  for (let r = 0; r < 9; r++) {
    const row = el("div", { class: "row", role: "row" });
    cells[r] = [];
    for (let c = 0; c < 9; c++) {
      const cell = el("div", { class: "cell", role: "gridcell", "data-r": r, "data-c": c, tabindex: -1 });
      cells[r][c] = cell;
      row.append(cell);
    }
    ui.board.append(row);
  }
}

function buildNumpad() {
  for (let d = 1; d <= 9; d++) {
    const button = el("button", { class: "num", type: "button", "data-d": d }, String(d));
    button.addEventListener("click", () => place(d));
    numButtons[d] = button;
    ui.numpad.append(button);
  }
}

/* ---------- Rendering ---------- */
function render() {
  if (!S) return;
  const enteringPause = S.status === "paused" && ui.pause.hidden;
  renderMode();
  renderBoard();
  renderHud();
  renderControls();
  renderHintNote();
  renderPause();
  renderTimer();
  renderTutor();
  if (enteringPause) ui.resume.focus();
}

function renderBoard() {
  const wrong = keySet(S.wrong), conflicts = keySet(S.conflicts);
  const revealed = keySet(S.revealed), auto = keySet(S.auto_solved);
  const hint = S.status === "playing" ? S.active_hint : null;
  const region = hint ? keySet(hint.unit.cells) : new Set();
  const target = hint && hint.cell ? hint.cell.join(",") : null;
  const selValue = sel ? S.board[sel[0]][sel[1]] : 0;

  const peers = new Set();
  if (sel) {
    const [sr, sc] = sel, br = Math.floor(sr / 3) * 3, bc = Math.floor(sc / 3) * 3;
    for (let i = 0; i < 9; i++) { peers.add(sr + "," + i); peers.add(i + "," + sc); }
    for (let r = br; r < br + 3; r++) for (let c = bc; c < bc + 3; c++) peers.add(r + "," + c);
  }

  for (let r = 0; r < 9; r++) {
    for (let c = 0; c < 9; c++) {
      const k = r + "," + c, v = S.board[r][c], clue = S.puzzle[r][c] !== 0;
      const isWrong = wrong.has(k), isSel = !!sel && sel[0] === r && sel[1] === c;
      const cls = ["cell"];
      if (clue) cls.push("clue");
      else if (revealed.has(k)) cls.push("revealed");
      else if (auto.has(k)) cls.push("auto");
      if (peers.has(k)) cls.push(clue ? "cpeer" : "peer");
      if (selValue && v === selValue && !isWrong) cls.push("same");
      if (region.has(k)) cls.push("region");
      if (isWrong) cls.push("wrong");
      if (conflicts.has(k) && !isWrong) cls.push("conflict");
      if (k === target) cls.push("target");
      if (isSel) cls.push("selected");
      if (S.mode === "learn" && tutorCells.has(k)) cls.push("tutor-cell");

      const node = cells[r][c];
      node.className = cls.join(" ");
      node.textContent = v ? String(v) : "";
      node.tabIndex = (sel ? isSel : (r === 0 && c === 0)) ? 0 : -1;

      let label = `Row ${r + 1}, column ${c + 1}, `;
      if (!v) label += "empty";
      else if (clue) label += `${v}, printed clue`;
      else if (revealed.has(k)) label += `${v}, revealed by a hint`;
      else if (isWrong) label += `${v}, incorrect`;
      else label += String(v);
      node.setAttribute("aria-label", label);
    }
  }
  ui.board.classList.toggle("locked", !canPlay());
}

function renderHud() {
  ui.level.textContent = S.difficulty + (S.attempt > 1 ? `, attempt ${S.attempt}` : "");

  // Mistakes: boxes that get struck through.
  const newGame = shownGame !== S.game_id;
  if (ui.mistakeBoxes.children.length !== S.max_mistakes || newGame) {
    ui.mistakeBoxes.replaceChildren(...Array.from({ length: S.max_mistakes }, () => el("span", { class: "box" })));
  }
  [...ui.mistakeBoxes.children].forEach((box, i) => {
    box.classList.toggle("struck", i < S.mistakes);
    box.classList.toggle("fresh", !newGame && i < S.mistakes && i >= prevMistakes);
  });
  prevMistakes = S.mistakes;
  shownGame = S.game_id;
  ui.mistakesText.textContent = `${S.mistakes}/${S.max_mistakes}`;

  // Hints: a dot per hint, filled while it is still available.
  if (ui.hintDots.children.length !== S.max_hints) {
    ui.hintDots.replaceChildren(...Array.from({ length: S.max_hints }, () => el("span", { class: "dot" })));
  }
  [...ui.hintDots.children].forEach((dot, i) => dot.classList.toggle("used", i >= S.hints_left));
  ui.hintsText.textContent = `${S.hints_left} left`;

  const danger = S.mistakes >= S.max_mistakes - 1;
  ui.frame.classList.toggle("danger", danger);
  ui.mistakeTally.classList.toggle("danger", danger);
}

function renderControls() {
  const active = canPlay(), over = isOver();

  // Number pad: how many of each digit are still missing (wrong entries do not count).
  const wrong = keySet(S.wrong), placed = Array(10).fill(0);
  for (let r = 0; r < 9; r++) for (let c = 0; c < 9; c++) {
    const v = S.board[r][c];
    if (v && !wrong.has(r + "," + c)) placed[v]++;
  }
  for (let d = 1; d <= 9; d++) {
    const left = 9 - placed[d];
    numButtons[d].disabled = !active || left === 0;
    numButtons[d].setAttribute("aria-label", `${d}, ${left} left to place`);
  }

  ui.undo.disabled = !S.can_undo;
  ui.erase.disabled = !active;
  ui.check.disabled = !active;
  ui.reset.disabled = !active;
  ui.solve.disabled = !active;
  ui.pauseBtn.disabled = over;
  ui.pauseBtn.textContent = S.status === "paused" ? "Resume" : "Pause";

  const level = S.active_hint ? S.active_hint.level : 0;
  const verb = level === 1 ? "Explain" : level === 2 ? "Reveal" : "Hint";
  const meaning = level === 1 ? "show which cell and why"
                : level === 2 ? "reveal the digit"
                : "get a nudge towards a good spot";
  ui.hint.replaceChildren(el("span", { class: "hint-verb" }, verb),
                          el("span", { class: "hint-count" }, `${S.hints_left} left`));
  ui.hint.disabled = !active || S.hints_left <= 0 || hintBusy;
  const aria = S.hints_left <= 0 ? "No hints left"
    : `${verb}: ${meaning}. Uses 1 of your ${S.hints_left} remaining hints.`;
  ui.hint.setAttribute("aria-label", aria);
  ui.hint.title = aria;
}

function renderHintNote() {
  const hint = S.status === "playing" ? S.active_hint : null;
  ui.note.hidden = !hint;
  if (!hint) return;
  const titles = { 1: "Nudge: where to look", 2: "Explanation: why this cell", 3: "Revealed" };
  ui.noteTitle.textContent = titles[hint.level];
  ui.noteText.textContent = hint.text;
}

function renderPause() {
  const paused = S.status === "paused";
  ui.pause.hidden = !paused;
  ui.board.inert = paused;
  ui.pauseNote.textContent = S.pause_reason === "away"
    ? "You switched away, so the clock stopped. Your game is safe."
    : "The clock is stopped and the board is hidden.";
}

function renderTimer() {
  const text = fmtTime(currentElapsed());
  if (ui.timer.textContent !== text) ui.timer.textContent = text;
}

function say(text, kind) {
  ui.status.textContent = text;
  ui.status.className = "status" + (kind && kind !== "info" ? " " + kind : "");
}

/* ---------- Selecting cells ---------- */
function selectCell(r, c, focus) {
  sel = [r, c];
  renderBoard();
  if (focus !== false) cells[r][c].focus();
}

function moveSelection(dr, dc) {
  const [r, c] = sel || [0, 0];
  selectCell(Math.min(8, Math.max(0, r + dr)), Math.min(8, Math.max(0, c + dc)));
}

/* ---------- Playing ---------- */
function place(digit) {
  if (!canPlay()) return;
  if (!sel) { say("Select a cell first.", "warn"); return; }
  const [r, c] = sel;
  if (S.puzzle[r][c]) { say("Printed clues cannot be changed.", "warn"); return; }
  if (isRevealed(r, c)) { say("That digit was revealed by a hint and is locked.", "warn"); return; }
  if (S.board[r][c] === digit) return;                    // nothing would change
  sendMove(r, c, digit);
}

function erase() {
  if (!canPlay()) return;
  if (!sel) { say("Select a cell first.", "warn"); return; }
  const [r, c] = sel;
  if (S.puzzle[r][c] || isRevealed(r, c)) { say("That cell is locked.", "warn"); return; }
  if (!S.board[r][c]) return;
  sendMove(r, c, 0);
}

async function sendMove(r, c, value) {
  const res = await send("/api/move", { game_id: S.game_id, row: r, col: c, value });
  if (!res.ok) {
    if (!["paused", "finished"].includes(res.json.code)) say(res.json.error, "warn");
    return;
  }
  const ev = res.json.event, where = `R${r + 1}C${c + 1}`;
  if (ev.type === "mistake") {
    shake(r, c);
    const left = S.max_mistakes - S.mistakes;
    if (S.status === "lost") say(`${value} doesn't belong in ${where}. That was your last mistake.`, "err");
    else say(`${value} doesn't belong in ${where}. ${left === 1 ? "One mistake left." : left + " mistakes left."}`, "err");
  } else if (ev.type === "repeat_mistake") {
    shake(r, c);
    say(`${value} still doesn't belong in ${where}. This one was already counted.`, "warn");
  } else if (S.status === "won") {
    say("Solved!", "ok");
  } else {
    say("", "info");
  }
}

function shake(r, c) {
  const node = cells[r][c];
  node.classList.remove("shake");
  void node.offsetWidth;                                   // restart the animation
  node.classList.add("shake");
  node.addEventListener("animationend", () => node.classList.remove("shake"), { once: true });
}

async function undo() {
  if (!S || !S.can_undo) return;
  const res = await send("/api/undo", { game_id: S.game_id });
  if (!res.ok && !["paused", "finished"].includes(res.json.code)) say(res.json.error, "warn");
}

async function requestHint() {
  if (!canPlay() || hintBusy) return;
  if (S.hints_left <= 0) { say("No hints left for this puzzle.", "warn"); return; }
  hintBusy = true;
  renderControls();
  const res = await send("/api/hint", { game_id: S.game_id, seq: S.hint_seq, selected: sel });
  hintBusy = false;
  if (S) renderControls();
  if (!res.ok) {
    if (!["paused", "finished"].includes(res.json.code)) say(res.json.error, "warn");
    return;
  }
  const ev = res.json.event;
  if (ev.type === "hint") say("", "info");     // the note under the board says it; the counters show the cost
  if (S.status === "won") say("Solved!", "ok");
}

/* ---------- Pause, resume, tab visibility ---------- */
function freezeLocally(reason) {      // stop the displayed clock this instant
  if (!S || S.status !== "playing") return;
  S = { ...S, status: "paused", running: false, elapsed: currentElapsed(), pause_reason: reason, can_undo: false };
  render();
}

function pauseGame(reason, opts) {
  if (!canPlay()) return Promise.resolve();
  freezeLocally(reason);
  say("Paused. The clock is stopped.", "info");
  return call("/api/pause", { game_id: S.game_id, reason }, opts);
}

function resumeGame() {
  if (!S || S.status !== "paused") return;
  send("/api/resume", { game_id: S.game_id }).then(() => {
    if (canPlay()) say("Resumed. The clock is running.", "info");
    if (canPlay() && sel) cells[sel[0]][sel[1]].focus();
  });
}

function togglePause() {
  if (canPlay()) pauseGame("manual");
  else if (S && S.status === "paused") resumeGame();
}

/* Switching tabs or minimising pauses the game; coming back never resumes it.
   Only the Resume button does. Losing focus alone (clicking into devtools or
   another window) does nothing: only "hidden" reliably means the player left. */
document.addEventListener("visibilitychange", () => {
  if (document.hidden && canPlay()) pauseGame("away", { keepalive: true });
});
window.addEventListener("pagehide", () => {
  if (canPlay() && navigator.sendBeacon) {
    navigator.sendBeacon("/api/pause",
      new Blob([JSON.stringify({ game_id: S.game_id, reason: "away" })], { type: "application/json" }));
  }
});

/* ---------- Dialogs ---------- */
function ask({ title, body, actions }) {
  const dlg = ui.dialog;
  if (dlg.open) dlg.close();
  return new Promise((resolve) => {
    $("dlg-title").textContent = title;
    $("dlg-body").replaceChildren(...(Array.isArray(body) ? body : [el("p", {}, body)]));
    $("dlg-actions").replaceChildren(...actions.map((a) => {
      const b = el("button", { class: "btn" + (a.primary ? " primary" : ""), type: "button" }, a.label);
      b.addEventListener("click", () => dlg.close(a.value));
      return b;
    }));
    dlg.returnValue = "";
    dlg.addEventListener("close", () => resolve(dlg.returnValue || null), { once: true });
    dlg.showModal();
  });
}

function bestScores() {
  try { return JSON.parse(store.get("sudoku-best") || "{}"); } catch (e) { return {}; }
}

function facts(rows) {
  const dl = el("dl", { class: "facts" });
  rows.forEach(([k, v]) => dl.append(el("dt", {}, k), el("dd", {}, v)));
  return dl;
}

async function showWin() {
  const s = S.summary, best = bestScores();
  // Games that used the tutor are scored apart: they are not comparable with unaided ones.
  const bestKey = S.difficulty + (s.learn_used ? " (Learn)" : ""), previous = best[bestKey] || 0;
  const isBest = s.score > previous;
  if (isBest) { best[bestKey] = s.score; store.set("sudoku-best", JSON.stringify(best)); }
  const stars = el("div", { class: "stars", role: "img", "aria-label": `${s.stars} of 3 stars` },
    "★".repeat(s.stars) + "☆".repeat(3 - s.stars));
  const lead = s.flawless ? "No mistakes and no hints." : "Nicely done.";
  const choice = await ask({
    title: "Puzzle solved",
    body: [stars, el("p", {}, lead), facts([
      ["Time", `${fmtTime(s.time)} (par ${fmtTime(s.par)})`],
      ["Mistakes", `${s.mistakes} of ${s.max_mistakes}`],
      ["Hints used", `${s.hints_used} of ${s.max_hints}`],
      ...(s.learn_used ? [["Tutor", `${s.tutor_questions} question${s.tutor_questions === 1 ? "" : "s"}`]] : []),
      ["Score", s.score.toLocaleString()],
      ["Best on " + bestKey, (isBest ? s.score : previous).toLocaleString() + (isBest ? " (new best)" : "")],
    ])],
    actions: [{ label: "Close", value: "close" }, { label: "New game", value: "new", primary: true }],
  });
  if (choice === "new") newGame();
}

async function showLost() {
  const choice = await ask({
    title: "Out of mistakes",
    body: [el("p", {}, `You made ${S.mistakes} mistakes. The wrong entries stay marked so you can see where it went wrong.`),
           facts([["Time", fmtTime(S.elapsed)], ["Hints used", `${S.hints_used} of ${S.max_hints}`]])],
    actions: [{ label: "Review board", value: "close" },
              { label: "Try this puzzle again", value: "retry" },
              { label: "New game", value: "new", primary: true }],
  });
  if (choice === "new") newGame();
  if (choice === "retry") newGame({ retry: true });
}

function afterState() {
  const key = S.game_id + ":" + S.status;
  if (announced === key) return;
  if (S.status === "won") { announced = key; showWin(); }
  else if (S.status === "lost") { announced = key; showLost(); }
  else if (S.status === "gave_up") { announced = key; say("Solution revealed. This game has no score.", "info"); }
}

/* ---------- Game-level actions ---------- */
async function newGame(opts) {
  const retry = !!(opts && opts.retry);
  if (!retry && S && ["playing", "paused"].includes(S.status) && S.has_progress) {
    const ok = await ask({
      title: "Start a new game?",
      body: "Your current game will be lost.",
      actions: [{ label: "Keep playing", value: "no" }, { label: "New game", value: "yes", primary: true }],
    });
    if (ok !== "yes") return;
  }
  const level = ui.difficulty.value;
  store.set("sudoku-level", level);
  say(retry ? "Setting up another attempt..." : `Generating a new ${level} puzzle...`, "info");
  const wantMode = S ? S.mode : (store.get("sudoku-mode") === "learn" ? "learn" : "play");
  resetTutor();
  const res = await send("/api/new", retry ? { retry_of: S.game_id } : { difficulty: level, mode: wantMode });
  if (!res.ok) { say(res.json.error || "Could not start a game.", "err"); return; }
  store.set("sudoku-game-id", S.game_id);
  sel = null;
  const clues = S.puzzle.flat().filter(Boolean).length;
  say(retry ? "Same puzzle, fresh counters."
            : `New ${S.difficulty} game. ${clues} clues, clock running.`, "info");
  renderBoard();
}

async function giveUp() {
  if (!canPlay()) return;
  const ok = await ask({
    title: "Give up and show the solution?",
    body: "The game ends and scores nothing.",
    actions: [{ label: "Keep playing", value: "no" }, { label: "Give up", value: "yes", primary: true }],
  });
  if (ok !== "yes") return;
  const res = await send("/api/solve", { game_id: S.game_id });
  if (!res.ok && !["paused", "finished"].includes(res.json.code)) say(res.json.error, "warn");
}

async function resetBoard() {
  if (!canPlay()) return;
  const hasEntries = S.board.some((row, r) => row.some((v, c) => v && !S.puzzle[r][c] && !isRevealed(r, c)));
  if (!hasEntries) { say("There is nothing to reset.", "info"); return; }
  const ok = await ask({
    title: "Reset the board?",
    body: "This clears your entries. Your mistakes, used hints and time stay as they are.",
    actions: [{ label: "Cancel", value: "no" }, { label: "Reset board", value: "yes", primary: true }],
  });
  if (ok !== "yes") return;
  const res = await send("/api/reset", { game_id: S.game_id });
  if (res.ok) say("Board cleared. Mistakes, hints and time were kept.", "info");
  else if (!["paused", "finished"].includes(res.json.code)) say(res.json.error, "warn");
}

async function checkProgress() {
  if (!canPlay()) return;
  const res = await send("/api/check", { game_id: S.game_id });
  if (res.ok) say(res.json.event.message, res.json.event.status === "ok" ? "ok" : "err");
  else if (!["paused", "finished"].includes(res.json.code)) say(res.json.error, "warn");
}

/* ---------- Learn mode and the tutor ---------- */
const viewingTutor = () => !!S && S.mode === "learn" && (WIDE.matches || tutorOpen);

function resetTutor() {
  tutorLog = []; tutorAfter = 0; tutorCells = new Set(); quizDone = {};
  renderedIds = []; unread = 0; stuckShown = false; lastActivity = performance.now();
  ui.tutorLog.replaceChildren();                     // the old conversation must leave the screen too
}

function ingestTutor(list) {
  if (!Array.isArray(list) || !list.length) return;
  let newest = null;
  for (const m of list) {
    const i = tutorLog.findIndex((x) => x.id === m.id);
    if (i >= 0) tutorLog[i] = m;
    else {
      tutorLog.push(m);
      if (m.role === "tutor" && !viewingTutor()) unread += 1;
    }
    if (m.id > tutorAfter) tutorAfter = m.id;
    if (m.role === "tutor") newest = m;
  }
  if (newest) tutorCells = keySet(newest.cells);     // point at what the newest reply is about
}

function addLocal(msg) {                              // page-only notes: errors and the stuck prompt
  tutorLog.push({ role: "tutor", kind: "info", id: "L" + (++localSeq), local: true, ...msg });
  if (!viewingTutor()) unread += 1;
  renderTutor();
}

function checkStuck() {
  if (!S || S.mode !== "learn" || S.status !== "playing" || stuckShown) return;
  if (performance.now() - lastActivity < STUCK_AFTER_MS) return;
  stuckShown = true;                                  // once, then quiet until the next move
  addLocal({ text: "Stuck for a while? I can point you at a good place to look. That uses one of your hints.",
             chips: [DEFAULT_CHIPS[0]] });
}

async function setMode(next) {
  if (!S || S.mode === next) return;
  const res = await send("/api/mode", { game_id: S.game_id, mode: next });
  if (!res.ok) { say(res.json.error || "Could not switch mode.", "warn"); return; }
  store.set("sudoku-mode", S.mode);
  if (S.mode === "learn") {
    if (!WIDE.matches) setDrawer(true);
    if (canPlay()) say("Learn mode on. The clock keeps running.", "info");
  } else {
    setDrawer(false);
    tutorCells = new Set();
    if (canPlay()) say("Play mode. No coaching.", "info");
  }
  render();
}

function setDrawer(open, returnFocus) {
  tutorOpen = open;
  if (open) unread = 0;
  render();
  if (open) ui.tutorLog.focus({ preventScroll: true });
  else if (returnFocus && !ui.tutorOpenBtn.hidden) ui.tutorOpenBtn.focus();
}

function renderMode() {
  const learn = S.mode === "learn";
  document.body.dataset.mode = S.mode;
  ui.modeBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === S.mode)));
  if (!learn) tutorOpen = false;
  ui.tutorOpenBtn.hidden = !learn || WIDE.matches;
  ui.tutorOpenBtn.setAttribute("aria-expanded", String(tutorOpen));
  const n = viewingTutor() ? 0 : unread;
  ui.badge.hidden = n === 0;
  ui.badge.textContent = n > 9 ? "9+" : String(n);
  ui.tutorOpenBtn.setAttribute("aria-label",
    n ? `Open the Sudoku Tutor, ${n} new message${n === 1 ? "" : "s"}` : "Open the Sudoku Tutor");
  if (viewingTutor()) unread = 0;
  ui.tutor.dataset.open = String(tutorOpen);
}

/* Cell names such as R3C5 in a reply become buttons that select that cell. */
function withRefs(text) {
  const out = [];
  let last = 0;
  String(text).replace(/R([1-9])C([1-9])/g, (hit, r, c, at) => {
    out.push(text.slice(last, at));
    const b = el("button", { class: "ref", type: "button", "aria-label": `Select row ${r}, column ${c}` }, hit);
    b.addEventListener("click", () => {
      if (S && S.status !== "paused") selectCell(+r - 1, +c - 1);
    });
    out.push(b);
    last = at + hit.length;
    return hit;
  });
  out.push(String(text).slice(last));
  return out;
}

function msgNode(m) {
  if (m.role === "user") {
    return el("div", { class: "msg user" }, el("div", { class: "bubble" }, el("p", {}, m.text)));
  }
  const cls = ["msg", "tutor-msg"];
  if (m.kind === "hint") cls.push("hint");
  if (m.local === "error") cls.push("error");
  const bubble = el("div", { class: "bubble" });
  if (m.title) bubble.append(el("p", { class: "m-title" }, m.title));
  bubble.append(el("p", {}, ...withRefs(m.text)));
  if (m.steps && m.steps.length) bubble.append(el("ol", {}, ...m.steps.map((t) => el("li", {}, ...withRefs(t)))));
  if (m.quiz) bubble.append(quizNode(m));
  const wrap = el("div", { class: cls.join(" ") }, bubble);
  const meta = [];
  if (m.kind === "hint") meta.push("Used 1 hint." + (m.note ? " " + m.note + "." : ""));
  if (m.source === "ai") meta.push("Worded by the AI model.");
  if (m.ai_error) meta.push("The AI model didn't answer, so this is the built-in answer.");
  if (meta.length) wrap.append(el("span", { class: "meta" }, meta.join(" ")));
  return wrap;
}

function quizNode(m) {
  const box = el("div", { class: "quiz", role: "group", "aria-label": "Quick check" },
                 el("p", { class: "q" }, m.quiz.question));
  m.quiz.options.forEach((opt, i) => {
    const b = el("button", { type: "button", "aria-pressed": "false", "data-i": i }, opt);
    b.addEventListener("click", () => {
      if (quizDone[m.id] !== undefined || tutorBusy) return;
      quizDone[m.id] = i;
      renderQuiz(box, m);
      askTutor({ intent: "lesson_answer", params: { name: m.quiz.lesson, choice: i }, label: opt });
    });
    box.append(b);
  });
  renderQuiz(box, m);
  return box;
}

function renderQuiz(box, m) {
  const chosen = quizDone[m.id];
  box.querySelectorAll("button").forEach((b) => {
    b.disabled = chosen !== undefined;
    b.setAttribute("aria-pressed", String(chosen === +b.dataset.i));
  });
}

function typingNode() {
  return el("div", { class: "msg tutor-msg typing-row", role: "status", "aria-label": "The tutor is thinking" },
            el("div", { class: "bubble typing" }, el("i"), el("i"), el("i")));
}

/* The newest reply's own follow-ups first, then the three standing questions
   (without repeating one the reply already offered). */
function currentChips() {
  const last = [...tutorLog].reverse().find((m) => m.role === "tutor");
  const own = last && last.chips ? last.chips : [];
  const key = (c) => c.intent + JSON.stringify(c.params || {});
  const seen = new Set(own.map(key));
  const seenIntent = new Set(own.map((c) => c.intent));
  return [...own, ...DEFAULT_CHIPS.filter((c) => !seen.has(key(c)) && !seenIntent.has(c.intent))];
}

function chipUsable(chip) {
  if (!S || tutorBusy || S.status === "paused") return false;
  if (isOver() && !AFTER_GAME.has(chip.intent)) return false;
  if (chip.intent === "next_move" && S.hints_left <= 0) return false;
  return true;
}

function renderTutor() {
  if (!S) return;
  const learn = S.mode === "learn";
  if (!learn) return;

  // Log: add only what is new, so a screen reader hears each message once.
  const log = ui.tutorLog;
  const ids = tutorLog.map((m) => String(m.id));
  const prefixOk = renderedIds.length <= ids.length && renderedIds.every((id, i) => id === ids[i]);
  if (!prefixOk) { log.replaceChildren(); renderedIds = []; }
  const before = log.scrollHeight - log.scrollTop - log.clientHeight;
  tutorLog.slice(renderedIds.length).forEach((m) => log.append(msgNode(m)));
  renderedIds = ids;
  log.querySelectorAll(".typing-row").forEach((n) => n.remove());
  if (tutorBusy) log.append(typingNode());
  log.setAttribute("aria-busy", String(tutorBusy));
  if (before < 80 || tutorBusy) log.scrollTop = log.scrollHeight;

  // Suggestion chips for the newest reply (or the three defaults).
  const hadFocus = ui.tutorChips.contains(document.activeElement);
  const chips = currentChips();
  const sig = chips.map((c) => c.label + (chipUsable(c) ? "+" : "-")).join("|");
  if (ui.tutorChips.dataset.sig !== sig) {
    ui.tutorChips.dataset.sig = sig;
    ui.tutorChips.replaceChildren(...chips.map((c) => {
      const b = el("button", { class: "chip", type: "button" }, c.label);
      b.disabled = !chipUsable(c);
      if (c.intent === "next_move" && S.hints_left <= 0) b.title = "No hints left";
      b.addEventListener("click", () => askTutor({
        intent: c.intent, params: c.params || {}, label: c.label.replace(/ \(uses a hint\)$/, ""),
      }));
      return b;
    }));
    if (hadFocus) ui.tutorLog.focus({ preventScroll: true });
  }

  const paused = S.status === "paused";
  ui.tutorInput.disabled = paused;
  ui.tutorSend.disabled = paused || tutorBusy;
  ui.tutorInput.placeholder = paused ? "Resume the game to chat"
    : isOver() ? "Ask for a review or a lesson" : "Ask about your board";
  ui.tutorFoot.textContent = aiOn
    ? "Hints, rule checks and board facts come from the game engine. An AI model only rewords them."
    : "Answers come from the game's own rules engine. No AI model is connected.";
}

async function askTutor(req) {
  if (!S || S.mode !== "learn" || tutorBusy) return;
  tutorBusy = true;
  renderTutor();
  const body = { game_id: S.game_id, selected: sel, ...req };
  if (req.intent === "next_move") body.seq = S.hint_seq;      // a replayed click is never charged twice
  const res = await send("/api/tutor", body);
  tutorBusy = false;
  if (!res.ok) {
    const text = res.json.code === "offline"
      ? "I couldn't reach the game server. Your puzzle is safe. Try again in a moment."
      : res.json.code === "paused" ? "The game is paused. Press Resume to carry on chatting."
      : (res.json.error || "Something went wrong. Please try again.");
    addLocal({ text, local: "error" });
  }
  renderTutor();
  if (res.ok && req.intent === "next_move" && S.status === "won") say("Solved!", "ok");
}

async function clearChat() {
  if (!S || S.mode !== "learn") return;
  const keep = S.game_id;
  resetTutor();
  renderBoard();
  await send("/api/tutor/clear", { game_id: keep });
  renderTutor();
}

/* ---------- Theme ---------- */
function applyTheme(theme, persist) {
  document.documentElement.setAttribute("data-theme", theme);
  const label = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
  ui.theme.setAttribute("aria-label", label);
  ui.theme.title = label;
  if (persist) store.set("sudoku-theme", theme);
}

/* ---------- Wiring ---------- */
function wire() {
  ui.board.addEventListener("click", (e) => {
    const cell = e.target.closest(".cell");
    if (cell && canPlay()) selectCell(+cell.dataset.r, +cell.dataset.c);
    else if (cell && S && !isOver()) return;
    else if (cell) selectCell(+cell.dataset.r, +cell.dataset.c);   // review a finished board
  });
  ui.board.addEventListener("focusin", (e) => {
    const cell = e.target.closest(".cell");
    if (!cell) return;
    const r = +cell.dataset.r, c = +cell.dataset.c;
    if (!sel || sel[0] !== r || sel[1] !== c) selectCell(r, c, false);
  });

  ui.undo.addEventListener("click", undo);
  ui.erase.addEventListener("click", erase);
  ui.hint.addEventListener("click", requestHint);
  ui.pauseBtn.addEventListener("click", togglePause);
  ui.resume.addEventListener("click", resumeGame);
  ui.newBtn.addEventListener("click", () => newGame());
  ui.check.addEventListener("click", checkProgress);
  ui.reset.addEventListener("click", resetBoard);
  ui.solve.addEventListener("click", giveUp);
  ui.difficulty.addEventListener("change", () => store.set("sudoku-level", ui.difficulty.value));

  ui.modeBtns.forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  ui.tutorOpenBtn.addEventListener("click", () => setDrawer(!tutorOpen, true));
  ui.tutorClose.addEventListener("click", () => setDrawer(false, true));
  ui.tutorClear.addEventListener("click", clearChat);
  ui.tutorForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = ui.tutorInput.value.trim();
    if (!text || tutorBusy) return;
    ui.tutorInput.value = "";
    askTutor({ text });
  });
  ui.tutor.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !WIDE.matches && tutorOpen) { e.stopPropagation(); setDrawer(false, true); }
  });
  WIDE.addEventListener("change", () => { unread = 0; if (S) render(); });

  ui.theme.addEventListener("click", () =>
    applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark", true));
  if (window.matchMedia) {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
      if (!store.get("sudoku-theme")) applyTheme(e.matches ? "dark" : "light", false);
    });
  }

  document.addEventListener("keydown", (e) => {
    if (ui.dialog.open || !S) return;
    if (e.target.closest && e.target.closest("select, input, textarea, #tutor")) return;
    if (e.ctrlKey || e.metaKey) {
      if (e.key.toLowerCase() === "z") { e.preventDefault(); undo(); }
      return;
    }
    if (e.altKey) return;
    const arrows = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (/^[1-9]$/.test(e.key)) { e.preventDefault(); place(+e.key); }
    else if (e.key === "Backspace" || e.key === "Delete" || e.key === "0") { e.preventDefault(); erase(); }
    else if (arrows[e.key] && !ui.board.inert) { e.preventDefault(); moveSelection(...arrows[e.key]); }
    else if (e.key === "Escape" && sel && !ui.board.inert) { sel = null; renderBoard(); }
  });
}

async function boot() {
  buildBoard();
  buildNumpad();
  wire();
  applyTheme(document.documentElement.getAttribute("data-theme") || "light", false);
  const savedLevel = store.get("sudoku-level");
  if (["Easy", "Medium", "Hard"].includes(savedLevel)) ui.difficulty.value = savedLevel;

  setInterval(() => { renderTimer(); checkStuck(); }, 250);   // one ticker for the life of the page

  const saved = store.get("sudoku-game-id");
  if (saved) {                         // a reload must not be a loophole: resume the same game
    const res = await call("/api/state?tutor_after=0&game_id=" + encodeURIComponent(saved), null, { method: "GET" });
    if (res.ok) {
      announced = S.game_id + ":" + S.status;
      ui.difficulty.value = S.difficulty;
      if (canPlay()) await pauseGame("away");
      say(S.status === "paused" ? "Welcome back. Press Resume when you are ready." : "Welcome back.", "info");
      return;
    }
  }
  await newGame();
}

boot();
