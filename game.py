"""
game.py
-------
Authoritative state for one Sudoku game. All rules that must not be bypassable
(mistakes, hint allowance, pausing, scoring) live here, on the server, so a
page reload or a hand-written request cannot refund them.

STATE MODEL
    playing --pause--> paused --resume--> playing
    playing --last cell correct-------------> won
    playing --mistake limit reached---------> lost
    playing --give up-----------------------> gave_up
    won / lost / gave_up are final.

    A paused game accepts no puzzle actions (moves, undo, hints, reset, check,
    give up). Only `resume` leaves the paused state - nothing else ever does.

MISTAKE MODEL
    A mistake is a digit placed in a cell that differs from the solution.
    Each distinct (cell, digit) pair is charged once per game:
      * placing a wrong digit costs 1 mistake the first time;
      * selecting cells, pressing buttons, erasing, undoing or re-typing the
        same wrong digit in the same cell never costs anything again;
      * changing a wrong digit to another wrong digit costs 1 (new pair);
      * replacing a wrong digit with the right one is free - and mistakes are
        never refunded (not by erase, undo, reset or correcting).

HINT MODEL
    Each difficulty has a finite allowance. One request = one hint, and
    repeated requests on the same target escalate: nudge -> explain -> reveal.
    A reveal therefore costs 3 hints. Requests carry a sequence number, so a
    double-click or replayed request is charged once.

LEARN MODE (the tutor)
    `mode` is "play" or "learn". Switching it changes nothing else: the board,
    clock, mistakes, hints and pause state are untouched, and the clock keeps
    running (only an explicit pause stops it). The server refuses tutor
    requests in play mode, so the tutor cannot be reached by a hand-made call.

    The tutor may speak freely about what the board already shows and about
    general technique. Anything that says more about an unsolved cell goes
    through hint(), so Learn mode shares the one hint allowance and cannot be
    used to get around it. Advice never writes to the board.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

import hints as hintlib
import sudoku_logic as sl
import tutor

Cell = Tuple[int, int]

MODES = ("play", "learn")
MAX_TUTOR_LOG = 80          # messages kept per game

PLAYING = "playing"
PAUSED = "paused"
WON = "won"
LOST = "lost"
GAVE_UP = "gave_up"
FINAL = {WON, LOST, GAVE_UP}


@dataclass(frozen=True)
class Rules:
    max_mistakes: int
    max_hints: int
    par_seconds: int        # finishing within par costs no time penalty
    multiplier: float       # score multiplier for the level


RULES: Dict[str, Rules] = {
    "Easy":   Rules(max_mistakes=5, max_hints=5, par_seconds=480,  multiplier=1.0),
    "Medium": Rules(max_mistakes=3, max_hints=4, par_seconds=900,  multiplier=1.5),
    "Hard":   Rules(max_mistakes=3, max_hints=3, par_seconds=1500, multiplier=2.0),
}


class GameError(Exception):
    """A rejected action. `code` is machine readable, `message` is for people."""

    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def as_int(value, what: str = "value") -> int:
    """Accept only whole numbers. Booleans, fractions, strings and null are
    rejected (a bare int() would turn 0.5 into 0 and true into 1)."""
    if isinstance(value, bool):
        raise GameError("bad_request", f"Invalid {what}.", 400)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise GameError("bad_request", f"Invalid {what}.", 400)


class Game:
    def __init__(self, puzzle: sl.Board, solution: sl.Board, difficulty: str, *,
                 clock: Callable[[], float] = time.monotonic,
                 game_id: Optional[str] = None, attempt: int = 1,
                 rules: Optional[Rules] = None, mode: str = "play"):
        if difficulty not in RULES:
            raise ValueError(f"Unknown difficulty: {difficulty!r}")
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode!r}")
        self.id = game_id or uuid.uuid4().hex
        self.puzzle = copy.deepcopy(puzzle)
        self.solution = copy.deepcopy(solution)
        self.difficulty = difficulty
        self.rules = rules or RULES[difficulty]
        self.attempt = attempt

        self.entries: sl.Board = [[0] * 9 for _ in range(9)]   # player's digits
        self.revealed: Dict[Cell, int] = {}                    # level-3 hints
        self.mistakes = 0
        self._ledger: Set[Tuple[int, int, int]] = set()        # charged (r,c,d)
        self.hints_used = 0
        self.hint_seq = 0
        self._active_hint: Optional[Tuple[hintlib.Hint, int]] = None
        self._undo: List[Tuple[int, int, int, tutor.MoveRecord]] = []

        # Tutor state. `moves` is recorded in every mode (it is only ever shown
        # when the player asks), so a review can cover moves made before Learn
        # mode was switched on.
        self.mode = mode
        self.learn_used = mode == "learn"
        self.moves: List[tutor.MoveRecord] = []
        self._tutor_log: List[dict] = []
        self._tutor_seq = 0
        self._explained: Set[str] = set()
        self.tutor_questions = 0
        self.llm_calls = 0
        self._llm_busy = False

        self.status = PLAYING
        self.pause_reason: Optional[str] = None
        self.summary: Optional[dict] = None
        self.rev = 0                                           # bumps on change

        self._clock = clock
        self._lock = threading.RLock()
        self._base = 0.0                      # seconds accumulated before _started
        self._started: Optional[float] = clock()   # timing starts when puzzle appears

    # ------------------------------------------------------------------ #
    # Time
    # ------------------------------------------------------------------ #
    def _elapsed(self) -> float:
        if self._started is None:
            return self._base
        return self._base + (self._clock() - self._started)

    def elapsed(self) -> float:
        with self._lock:
            return self._elapsed()

    def _stop_clock(self) -> None:
        self._base = self._elapsed()
        self._started = None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _bump(self) -> None:
        self.rev += 1

    def _require_active(self) -> None:
        if self.status == PAUSED:
            raise GameError("paused", "The game is paused. Press Resume to continue.")
        if self.status in FINAL:
            raise GameError("finished", "This game is over. Start a new game.")

    @staticmethod
    def _cell(row, col) -> Cell:
        r, c = as_int(row, "cell"), as_int(col, "cell")
        if not (0 <= r < 9 and 0 <= c < 9):
            raise GameError("bad_request", "Cell out of range.", 400)
        return r, c

    def _value_at(self, r: int, c: int) -> int:
        if self.puzzle[r][c]:
            return self.puzzle[r][c]
        if (r, c) in self.revealed:
            return self.revealed[(r, c)]
        return self.entries[r][c]

    def _is_open(self, cell: Cell) -> bool:
        r, c = cell
        return self._value_at(r, c) == 0

    def _display_board(self) -> sl.Board:
        if self.status == GAVE_UP:
            return copy.deepcopy(self.solution)
        return [[self._value_at(r, c) for c in range(9)] for r in range(9)]

    def _known_board(self) -> sl.Board:
        """Clues + revealed hints + *correct* entries only (wrong ones ignored)."""
        board = [[0] * 9 for _ in range(9)]
        for r in range(9):
            for c in range(9):
                v = self._value_at(r, c)
                board[r][c] = v if v == self.solution[r][c] else 0
        return board

    def _is_solved(self) -> bool:
        return all(self._value_at(r, c) == self.solution[r][c]
                   for r in range(9) for c in range(9))

    def wrong_cells(self) -> List[Cell]:
        return [(r, c) for r in range(9) for c in range(9)
                if self.entries[r][c] and self.entries[r][c] != self.solution[r][c]
                and not self.puzzle[r][c] and (r, c) not in self.revealed]

    def _finish(self, status: str) -> None:
        self._stop_clock()
        self.status = status
        self.pause_reason = None
        self._active_hint = None
        if status == WON:
            self.summary = self._summarise()

    def _summarise(self) -> dict:
        t = self._elapsed()
        r = self.rules
        raw = 1000 - 100 * self.mistakes - 60 * self.hints_used \
            - max(0.0, t - r.par_seconds) / 3
        raw = max(0.0, raw)
        return {
            "time": round(t, 1),
            "par": r.par_seconds,
            "mistakes": self.mistakes,
            "max_mistakes": r.max_mistakes,
            "hints_used": self.hints_used,
            "max_hints": r.max_hints,
            "score": round(raw * r.multiplier),
            "stars": 3 if raw >= 850 else 2 if raw >= 600 else 1,
            "flawless": self.mistakes == 0 and self.hints_used == 0,
            "learn_used": self.learn_used,
            "tutor_questions": self.tutor_questions,
        }

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def move(self, row, col, value) -> dict:
        """Place (1-9) or erase (0) a digit in an editable cell."""
        with self._lock:
            self._require_active()
            r, c = self._cell(row, col)
            v = as_int(value, "digit")
            if not 0 <= v <= 9:
                raise GameError("bad_request", "Digits 1-9 only.", 400)
            if self.puzzle[r][c]:
                raise GameError("locked", "Given clues can't be changed.")
            if (r, c) in self.revealed:
                raise GameError("locked", "That digit was revealed by a hint.")

            prev = self.entries[r][c]
            event = {"row": r, "col": c, "value": v}
            if prev == v:
                event["type"] = "noop"
                return event

            # What the board looked like before this move: the tutor classifies
            # the move against it, not against the board the move creates.
            shown_before = self._player_board()
            known_before = self._known_board()
            wrong_before = set(self.wrong_cells())

            self.entries[r][c] = v
            if self._active_hint and (self._active_hint[1] == hintlib.MAX_LEVEL
                                      or self._active_hint[0].cell == (r, c)):
                self._active_hint = None

            if v == 0:
                event["type"] = "erase"
            elif v == self.solution[r][c]:
                event["type"] = "correct"
            else:
                key = (r, c, v)
                if key in self._ledger:
                    event["type"] = "repeat_mistake"       # already charged
                else:
                    self._ledger.add(key)
                    self.mistakes += 1
                    event["type"] = "mistake"

            rec = tutor.analyse_move(len(self.moves) + 1, (r, c), v, prev,
                                     "erase" if event["type"] == "erase" else event["type"],
                                     shown_before, known_before, wrong_before)
            self.moves.append(rec)
            self._undo.append((r, c, prev, rec))

            self._bump()
            if self.mistakes >= self.rules.max_mistakes:
                self._finish(LOST)
            elif self._is_solved():
                self._finish(WON)
            event["mistakes"] = self.mistakes
            event["status"] = self.status

            if self.mode == "learn":                  # coaching on the move just made
                if self.status in (WON, LOST):
                    self._log_tutor(tutor.review_reply(self._view(), self.status))
                else:
                    msg = tutor.react(self._view(), rec)
                    if msg:
                        self._log_tutor(msg)
            return event

    def undo(self) -> dict:
        """Revert the last entry. Never refunds mistakes or hints."""
        with self._lock:
            self._require_active()
            if not self._undo:
                raise GameError("nothing_to_undo", "Nothing to undo.")
            r, c, prev, rec = self._undo.pop()
            rec.undone = True                       # stays in the log: a mistake was still made
            self.entries[r][c] = prev
            if self._active_hint and self._active_hint[1] == hintlib.MAX_LEVEL:
                self._active_hint = None
            self._bump()
            return {"type": "undo", "row": r, "col": c, "value": prev}

    def reset(self) -> dict:
        """Clear the player's entries. Mistakes, hints used, revealed hints and
        elapsed time are kept - otherwise Reset would be a free refund."""
        with self._lock:
            self._require_active()
            cleared = sum(1 for row in self.entries for v in row if v)
            self.entries = [[0] * 9 for _ in range(9)]
            self._undo.clear()
            self._active_hint = None
            self._bump()
            return {"type": "reset", "cleared": cleared}

    def hint(self, seq: Optional[int] = None, prefer=None) -> dict:
        """Spend one hint: nudge, then explain, then reveal the same target."""
        with self._lock:
            self._require_active()
            if seq is not None and seq != self.hint_seq:
                return {"type": "stale"}           # duplicate request: no charge
            if self.hints_used >= self.rules.max_hints:
                raise GameError("no_hints", "No hints left for this puzzle.")

            prefer_cell: Optional[Cell] = None
            if prefer is not None:
                try:
                    prefer_cell = self._cell(prefer[0], prefer[1])
                except (TypeError, IndexError, KeyError, GameError):
                    prefer_cell = None             # a bad hint of focus is ignored

            active = self._active_hint
            if (active and active[1] < hintlib.MAX_LEVEL
                    and self._is_open(active[0].cell)):
                hint, level = active[0], active[1] + 1
            else:
                open_cells = [(r, c) for r in range(9) for c in range(9)
                              if self._is_open((r, c))]
                hint = hintlib.find_hint(self._known_board(), self.solution,
                                         open_cells, prefer_cell)
                if hint is None:
                    raise GameError(
                        "no_target",
                        "Every empty cell already has an entry. "
                        "Fix the cells marked in red first.")
                level = 1

            self.hints_used += 1
            self.hint_seq += 1
            self._active_hint = (hint, level)
            if level == hintlib.MAX_LEVEL:
                r, c = hint.cell
                self.revealed[hint.cell] = self.solution[r][c]
            self._bump()
            if level == hintlib.MAX_LEVEL and self._is_solved():
                self._finish(WON)
            if self.mode == "learn":                 # the same words, echoed in the chat
                self._log_tutor(tutor.hint_reply(hint, level, self.rules.max_hints - self.hints_used))
                if self.status == WON:
                    self._log_tutor(tutor.review_reply(self._view(), WON))
            return {"type": "hint", "level": level, "technique": hint.technique,
                    "status": self.status}

    def pause(self, reason: str = "manual") -> bool:
        """Freeze the clock. Returns True if the state changed."""
        with self._lock:
            if self.status != PLAYING:
                return False
            self._stop_clock()
            self.status = PAUSED
            self.pause_reason = reason if reason in ("manual", "away") else "manual"
            self._bump()
            return True

    def resume(self) -> bool:
        """Restart the clock. The only way out of the paused state."""
        with self._lock:
            if self.status != PAUSED:
                return False
            self._started = self._clock()
            self.status = PLAYING
            self.pause_reason = None
            self._bump()
            return True

    def check(self) -> dict:
        """Report progress. Reveals nothing the board does not already show."""
        with self._lock:
            self._require_active()
            wrong = len(self.wrong_cells())
            empty = sum(1 for r in range(9) for c in range(9)
                        if self._value_at(r, c) == 0)
            if wrong:
                word = "entry is" if wrong == 1 else "entries are"
                return {"type": "check", "status": "wrong", "wrong": wrong,
                        "empty": empty,
                        "message": f"{wrong} {word} wrong (marked in red)."}
            word = "cell" if empty == 1 else "cells"
            return {"type": "check", "status": "ok", "wrong": 0, "empty": empty,
                    "message": f"No mistakes so far. {empty} {word} to go."}

    def give_up(self) -> dict:
        """End the game and reveal the solution. Scores nothing."""
        with self._lock:
            self._require_active()
            self._bump()
            self._finish(GAVE_UP)
            return {"type": "give_up", "status": self.status}

    # ------------------------------------------------------------------ #
    # Learn mode: the tutor
    # ------------------------------------------------------------------ #
    def _player_board(self) -> sl.Board:
        """The board as the player sees it, wrong entries included."""
        return [[self._value_at(r, c) for c in range(9)] for r in range(9)]

    def _view(self, selected: Optional[Cell] = None) -> tutor.View:
        return tutor.View(
            puzzle=self.puzzle, shown=self._player_board(), known=self._known_board(),
            wrong=set(self.wrong_cells()), revealed=set(self.revealed), selected=selected,
            difficulty=self.difficulty, status=self.status, mistakes=self.mistakes,
            max_mistakes=self.rules.max_mistakes,
            hints_left=self.rules.max_hints - self.hints_used,
            moves=self.moves, explained=self._explained)

    def _log_tutor(self, msg: dict, role: str = "tutor") -> dict:
        self._tutor_seq += 1
        entry = dict(msg)
        entry.update(id=self._tutor_seq, role=role)
        entry.setdefault("source", "rules")
        self._tutor_log.append(entry)
        del self._tutor_log[:-MAX_TUTOR_LOG]
        self._bump()
        return copy.deepcopy(entry)

    def _intro(self) -> dict:
        return tutor.reply(
            "Hi, I'm your Sudoku tutor. I read your actual board, so I can explain a cell, say "
            "why a digit doesn't fit, list what a row is missing or teach a technique. I react "
            "to mistakes and the odd lucky guess, but I stay quiet when you're doing fine. "
            "Your clock keeps running while we chat; pause it if you need a break. I never "
            "fill cells for you, and a nudge towards a good spot uses one of your hints.",
            "info", chips=tutor.default_chips())

    def set_mode(self, mode) -> dict:
        """Switch between play and learn. Touches nothing but the mode."""
        with self._lock:
            if mode not in MODES:
                raise GameError("bad_request", "Mode must be play or learn.", 400)
            changed = mode != self.mode
            self.mode = mode
            if mode == "learn":
                self.learn_used = True
                if not self._tutor_log:
                    self._log_tutor(self._intro())
            if changed:
                self._bump()
            return {"type": "mode", "mode": self.mode, "changed": changed}

    def selected_cell(self, selected) -> Optional[Cell]:
        """Parse a [row, col] the page says is selected; anything odd means none."""
        if selected is None:
            return None
        try:
            return self._cell(selected[0], selected[1])
        except (TypeError, IndexError, KeyError, GameError):
            return None

    def tutor_messages(self, after: Optional[int] = None) -> List[dict]:
        """Messages with an id greater than `after` (None -> none)."""
        with self._lock:
            if after is None:
                return []
            return [copy.deepcopy(m) for m in self._tutor_log if m["id"] > after]

    def tutor_clear(self) -> dict:
        """Start a fresh conversation. The puzzle and its counters are untouched."""
        with self._lock:
            self._tutor_log.clear()
            self._explained.clear()
            if self.mode == "learn":
                self._log_tutor(self._intro())
            self._bump()
            return {"type": "tutor_clear"}

    def tutor_ask(self, text=None, intent=None, params=None, selected=None,
                  seq=None, label=None) -> dict:
        """Answer a question typed (`text`) or chosen (`intent`) by the player.

        Free intents are answered from the board and cost nothing. `next_move`
        is a real hint: it runs through hint(), so it spends the shared
        allowance, escalates and is idempotent exactly like the Hint button.
        """
        with self._lock:
            if self.mode != "learn":
                raise GameError("play_mode", "Switch to Learn mode to ask the tutor.")
            if self.status == PAUSED:
                raise GameError("paused", "The game is paused. Press Resume to continue.")

            sel = self.selected_cell(selected)

            shown = None
            if intent is None:
                if not isinstance(text, str) or not text.strip():
                    raise GameError("bad_request", "Type a question first.", 400)
                shown = " ".join(text.split())
                if len(shown) > tutor.MAX_QUESTION_CHARS:
                    raise GameError("bad_request",
                                    f"Keep questions under {tutor.MAX_QUESTION_CHARS} characters.", 400)
                intent, params = tutor.route(shown)
            else:
                if intent not in tutor.CLIENT_INTENTS:
                    raise GameError("bad_request", "Unknown tutor request.", 400)
                try:
                    params = tutor.clean_params(intent, params)
                except ValueError:
                    raise GameError("bad_request", "Invalid tutor request.", 400)
                shown = label.strip()[:120] if isinstance(label, str) and label.strip() else intent
            if (intent == "next_move" and seq is not None and seq != self.hint_seq
                    and self.status not in FINAL):
                # A duplicate of a request already handled: no charge, no new chat line.
                return {"type": "tutor", "intent": intent, "cost": 0, "stale": True,
                        "reply": None}
            self.tutor_questions += 1
            self._log_tutor({"text": shown, "kind": "user"}, role="user")

            if self.status in FINAL and intent not in tutor.AFTER_GAME_INTENTS | {"refuse_solve", "unknown"}:
                msg = tutor.reply("This game is over. I can review how it went or teach a "
                                  "technique, or you can start a new game.", "info",
                                  chips=[tutor.chip("Review my moves", "review"),
                                         tutor.chip("Teach me a technique", "technique")])
            elif intent == "next_move":
                try:
                    self.hint(seq=seq, prefer=sel)            # spends a hint, logs its own reply
                    return {"type": "tutor", "intent": intent, "cost": 1,
                            "reply": copy.deepcopy(self._tutor_log[-1])}
                except GameError as exc:
                    if exc.code == "no_hints":
                        msg = tutor.no_hints_reply()
                    elif exc.code == "no_target":
                        msg = tutor.no_target_reply()
                    else:
                        raise
            else:
                msg = tutor.answer(intent, params, self._view(sel))
            logged = self._log_tutor(msg)
            return {"type": "tutor", "intent": intent, "cost": 0, "reply": logged}

    # -- optional language-model wording (see tutor_llm.py) --
    def llm_acquire(self, max_calls: int) -> bool:
        with self._lock:
            if self._llm_busy or self.llm_calls >= max_calls:
                return False
            self._llm_busy = True
            self.llm_calls += 1
            return True

    def llm_release(self) -> None:
        with self._lock:
            self._llm_busy = False

    def tutor_annotate(self, msg_id: int, **fields) -> Optional[dict]:
        """Update a logged tutor message (reworded text, an error flag)."""
        with self._lock:
            for m in self._tutor_log:
                if m["id"] == msg_id and m.get("role") == "tutor":
                    m.update(fields)
                    self._bump()
                    return copy.deepcopy(m)
            return None

    def tutor_open_facts(self, selected) -> dict:
        """The only facts an open-ended AI answer may use. Deliberately not the
        board: no solution, no candidates, nothing that could solve a cell."""
        with self._lock:
            selected = self.selected_cell(selected)
            facts = {"difficulty": self.difficulty, "mistakes": self.mistakes,
                     "max_mistakes": self.rules.max_mistakes, "status": self.status}
            if selected is not None:
                known = self._known_board()
                r, c = selected
                facts["selected_cell"] = tutor.cell_label(selected)
                facts["selected_cell_is_empty"] = self._value_at(r, c) == 0
                facts["digits_already_in_its_units"] = {
                    hintlib.unit_label(u): sorted(tutor.digits_in(known, u))
                    for u in tutor.units_of(selected)}
            return facts

    # ------------------------------------------------------------------ #
    # What the browser is allowed to see
    # ------------------------------------------------------------------ #
    def _hint_view(self) -> Optional[dict]:
        if not self._active_hint:
            return None
        hint, level = self._active_hint
        view = {
            "level": level,
            "name": hintlib.LEVEL_NAMES[level],
            "technique": hint.technique,
            "text": hintlib.describe(hint, level),
            "unit": {"label": hintlib.unit_label(hint.unit),
                     "cells": [list(x) for x in hintlib.unit_cells(hint.unit)]},
        }
        if level >= 2:                       # the target is only named from level 2
            view["cell"] = list(hint.cell)
        return view

    def public_state(self) -> dict:
        with self._lock:
            board = self._display_board()
            wrong = [] if self.status == GAVE_UP else [list(x) for x in self.wrong_cells()]
            auto_solved = []
            if self.status == GAVE_UP:
                auto_solved = [[r, c] for r in range(9) for c in range(9)
                               if not self.puzzle[r][c] and (r, c) not in self.revealed
                               and self.entries[r][c] != self.solution[r][c]]
            r = self.rules
            return {
                "game_id": self.id,
                "rev": self.rev,
                "difficulty": self.difficulty,
                "attempt": self.attempt,
                "status": self.status,
                "pause_reason": self.pause_reason,
                "puzzle": self.puzzle,
                "board": board,
                "revealed": [list(x) for x in self.revealed],
                "wrong": wrong,
                "conflicts": [list(x) for x in sl.find_conflicts(board)],
                "auto_solved": auto_solved,
                "mistakes": self.mistakes,
                "max_mistakes": r.max_mistakes,
                "hints_used": self.hints_used,
                "max_hints": r.max_hints,
                "hints_left": r.max_hints - self.hints_used,
                "hint_seq": self.hint_seq,
                "active_hint": self._hint_view(),
                "mode": self.mode,
                "learn_used": self.learn_used,
                "tutor_last": self._tutor_seq,
                "elapsed": round(self._elapsed(), 3),
                "running": self.status == PLAYING,
                "can_undo": bool(self._undo) and self.status == PLAYING,
                "has_progress": bool(self._undo) or bool(self.revealed)
                                or self.mistakes > 0 or self.hints_used > 0,
                "summary": self.summary,
            }


class GameStore:
    """Thread-safe in-memory store that forgets the oldest games when full."""

    def __init__(self, max_games: int = 500):
        self._games: "OrderedDict[str, Game]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_games = max_games

    def add(self, game: Game) -> Game:
        with self._lock:
            self._games[game.id] = game
            self._games.move_to_end(game.id)
            while len(self._games) > self.max_games:
                self._games.popitem(last=False)
        return game

    def get(self, game_id) -> Optional[Game]:
        with self._lock:
            game = self._games.get(game_id) if isinstance(game_id, str) else None
            if game:
                self._games.move_to_end(game_id)
            return game

    def __len__(self) -> int:
        return len(self._games)
