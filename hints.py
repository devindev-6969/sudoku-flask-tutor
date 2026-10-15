"""
hints.py
--------
Logic-based hints. Instead of reading the answer off the solution, the engine
looks for the deduction a human would make and explains it in three steps:

    level 1  "Nudge"    - which row / column / box to look at
    level 2  "Explain"  - which cell, and why it is forced (digit withheld)
    level 3  "Reveal"   - the digit itself

Techniques, easiest first:
    naked_single   a cell where only one digit can still fit
    hidden_single  a digit that has only one possible cell inside a unit
    narrow         fallback when nothing is forced: the most constrained cell

All reasoning uses the *trusted* board (clues + correct entries + revealed
hints). The player's wrong entries are treated as unknown, so a mistake can
never mislead the explanation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

import sudoku_logic as sl

Cell = Tuple[int, int]

NAKED = "naked_single"
HIDDEN = "hidden_single"
NARROW = "narrow"

MAX_LEVEL = 3


@dataclass(frozen=True)
class Hint:
    technique: str
    cell: Cell                    # the cell the hint is about
    digit: int                    # its true value (only shown at level 3)
    unit: int                     # index into sl.UNITS: region to look at
    candidates: Tuple[int, ...]   # digits that can still fit in `cell`


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
def unit_label(unit: int) -> str:
    """Human name of a unit index: 0-8 rows, 9-17 columns, 18-26 boxes."""
    if unit < 9:
        return f"Row {unit + 1}"
    if unit < 18:
        return f"Column {unit - 9 + 1}"
    return f"Box {unit - 18 + 1}"


def unit_cells(unit: int) -> List[Cell]:
    return list(sl.UNITS[unit])


def cell_label(cell: Cell) -> str:
    return f"R{cell[0] + 1}C{cell[1] + 1}"


def _units_of(cell: Cell) -> List[int]:
    r, c = cell
    return [r, 9 + c, 18 + (r // 3) * 3 + c // 3]


def _most_filled_unit(cell: Cell, board: sl.Board) -> int:
    """The unit around `cell` with the fewest empty cells (the easiest to read)."""
    def empties(u: int) -> int:
        return sum(1 for r, c in sl.UNITS[u] if board[r][c] == 0)
    return min(_units_of(cell), key=lambda u: (empties(u), u))


# --------------------------------------------------------------------------- #
# Finding a hint
# --------------------------------------------------------------------------- #
def find_hint(known: sl.Board, solution: sl.Board, open_cells: Iterable[Cell],
              prefer: Optional[Cell] = None) -> Optional[Hint]:
    """Pick the most useful hint for the current position.

    known       trusted board: clues + correct entries + revealed hints
    solution    the full answer (used only for the digit shown at level 3)
    open_cells  cells with no entry at all - the only legal hint targets
    prefer      cell the player is looking at; chosen if it can be hinted

    Returns None when there is no open cell to hint at.
    """
    open_set: Set[Cell] = set(open_cells)
    if not open_set:
        return None

    masks = sl.candidate_masks(known)      # every cell empty in the trusted board

    # Naked singles: exactly one candidate.
    naked: Dict[Cell, Hint] = {}
    for cell in open_set:
        m = masks.get(cell, 0)
        if bin(m).count("1") == 1:
            digit = sl.mask_to_digits(m)[0]
            naked[cell] = Hint(NAKED, cell, digit,
                               _most_filled_unit(cell, known), (digit,))

    # Hidden singles: a digit with a single possible cell in some unit.
    hidden: Dict[Cell, Hint] = {}
    for ui, unit in enumerate(sl.UNITS):
        for d in range(sl.SIZE):
            if any(known[r][c] == d + 1 for r, c in unit):
                continue
            spots = [cell for cell in unit if cell in masks and masks[cell] >> d & 1]
            if len(spots) == 1 and spots[0] in open_set and spots[0] not in naked:
                cell = spots[0]
                hint = Hint(HIDDEN, cell, d + 1, ui,
                            tuple(sl.mask_to_digits(masks[cell])))
                # keep the unit that is easiest to read (fewest empty cells)
                old = hidden.get(cell)
                if old is None or _empties(ui, known) < _empties(old.unit, known):
                    hidden[cell] = hint

    singles = {**hidden, **naked}          # naked wins if a cell is both

    def distance(cell: Cell) -> int:
        if prefer is None:
            return 0
        return abs(cell[0] - prefer[0]) + abs(cell[1] - prefer[1])

    if prefer in singles:
        return singles[prefer]
    if naked:
        return min(naked.values(), key=lambda h: (distance(h.cell), h.cell))
    if hidden:
        return min(hidden.values(),
                   key=lambda h: (_empties(h.unit, known), distance(h.cell), h.cell))

    # Nothing is forced: point at the most constrained open cell.
    cell = min(open_set, key=lambda x: (bin(masks.get(x, 0)).count("1"),
                                        distance(x), x))
    digits = tuple(sl.mask_to_digits(masks.get(cell, 0)))
    return Hint(NARROW, cell, solution[cell[0]][cell[1]],
                _most_filled_unit(cell, known), digits)


def _empties(unit: int, board: sl.Board) -> int:
    return sum(1 for r, c in sl.UNITS[unit] if board[r][c] == 0)


# --------------------------------------------------------------------------- #
# Explaining a hint
# --------------------------------------------------------------------------- #
LEVEL_NAMES = {1: "Nudge", 2: "Explain", 3: "Reveal"}


def describe(hint: Hint, level: int) -> str:
    """Text for `hint` at the given level. Never mentions the digit before 3."""
    where = unit_label(hint.unit)
    cell = cell_label(hint.cell)

    # Kept to about 80 characters so each step fits two lines on a phone.
    if level == 1:
        if hint.technique == NAKED:
            return f"Look at {where}. One empty cell there has only one digit that fits."
        if hint.technique == HIDDEN:
            return f"Look at {where}. A digit it still needs has only one place to go."
        return f"Nothing is forced. In {where}, try the cell with the fewest options."

    if level == 2:
        if hint.technique == NAKED:
            return f"{cell}: its row, column and box rule out 8 digits. Find the one left."
        if hint.technique == HIDDEN:
            return f"{cell} is the only cell in {where} that can take a missing digit."
        options = " or ".join(str(d) for d in hint.candidates)
        return f"{cell} can only be {options}. Use its row, column and box to decide."

    if hint.technique == NAKED:
        return f"{cell} = {hint.digit}. The only digit left that fits (naked single)."
    if hint.technique == HIDDEN:
        return f"{cell} = {hint.digit}. In {where}, {hint.digit} had nowhere else to go."
    return f"{cell} = {hint.digit}. No simple deduction was available."
