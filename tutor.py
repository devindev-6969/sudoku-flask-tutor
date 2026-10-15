"""
tutor.py
--------
The Sudoku tutor's brain. It is deterministic: every statement it makes is
computed from the real game state by the same engine that validates moves, so it
cannot invent a deduction or contradict the board. (An optional language model
may later *reword* a reply - see tutor_llm.py - but never decides what is true.)

THE ASSISTANCE RULE
    The tutor may say, for free, anything the board already shows (digits that
    are placed, conflicts, which entries are marked wrong) and anything that is
    general theory (lessons, strategy). Anything that would tell the player
    more about an UNSOLVED cell - which cell to look at, why it is forced, what
    goes in it - is a hint, and spends one from the same allowance as the Hint
    button. So Learn mode can explain a lot but cannot become a way around the
    hint limit. (Game.tutor_ask routes those requests through Game.hint.)

Nothing here touches the clock, the mistake ledger or the allowance. Functions
take a read-only `View` and return plain dicts.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import hints as hintlib
import lessons
import sudoku_logic as sl

Cell = Tuple[int, int]

NAKED = hintlib.NAKED
HIDDEN = hintlib.HIDDEN

# Intents a client may name directly (chips and follow-ups).
CLIENT_INTENTS = {"next_move", "explain_cell", "why_not", "missing", "why_wrong",
                  "better", "overview", "technique", "lesson", "lesson_answer",
                  "review", "help"}
# Still available once the game has ended: looking back and learning.
AFTER_GAME_INTENTS = {"review", "technique", "lesson", "lesson_answer", "help"}

MAX_QUESTION_CHARS = 300


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def fmt_digits(digits) -> str:
    ds = sorted(digits)
    return ", ".join(str(d) for d in ds) if ds else "nothing yet"


def fmt_list(items) -> str:
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def cell_label(cell: Cell) -> str:
    return hintlib.cell_label(cell)


def units_of(cell: Cell) -> List[int]:
    r, c = cell
    return [r, 9 + c, 18 + (r // 3) * 3 + c // 3]


def unit_kind(u: int) -> str:
    return "row" if u < 9 else "column" if u < 18 else "box"


def shared_unit(a: Cell, b: Cell) -> int:
    ub = set(units_of(b))
    return next(u for u in units_of(a) if u in ub)


def digits_in(board: sl.Board, u: int) -> Set[int]:
    return {board[r][c] for r, c in sl.UNITS[u] if board[r][c]}


def reply(text: str, kind: str = "info", **extra) -> dict:
    out = {"text": text, "kind": kind, "cost": 0}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def chip(label: str, intent: str, **params) -> dict:
    return {"label": label, "intent": intent, "params": params}


NEXT_MOVE_CHIP = chip("Help me find my next move (uses a hint)", "next_move")


def lesson_chip(name: str, prefix: str = "Teach me") -> dict:
    return chip(f"{prefix}: {lessons.LESSONS[name]['title'].lower()}", "lesson", name=name)


# Matches things like R3C5, Row 3, Box 2, 4 of 9, so they are not mistaken for
# digits when checking a sentence for a leaked answer.
_LABELS = re.compile(r"\bR[1-9]C[1-9]\b|\b(?:Row|Column|Box)\s+[1-9]\b|\b\d+\s+of\s+\d+\b",
                     re.IGNORECASE)


def mentions_digit(text: str, digit: int) -> bool:
    """True if `digit` appears as a stand-alone number once cell, unit and
    count labels are ignored. Used to prove a reply does not name an answer."""
    cleaned = _LABELS.sub(" ", text)
    return re.search(rf"(?<![\w.]){digit}(?![\w.])", cleaned) is not None


# --------------------------------------------------------------------------- #
# What happened on each move
# --------------------------------------------------------------------------- #
@dataclass
class MoveRecord:
    n: int
    cell: Cell
    digit: int                 # 0 = erase
    prev: int
    kind: str                  # correct | mistake | repeat_mistake | erase
    forced: Optional[str] = None            # naked_single | hidden_single | None
    forced_unit: Optional[int] = None       # unit that made it a hidden single
    cause: Optional[str] = None             # conflict | cascade | guess  (wrong moves)
    clashes: Tuple[Cell, ...] = ()
    clash_units: Tuple[int, ...] = ()
    undone: bool = False


def forced_technique(known: sl.Board, cell: Cell, digit: int
                     ) -> Tuple[Optional[str], Optional[int]]:
    """Was `digit` in `cell` forced by singles on the trusted board `known`
    (the cell itself empty)? Returns (technique, unit) or (None, None)."""
    masks = sl.candidate_masks(known)
    mask = masks.get(cell)
    if mask is None:
        return None, None
    if mask == 1 << (digit - 1):
        best = min(units_of(cell), key=lambda u: (_empties(known, u), u))
        return NAKED, best
    hidden_units = []
    for u in units_of(cell):
        if digit in digits_in(known, u):
            continue
        spots = [x for x in sl.UNITS[u] if x in masks and masks[x] >> (digit - 1) & 1]
        if spots == [cell]:
            hidden_units.append(u)
    if hidden_units:
        return HIDDEN, min(hidden_units, key=lambda u: (_empties(known, u), u))
    return None, None


def _empties(board: sl.Board, u: int) -> int:
    return sum(1 for r, c in sl.UNITS[u] if board[r][c] == 0)


def find_clashes(shown: sl.Board, cell: Cell, digit: int) -> Tuple[List[Cell], List[int]]:
    """Cells in the same row, column or box that already hold `digit`."""
    clashes: List[Cell] = []
    for u in units_of(cell):
        for other in sl.UNITS[u]:
            if other != cell and shown[other[0]][other[1]] == digit and other not in clashes:
                clashes.append(other)
    return clashes, [shared_unit(cell, o) for o in clashes]


def analyse_move(n: int, cell: Cell, digit: int, prev: int, kind: str,
                 shown_before: sl.Board, known_before: sl.Board,
                 wrong_before: Set[Cell]) -> MoveRecord:
    """Classify one entry using the board as it was *before* the move."""
    rec = MoveRecord(n, cell, digit, prev, kind)
    if kind in ("mistake", "repeat_mistake"):
        clashes, units = find_clashes(shown_before, cell, digit)
        rec.clashes, rec.clash_units = tuple(clashes), tuple(units)
        if not clashes:
            rec.cause = "guess"
        elif all(c in wrong_before for c in clashes):
            rec.cause = "cascade"
        else:
            rec.cause = "conflict"
    elif kind == "correct":
        rec.forced, rec.forced_unit = forced_technique(known_before, cell, digit)
    return rec


# --------------------------------------------------------------------------- #
# What the tutor can see
# --------------------------------------------------------------------------- #
@dataclass
class View:
    puzzle: sl.Board
    shown: sl.Board             # the board as the player sees it (wrong entries included)
    known: sl.Board             # clues + correct entries + hint reveals
    wrong: Set[Cell]
    revealed: Set[Cell]
    selected: Optional[Cell]
    difficulty: str
    status: str
    mistakes: int
    max_mistakes: int
    hints_left: int
    moves: List[MoveRecord] = field(default_factory=list)
    explained: Set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# Explaining a wrong move
# --------------------------------------------------------------------------- #
def clash_sentence(digit: int, clashes, units, puzzle: sl.Board) -> str:
    parts = []
    for other, u in list(zip(clashes, units))[:2]:
        clue = " (a printed clue)" if puzzle[other[0]][other[1]] else ""
        parts.append(f"{hintlib.unit_label(u)} already has a {digit} at "
                     f"{cell_label(other)}{clue}")
    return " and ".join(parts) + "."


def mistake_reply(rec: MoveRecord, puzzle: sl.Board, first_tip: bool = False,
                  tip_pattern: bool = False) -> dict:
    d, where = rec.digit, cell_label(rec.cell)
    if rec.cause == "conflict":
        text = f"Not quite. {d} can't go in {where}: " + \
               clash_sentence(d, rec.clashes, rec.clash_units, puzzle)
        if first_tip:
            text += " Tip: before typing a digit, glance along its row, column and box."
        cells = [list(rec.cell)] + [list(c) for c in rec.clashes[:2]]
        chips = [lesson_chip("scanning")] if first_tip else None
        return reply(text, "feedback", cells=cells, chips=chips)
    if rec.cause == "cascade":
        other = cell_label(rec.clashes[0])
        text = (f"Not quite. {d} clashes with your own wrong entry at {other}. "
                f"Fix the red cell first, then come back to {where}.")
        return reply(text, "feedback", cells=[list(rec.cell), list(rec.clashes[0])])
    text = (f"Not quite. {d} doesn't clash with anything on the board, but it isn't "
            f"the right digit for {where}. A digit that merely fits is not proof. "
            f"Look for a digit that is forced: the only one that fits the cell, or "
            f"the only place for it in a row, column or box.")
    return reply(text, "feedback", cells=[list(rec.cell)],
                 chips=[lesson_chip("naked_single"), lesson_chip("hidden_single")])


# --------------------------------------------------------------------------- #
# Reacting to a move (Learn mode only)
# --------------------------------------------------------------------------- #
def react(view: View, rec: MoveRecord) -> Optional[dict]:
    """Coaching for the move just made, or None when silence is better.

    Never repeats itself: an unchanged mistake (already charged) gets nothing,
    each technique is praised once, and nudges about guessing are rationed.
    """
    if rec.kind in ("erase", "repeat_mistake"):
        return None
    if rec.kind == "mistake":
        first_tip = rec.cause == "conflict" and "tip_scan" not in view.explained
        if first_tip:
            view.explained.add("tip_scan")
        out = mistake_reply(rec, view.puzzle, first_tip)
        pattern = _pattern_note(view)
        if pattern:
            out["text"] += "\n" + pattern[0]
            out["chips"] = (out.get("chips") or []) + [lesson_chip(pattern[1])]
        return out

    # a correct entry
    where, d = cell_label(rec.cell), rec.digit
    if rec.forced == NAKED and "first_naked" not in view.explained:
        view.explained.add("first_naked")
        return reply(f"Good. {where} = {d} was a naked single: its row, column and box "
                     f"already contain every other digit, so only {d} fits.",
                     "feedback", cells=[list(rec.cell)],
                     chips=[lesson_chip("naked_single", "More on")])
    if rec.forced == HIDDEN and "first_hidden" not in view.explained:
        view.explained.add("first_hidden")
        return reply(f"Good. {where} = {d} was a hidden single: in "
                     f"{hintlib.unit_label(rec.forced_unit)}, {d} had nowhere else to go.",
                     "feedback", cells=[list(rec.cell)],
                     chips=[lesson_chip("hidden_single", "More on")])
    if rec.forced is None:
        unforced = sum(1 for m in view.moves
                       if m.kind == "correct" and m.forced is None and not m.undone)
        if unforced == 1 or unforced % 3 == 0:
            return reply(
                f"{d} is correct for {where}, but it wasn't a single: with what was on the "
                f"board, more than one digit could have fit. If that was a guess, it "
                f"worked this time, but guessing gets expensive on harder puzzles. "
                f"Try to find a cell where only one digit fits.",
                "feedback", cells=[list(rec.cell)],
                chips=[lesson_chip("scanning")])
    return None


def _pattern_note(view: View) -> Optional[Tuple[str, str]]:
    """A one-off remark when the same kind of mistake keeps recurring."""
    mistakes = [m for m in view.moves if m.kind == "mistake"]
    conflicts = [m for m in mistakes if m.cause == "conflict"]
    guesses = [m for m in mistakes if m.cause == "guess"]
    for group, label in ((conflicts, "conflict"), (guesses, "guess")):
        n = len(group)
        key = f"pattern_{label}_{n}"
        if n in (2, 4, 6) and key not in view.explained:
            view.explained.add(key)
            if label == "conflict":
                kinds = Counter(unit_kind(m.clash_units[0]) for m in group)
                kind, count = kinds.most_common(1)[0]
                where = f"the same {kind}" if count == n else "its row, column or box"
                return (f"Pattern: {n} of your {len(mistakes)} mistakes reused a digit "
                        f"already in {where}. Scanning for the digit before you type "
                        f"would catch these.", "scanning")
            return (f"Pattern: {n} of your {len(mistakes)} mistakes were guesses that "
                    f"didn't clash with anything. Forced cells are safer than guesses.",
                    "hidden_single")
    return None


# --------------------------------------------------------------------------- #
# Hints, in the tutor's voice (same text as the Hint button: one source of truth)
# --------------------------------------------------------------------------- #
def hint_reply(hint: hintlib.Hint, level: int, hints_left: int) -> dict:
    name = hintlib.LEVEL_NAMES[level]
    text = hintlib.describe(hint, level)
    chips: List[dict] = []
    if level == 1:
        chips.append(chip("Explain why (uses a hint)", "next_move"))
    elif level == 2:
        chips.append(chip("Reveal the digit (uses a hint)", "next_move"))
    lesson = lessons.FOR_TECHNIQUE.get(hint.technique)
    if lesson and level >= 2:
        chips.append(lesson_chip(lesson))
    if hint.technique == hintlib.NARROW and level == 1:
        chips.append(lesson_chip("naked_pair", "About"))
    cells = [list(hint.cell)] if level >= 2 else None
    return reply(text, "hint", title=f"Hint: {name.lower()}", cost=1, level=level,
                 technique=hint.technique, cells=cells, chips=chips or None,
                 note=f"{hints_left} hint{'s' if hints_left != 1 else ''} left")


def no_hints_reply() -> dict:
    return reply("You've used every hint for this puzzle. I can still teach techniques, "
                 "list what a row, column or box is missing, or review your moves.",
                 "info", chips=[chip("What am I missing in this row?", "missing"),
                                lesson_chip("scanning"), chip("Review my moves", "review")])


def no_target_reply() -> dict:
    return reply("Every empty cell already has an entry, so there is nothing to hint at. "
                 "Fix the cells marked in red first.", "info")


# --------------------------------------------------------------------------- #
# Understanding a typed question
# --------------------------------------------------------------------------- #
_UNIT_WORDS = r"(row|column|col|box|region|block|square)"


def route(text: str) -> Tuple[str, dict]:
    """Turn a sentence into (intent, params). Simple on purpose: predictable
    keyword rules beat a model for deciding what is allowed to be answered."""
    low = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "

    if re.search(r"\b(give|tell|show|reveal|fill|solve|complete|finish)\b.{0,25}"
                 r"\b(answer|answers|solution|everything|whole|entire|all of it|the puzzle|"
                 r"it for me|for me)\b", low) and "without" not in low:
        return "refuse_solve", {}

    m = re.search(r"\b(?:why|how come)\b.{0,20}\b(?:can'?t|cant|cannot|can not|isn'?t|"
                  r"is not|won'?t|not)\b.{0,40}?\b([1-9])\b", low) \
        or re.search(r"\bwhy\b.{0,15}\b([1-9])\b.{0,15}\b(?:not allowed|illegal|invalid|"
                     r"doesn'?t (?:fit|work)|not (?:fit|work|ok|okay|valid))\b", low) \
        or re.search(r"\bcan i (?:put|place|enter|use|write)\b.{0,10}\b([1-9])\b", low) \
        or re.search(r"\b(?:put|place|enter)\b.{0,10}\b([1-9])\b.{0,20}\b(?:here|this cell)\b", low)
    if m:
        return "why_not", {"digit": int(m.group(1))}

    if re.search(r"\b(why|what)\b.{0,30}\b(wrong|incorrect|mistake|error|doesn'?t belong|"
                 r"not (?:right|correct))\b", low) or "what did i do wrong" in low:
        return "why_wrong", {}

    if re.search(r"\b(missing|left in|remaining|still need|what.{0,12}(?:in|about) "
                 r"(?:this|the|row|column|col|box))", low):
        unit = _parse_unit(low)
        return "missing", ({"unit": unit} if unit is not None else {})

    name = lessons.find_lesson(low)
    if name:
        return "lesson", {"name": name}
    if re.search(r"\b(technique|techniques|teach|lesson|tutorial|learn|strateg(?:y|ies) "
                 r"i should)\b", low):
        return "technique", {}

    if re.search(r"\b(review|how am i doing|how'?m i doing|my moves|my progress|summary|"
                 r"feedback on|assess)\b", low):
        return "review", {}

    if re.search(r"\b(next move|next step|what should i do|what do i do|where (?:do|should|"
                 r"can) i (?:start|look|go)|i'?m stuck|im stuck|stuck|hint|nudge|help me find)\b",
                 low):
        return "next_move", {}

    if re.search(r"\b(explain|tell me about|what about|analy[sz]e|candidates?|options)\b"
                 r".{0,25}\b(this|the|that|selected)?\s*(cell|square|box|spot)\b", low) \
            or re.search(r"\bthis cell\b", low):
        return "explain_cell", {}

    if re.search(r"\b(better|improve|get better|tips?|advice|faster|smarter|approach|"
                 r"method)\b", low):
        return "better", {}

    if re.search(r"\b(overview|walk me|how (?:do|to|should) i (?:solve|play|approach)|"
                 r"solve this|without (?:revealing|spoil|giving)|strateg(?:y|ies))\b", low):
        return "overview", {}

    if re.search(r"^\s*(hi|hello|hey|help|\?|what can you do|who are you)\W*\s*$", low):
        return "help", {}
    return "unknown", {}


def _parse_unit(low: str) -> Optional[int]:
    m = re.search(rf"\b{_UNIT_WORDS}\s*(?:number\s*)?([1-9])\b", low)
    if not m:
        return None
    kind, n = m.group(1), int(m.group(2)) - 1
    if kind == "row":
        return n
    if kind in ("column", "col"):
        return 9 + n
    return 18 + n


def clean_params(intent: str, params) -> dict:
    """Validate parameters a client sent along with a named intent."""
    params = params if isinstance(params, dict) else {}
    out: dict = {}
    if intent == "why_not":
        d = params.get("digit")
        if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 9:
            raise ValueError("digit")
        out["digit"] = d
    elif intent == "missing":
        u = params.get("unit")
        if u is not None:
            if isinstance(u, bool) or not isinstance(u, int) or not 0 <= u <= 26:
                raise ValueError("unit")
            out["unit"] = u
    elif intent in ("lesson", "lesson_answer"):
        name = params.get("name")
        if name not in lessons.LESSONS:
            raise ValueError("lesson")
        out["name"] = name
        if intent == "lesson_answer":
            choice = params.get("choice")
            n_opts = len(lessons.LESSONS[name]["quiz"]["options"])
            if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < n_opts:
                raise ValueError("choice")
            out["choice"] = choice
    return out


# --------------------------------------------------------------------------- #
# Answering (free intents)
# --------------------------------------------------------------------------- #
def answer(intent: str, params: dict, view: View) -> dict:
    handler = {
        "explain_cell": _explain_cell, "why_not": _why_not, "missing": _missing,
        "why_wrong": _why_wrong, "better": _better, "overview": _overview,
        "technique": _technique_menu, "lesson": _lesson, "lesson_answer": _lesson_answer,
        "review": _review, "help": _help, "refuse_solve": _refuse, "unknown": _unknown,
    }.get(intent, _unknown)
    return handler(params, view)


def _help(params, view) -> dict:
    return reply("I'm your Sudoku tutor. I read your actual board, so I can explain a cell, "
                 "say why a digit doesn't fit, list what a row is missing, teach a technique, "
                 "or review your moves. I won't fill cells for you; if you want a nudge "
                 "towards a good spot, that uses one of your hints.", "info",
                 chips=default_chips())


def default_chips() -> List[dict]:
    return [NEXT_MOVE_CHIP, chip("Explain this cell", "explain_cell"),
            chip("What am I missing in this row?", "missing"),
            chip("Teach me a technique", "technique"),
            chip("Review my moves", "review")]


def _unknown(params, view) -> dict:
    return reply("I'm not sure what you're asking. I can explain a cell, say why a digit "
                 "doesn't fit, list what a row, column or box is missing, teach a technique, "
                 "or review your moves. Pick one below or rephrase.", "info",
                 chips=default_chips())


def _refuse(params, view) -> dict:
    left = view.hints_left
    return reply("I won't fill the board for you, because that wouldn't teach you anything. "
                 "I can walk you through it a step at a time instead. A nudge, an "
                 "explanation and a reveal each cost one of your hints "
                 f"({left} left).", "info",
                 chips=[NEXT_MOVE_CHIP, chip("Explain how to solve this", "overview"),
                        lesson_chip("scanning")])


def _need_cell(what: str) -> dict:
    return reply(f"Select a cell on the board first, then ask me {what}.", "info",
                 chips=[chip("What am I missing in this row?", "missing")])


def _explain_cell(params, view) -> dict:
    if view.selected is None:
        return _need_cell("to explain it")
    r, c = view.selected
    cell, where = view.selected, cell_label(view.selected)
    if view.puzzle[r][c]:
        return reply(f"{where} is a printed clue ({view.puzzle[r][c]}). Clues never change, "
                     f"but they are the starting point: they rule that digit out of the "
                     f"rest of {hintlib.unit_label(r)}, {hintlib.unit_label(9 + c)} and "
                     f"{hintlib.unit_label(units_of(cell)[2])}.", "explain", cells=[[r, c]])
    if cell in view.wrong:
        return _why_wrong({}, view)
    value = view.shown[r][c]
    if value:
        known = [row[:] for row in view.known]
        known[r][c] = 0
        tech, unit = forced_technique(known, cell, value)
        how = {NAKED: " It's a naked single: nothing else fits there.",
               HIDDEN: f" It's a hidden single: in {hintlib.unit_label(unit or 0)}, "
                       f"{value} had nowhere else to go."}.get(
                           tech, " It wasn't a single, so it needed deeper logic or a guess.")
        source = "revealed by a hint" if cell in view.revealed else "correct"
        return reply(f"{where} = {value} is {source}.{how}", "explain", cells=[[r, c]],
                     chips=[lesson_chip(lessons.FOR_TECHNIQUE[tech])] if tech else None)

    # An empty cell: show the three lists the player has to combine themselves.
    steps, ignored = [], []
    for u in units_of(cell):
        steps.append(f"{hintlib.unit_label(u)} already has {fmt_digits(digits_in(view.known, u))}.")
        ignored += [x for x in sl.UNITS[u] if x in view.wrong]
    steps.append("Digits missing from all three lists could go here. If exactly one is "
                 "left, that is your answer.")
    note = ""
    if ignored:
        note = " Ignoring your wrong entries at " + fmt_list(
            sorted({cell_label(x) for x in ignored})) + "."
    return reply(f"{where} is empty. Here is what its row, column and box rule out."
                 f"{note} Combining the lists is the skill, so I'll leave that to you.",
                 "explain", steps=steps, cells=[[r, c]], chips=[NEXT_MOVE_CHIP])


def _why_not(params, view) -> dict:
    if view.selected is None:
        return _need_cell("why a digit can't go there")
    d = params.get("digit")
    r, c = view.selected
    cell, where = view.selected, cell_label(view.selected)
    if not d:
        return reply("Which digit? Ask me something like \"Why can't I put 5 here?\".", "info")
    if view.puzzle[r][c] or cell in view.revealed:
        return reply(f"{where} is locked, so no digit can be entered there.", "info")
    if view.shown[r][c] == d:
        return reply(f"{where} already holds {d}.", "info")
    clashes, units = find_clashes(view.shown, cell, d)
    if clashes:
        return reply(f"{d} can't go in {where}: " + clash_sentence(d, clashes, units, view.puzzle),
                     "explain", cells=[[r, c]] + [list(x) for x in clashes[:2]])
    return reply(f"{d} doesn't break any rule in {where} on the current board, so it's allowed. "
                 f"Whether it's right is something I can't tell you for free. Work it out, "
                 f"try it (a wrong digit costs a mistake), or ask for a hint.",
                 "info", cells=[[r, c]], chips=[NEXT_MOVE_CHIP, chip("Explain this cell", "explain_cell")])


def _missing(params, view) -> dict:
    u = params.get("unit")
    if u is None:
        if view.selected is None:
            return _need_cell("what its row is missing")
        u = units_of(view.selected)[0]
    cells = sl.UNITS[u]
    present = digits_in(view.known, u)
    missing = [d for d in range(1, 10) if d not in present]
    label = hintlib.unit_label(u)
    if not missing:
        return reply(f"{label} is complete.", "info", cells=[list(x) for x in cells])
    open_cells = [x for x in cells if view.shown[x[0]][x[1]] == 0]
    bad = [x for x in cells if x in view.wrong]
    steps = [f"{label} has {fmt_digits(present)} ({len(present)} of 9).",
             f"It still needs {fmt_digits(missing)}.",
             "For each missing digit, ask which empty cells could still hold it. If only "
             "one can, that's a hidden single."]
    if open_cells:
        steps.append("Empty cells: " + fmt_list([cell_label(x) for x in open_cells]) + ".")
    if bad:
        steps.append("Ignoring your wrong entries at " + fmt_list([cell_label(x) for x in bad]) + ".")
    return reply(f"Here is what {label} is missing.", "explain", steps=steps,
                 cells=[list(x) for x in cells], chips=[lesson_chip("hidden_single")])


def _why_wrong(params, view) -> dict:
    cell = view.selected if view.selected in view.wrong else None
    rec = None
    if cell is None:
        for m in reversed(view.moves):
            if m.kind in ("mistake", "repeat_mistake") and m.cell in view.wrong \
                    and view.shown[m.cell[0]][m.cell[1]] == m.digit:
                rec = m
                break
        if rec is None:
            return reply("Nothing on your board is marked wrong right now.", "info")
        cell = rec.cell
    r, c = cell
    d = view.shown[r][c]
    clashes, units = find_clashes(view.shown, cell, d)
    if clashes:
        wrong_peers = all(x in view.wrong for x in clashes)
        rec = MoveRecord(0, cell, d, 0, "mistake", cause="cascade" if wrong_peers else "conflict",
                         clashes=tuple(clashes), clash_units=tuple(units))
    else:
        rec = MoveRecord(0, cell, d, 0, "mistake", cause="guess")
    out = mistake_reply(rec, view.puzzle)
    out["text"] = out["text"].replace("Not quite. ", "", 1)
    out["text"] = out["text"][0].upper() + out["text"][1:]
    return out


def _board_facts(view: View) -> Tuple[Counter, List[int]]:
    counts = Counter(v for row in view.known for v in row if v)
    return counts, [u for u in range(27) if 0 < _empties(view.known, u)]


def _better(params, view) -> dict:
    steps: List[str] = []
    if view.wrong:
        steps.append("Fix first: " + fmt_list([cell_label(x) for x in sorted(view.wrong)]) +
                     " is marked wrong. Everything built on a wrong digit stays shaky.")
    counts, open_units = _board_facts(view)
    candidates = [d for d in range(1, 10) if counts[d] < 9]
    if candidates:
        d = max(candidates, key=lambda x: (counts[x], -x))
        left = 9 - counts[d]
        steps.append(f"Scan for {d}: it's on the board {counts[d]} times, so only {left} "
                     f"more to place. Digits that appear most are the easiest to finish.")
    if open_units:
        u = min(open_units, key=lambda x: (_empties(view.known, x), x))
        steps.append(f"Look at {hintlib.unit_label(u)}: only {_empties(view.known, u)} "
                     f"cell{'s' if _empties(view.known, u) != 1 else ''} left, so there are "
                     f"few possibilities to check.")
    guesses = sum(1 for m in view.moves if (m.kind == "correct" and m.forced is None and not m.undone)
                  or (m.kind == "mistake" and m.cause == "guess"))
    if guesses >= 2:
        steps.append("You've made a few entries that weren't forced. Only enter a digit once you "
                     "can say why nothing else fits.")
    steps.append("When you're truly stuck, ask for a nudge: it costs one hint and keeps the "
                 "decision with you.")
    return reply("A more reliable way to make progress from your current board:", "explain",
                 steps=steps, chips=[lesson_chip("scanning"), NEXT_MOVE_CHIP])


def _overview(params, view) -> dict:
    level = view.difficulty
    scope = {
        "Easy": "Every cell on this board can be found with naked singles alone, so no "
                "guessing is ever needed.",
        "Medium": "This board can be finished with singles, naked and hidden. No guessing "
                  "is needed.",
        "Hard": "Singles alone won't finish this board; at some point you'll need a "
                "stronger technique such as naked pairs. I can teach those, but my board "
                "analysis only spots singles.",
    }.get(level, "")
    steps = [
        "Scan the digits that appear most and find where they still fit.",
        "For an empty cell, list what its row, column and box rule out. One digit left? Place it.",
        "For a missing digit in a row, column or box, check where it can still go. One cell? Place it.",
        "Only enter a digit when you can say why nothing else fits. A wrong digit costs a mistake.",
        f"When stuck, a nudge, an explanation and a reveal each cost one hint ({view.hints_left} left).",
    ]
    return reply(f"{scope} Here's a way to work through it without anyone giving away the answer:",
                 "explain", steps=steps,
                 chips=[lesson_chip("scanning"), lesson_chip("hidden_single"), NEXT_MOVE_CHIP])


def _technique_menu(params, view) -> dict:
    order = list(lessons.ORDER)
    suggestion = _suggested_lesson(view)
    if suggestion in order:
        order.remove(suggestion)
        order.insert(0, suggestion)
    text = "Pick a technique to learn."
    if suggestion and view.moves:
        text += f" Based on your moves, {lessons.LESSONS[suggestion]['title'].lower()} is a good one to start with."
    return reply(text, "lesson", chips=[lesson_chip(n, "Teach me") for n in order])


def _suggested_lesson(view: View) -> Optional[str]:
    active = [m for m in view.moves if not m.undone]
    mistakes = [m for m in active if m.kind == "mistake"]
    if sum(1 for m in mistakes if m.cause in ("conflict", "cascade")) >= 1:
        return "scanning"
    unforced = sum(1 for m in active if m.kind == "correct" and m.forced is None)
    guesses = sum(1 for m in mistakes if m.cause == "guess")
    if unforced + guesses >= 2:
        return "hidden_single"
    if sum(1 for m in active if m.kind == "correct" and m.forced == HIDDEN) == 0 \
            and sum(1 for m in active if m.kind == "correct") >= 3:
        return "hidden_single"
    return None


def _lesson(params, view) -> dict:
    name = params["name"]
    lesson = lessons.LESSONS[name]
    text = lesson["intro"] + " " + lesson["example"]
    if lesson["note"]:
        text += " " + lesson["note"]
    q = lesson["quiz"]
    return reply(text, "lesson", title=lesson["title"], steps=lesson["steps"],
                 quiz={"lesson": name, "question": q["question"], "options": q["options"]})


def _lesson_answer(params, view) -> dict:
    name, choice = params["name"], params["choice"]
    q = lessons.LESSONS[name]["quiz"]
    right = choice == q["answer"]
    head = "Correct." if right else f"Not quite. The answer is {q['options'][q['answer']]}."
    order = lessons.ORDER
    nxt = order[(order.index(name) + 1) % len(order)]
    return reply(f"{head} {q['explain']}", "lesson", chips=[
        lesson_chip(nxt, "Next lesson"), chip("Back to the board", "help")])


def review_reply(view: View, final: Optional[str] = None) -> dict:
    return _review({"final": final}, view)


def _review(params, view) -> dict:
    final = (params.get("final") if params else None) or view.status
    played = [m for m in view.moves if m.kind in ("correct", "mistake")]
    if not played:
        return reply("Nothing to review yet. Make a few moves and ask again.", "review",
                     chips=[NEXT_MOVE_CHIP])
    correct = [m for m in played if m.kind == "correct" and not m.undone]
    naked = sum(1 for m in correct if m.forced == NAKED)
    hidden = sum(1 for m in correct if m.forced == HIDDEN)
    unforced = len(correct) - naked - hidden
    mistakes = [m for m in played if m.kind == "mistake"]
    by = Counter(m.cause for m in mistakes)

    steps = [f"{len(correct)} correct entries: {naked} naked single{'s' if naked != 1 else ''}, "
             f"{hidden} hidden single{'s' if hidden != 1 else ''} and {unforced} that weren't singles."]
    if mistakes:
        bits = []
        if by["conflict"]:
            bits.append(f"{by['conflict']} reused a digit already in its row, column or box")
        if by["cascade"]:
            bits.append(f"{by['cascade']} clashed with an earlier wrong entry")
        if by["guess"]:
            bits.append(f"{by['guess']} {'was a guess' if by['guess'] == 1 else 'were guesses'} that didn't clash")
        steps.append(f"{len(mistakes)} mistake{'s' if len(mistakes) != 1 else ''}: " + "; ".join(bits) + ".")
    else:
        steps.append("No mistakes.")

    lesson = _suggested_lesson(view)
    if by["conflict"] + by["cascade"] >= by["guess"] and by["conflict"] + by["cascade"] > 0:
        advice = "Next focus: check the digit against its row, column and box before you type it."
    elif by["guess"] or unforced >= 2:
        advice = "Next focus: look for forced cells instead of guessing. Say why nothing else fits before you enter a digit."
    elif hidden == 0 and len(correct) >= 3:
        advice = "Next focus: try spotting hidden singles; you've only used naked ones so far."
    else:
        advice = "Clean play. To stretch yourself, try a harder level or use fewer hints."
    steps.append(advice)

    heads = {"won": "Puzzle solved. Here's how it went.", "lost": "Game over. Here's what happened.",
             "gave_up": "Here's how your game went before you gave up."}
    chips = [lesson_chip(lesson)] if lesson else [chip("Teach me a technique", "technique")]
    return reply(heads.get(final, "Here's how you're doing so far."), "review", steps=steps, chips=chips)
