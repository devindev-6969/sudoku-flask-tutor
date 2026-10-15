"""
sudoku_logic.py
---------------
Pure Sudoku engine: board generation, solving, validation and difficulty.

This module contains no UI code so it can be tested and reused independently.
A board is represented as a 9x9 list of lists of ints, where 0 means empty.

Difficulty is defined by the *technique a human needs*, not just by how many
clues are given (see `grade_puzzle`):

    naked    solvable using only "naked singles" (a cell with one candidate)
    singles  also needs "hidden singles" (a digit with one place in a unit)
    hard     cannot be finished with singles alone
"""

from __future__ import annotations

import copy
import random
from typing import Dict, List, Optional, Set, Tuple

Board = List[List[int]]
Cell = Tuple[int, int]

SIZE = 9
BOX = 3

# What each difficulty level generates: a clue target plus the technique grade
# the puzzle must require. Measured against the generator (see test suite):
# clue count alone does NOT separate the levels, technique does.
DIFFICULTY_PROFILE = {
    "Easy":   {"clues": 42, "grade": "naked"},
    "Medium": {"clues": 32, "grade": "singles"},
    "Hard":   {"clues": 27, "grade": "hard"},
}

# Kept for compatibility: target number of given clues per level.
DIFFICULTY_CLUES = {level: p["clues"] for level, p in DIFFICULTY_PROFILE.items()}

# Every row, column and 3x3 box as a list of (row, col) cells.
UNITS: List[List[Cell]] = (
    [[(r, c) for c in range(SIZE)] for r in range(SIZE)]
    + [[(r, c) for r in range(SIZE)] for c in range(SIZE)]
    + [[(br + i, bc + j) for i in range(BOX) for j in range(BOX)]
       for br in range(0, SIZE, BOX) for bc in range(0, SIZE, BOX)]
)

_ALL_DIGITS = 0x1FF                       # bits 0..8  <->  digits 1..9
_POPCOUNT = [bin(i).count("1") for i in range(512)]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def is_safe(board: Board, row: int, col: int, num: int) -> bool:
    """Return True if placing `num` at (row, col) breaks no Sudoku rule.

    The cell at (row, col) is ignored during the check, so this also works
    for a value already sitting in that cell.
    """
    for i in range(SIZE):
        if i != col and board[row][i] == num:
            return False
        if i != row and board[i][col] == num:
            return False

    box_row = (row // BOX) * BOX
    box_col = (col // BOX) * BOX
    for r in range(box_row, box_row + BOX):
        for c in range(box_col, box_col + BOX):
            if (r != row or c != col) and board[r][c] == num:
                return False
    return True


def find_empty(board: Board) -> Optional[Cell]:
    """Return the (row, col) of the first empty cell, or None if full."""
    for r in range(SIZE):
        for c in range(SIZE):
            if board[r][c] == 0:
                return r, c
    return None


def find_conflicts(board: Board) -> List[Cell]:
    """Return a list of (row, col) coordinates of cells that violate a rule."""
    conflicts = set()
    for r in range(SIZE):
        for c in range(SIZE):
            val = board[r][c]
            if val != 0 and not is_safe(board, r, c, val):
                conflicts.add((r, c))
    return sorted(conflicts)


def is_complete_and_valid(board: Board) -> bool:
    """Return True only if the board is completely filled and rule-abiding."""
    for r in range(SIZE):
        for c in range(SIZE):
            val = board[r][c]
            if val == 0 or not is_safe(board, r, c, val):
                return False
    return True


# --------------------------------------------------------------------------- #
# Candidates (what digits can still go where)
# --------------------------------------------------------------------------- #
def candidate_masks(board: Board) -> Dict[Cell, int]:
    """Map every empty cell to a bitmask of the digits that can legally go there.

    Bit d-1 set  <->  digit d is a candidate.
    """
    rows = [0] * SIZE
    cols = [0] * SIZE
    boxes = [0] * SIZE
    for r in range(SIZE):
        for c in range(SIZE):
            v = board[r][c]
            if v:
                bit = 1 << (v - 1)
                rows[r] |= bit
                cols[c] |= bit
                boxes[(r // BOX) * BOX + c // BOX] |= bit
    return {
        (r, c): ~(rows[r] | cols[c] | boxes[(r // BOX) * BOX + c // BOX]) & _ALL_DIGITS
        for r in range(SIZE) for c in range(SIZE) if board[r][c] == 0
    }


def mask_to_digits(mask: int) -> List[int]:
    """Convert a candidate bitmask to a sorted list of digits."""
    return [d + 1 for d in range(SIZE) if mask >> d & 1]


def candidates(board: Board) -> Dict[Cell, Set[int]]:
    """Map every empty cell to the set of digits that can legally go there."""
    return {cell: set(mask_to_digits(m)) for cell, m in candidate_masks(board).items()}


# --------------------------------------------------------------------------- #
# Solving (bitmask backtracking, always branching on the most constrained cell)
# --------------------------------------------------------------------------- #
def _search(board: Board, limit: int, shuffle: bool = False
            ) -> Tuple[int, Optional[Board]]:
    """Count solutions of `board`, stopping once `limit` are found.

    Returns (count, first_solution). `board` is never modified. Branching on
    the cell with the fewest candidates keeps the search tiny even for sparse
    puzzles. `shuffle=True` randomises digit order (used to build full boards).
    """
    rows = [0] * SIZE
    cols = [0] * SIZE
    boxes = [0] * SIZE
    empties: List[Tuple[int, int, int]] = []
    for r in range(SIZE):
        for c in range(SIZE):
            b = (r // BOX) * BOX + c // BOX
            v = board[r][c]
            if v:
                bit = 1 << (v - 1)
                if rows[r] & bit or cols[c] & bit or boxes[b] & bit:
                    return 0, None            # the givens already contradict
                rows[r] |= bit
                cols[c] |= bit
                boxes[b] |= bit
            else:
                empties.append((r, c, b))

    grid = [row[:] for row in board]
    state = {"count": 0, "first": None}

    def rec(remaining: List[Tuple[int, int, int]]) -> bool:
        """Return True when the search should stop (limit reached)."""
        if not remaining:
            state["count"] += 1
            if state["first"] is None:
                state["first"] = [row[:] for row in grid]
            return state["count"] >= limit

        best_i, best_mask, best_pop = -1, 0, 10
        for i, (r, c, b) in enumerate(remaining):
            m = ~(rows[r] | cols[c] | boxes[b]) & _ALL_DIGITS
            p = _POPCOUNT[m]
            if p < best_pop:
                best_i, best_mask, best_pop = i, m, p
                if p <= 1:
                    break
        if best_pop == 0:
            return False                      # dead end

        r, c, b = remaining[best_i]
        rest = remaining[:best_i] + remaining[best_i + 1:]
        digits = [d for d in range(SIZE) if best_mask >> d & 1]
        if shuffle:
            random.shuffle(digits)
        for d in digits:
            bit = 1 << d
            rows[r] |= bit
            cols[c] |= bit
            boxes[b] |= bit
            grid[r][c] = d + 1
            stop = rec(rest)
            rows[r] &= ~bit
            cols[c] &= ~bit
            boxes[b] &= ~bit
            grid[r][c] = 0
            if stop:
                return True
        return False

    rec(empties)
    return state["count"], state["first"]


def solve(board: Board) -> bool:
    """Solve `board` in place using backtracking. Return True if solvable."""
    _, first = _search(board, 1)
    if first is None:
        return False
    for r in range(SIZE):
        board[r][:] = first[r]
    return True


def count_solutions(board: Board, limit: int = 2) -> int:
    """Count solutions of `board` up to `limit` (board is left unmodified).

    Stopping at `limit` keeps uniqueness testing fast: we only ever need to
    know whether there are 0, exactly 1, or 2-or-more solutions.
    """
    return _search(board, limit)[0]


# --------------------------------------------------------------------------- #
# Difficulty grading (which human technique is required?)
# --------------------------------------------------------------------------- #
def _solvable_by_singles(puzzle: Board, allow_hidden: bool) -> bool:
    """Try to finish `puzzle` using only naked (and optionally hidden) singles."""
    board = copy.deepcopy(puzzle)
    while True:
        masks = candidate_masks(board)
        if not masks:
            return True
        progressed = False

        for (r, c), m in masks.items():                       # naked singles
            if _POPCOUNT[m] == 1:
                board[r][c] = mask_to_digits(m)[0]
                progressed = True
        if progressed:
            continue

        if allow_hidden:                                       # hidden singles
            for unit in UNITS:
                for d in range(SIZE):
                    bit = 1 << d
                    spots = [cell for cell in unit
                             if cell in masks and masks[cell] & bit]
                    placed = any(board[r][c] == d + 1 for r, c in unit)
                    if len(spots) == 1 and not placed:
                        r, c = spots[0]
                        board[r][c] = d + 1
                        progressed = True
                if progressed:
                    break
        if not progressed:
            return False


def grade_puzzle(puzzle: Board) -> str:
    """Return 'naked', 'singles' or 'hard' - the simplest technique set that
    can solve `puzzle` completely."""
    if _solvable_by_singles(puzzle, allow_hidden=False):
        return "naked"
    if _solvable_by_singles(puzzle, allow_hidden=True):
        return "singles"
    return "hard"


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def generate_full_board() -> Board:
    """Generate a complete, valid, randomly-filled Sudoku board."""
    _, board = _search([[0] * SIZE for _ in range(SIZE)], 1, shuffle=True)
    assert board is not None
    return board


def _carve(solution: Board, target_clues: int) -> Board:
    """Remove cells from `solution` while the puzzle keeps a unique solution."""
    puzzle = copy.deepcopy(solution)
    cells = [(r, c) for r in range(SIZE) for c in range(SIZE)]
    random.shuffle(cells)

    clues = SIZE * SIZE
    for row, col in cells:
        if clues <= target_clues:
            break
        backup = puzzle[row][col]
        puzzle[row][col] = 0
        if count_solutions(puzzle, limit=2) == 1:
            clues -= 1
        else:
            puzzle[row][col] = backup
    return puzzle


def generate_puzzle(difficulty: str = "Medium",
                    max_attempts: int = 200) -> Tuple[Board, Board]:
    """Generate a puzzle for the given difficulty.

    Returns (puzzle, solution). The puzzle has exactly one solution and needs
    the technique grade that `DIFFICULTY_PROFILE` asks for. If no matching
    puzzle turns up within `max_attempts` (not observed in practice), the last
    uniquely-solvable puzzle is returned so the call always terminates.
    """
    if difficulty not in DIFFICULTY_PROFILE:
        raise ValueError(f"Unknown difficulty: {difficulty!r}")

    profile = DIFFICULTY_PROFILE[difficulty]
    puzzle, solution = [], []
    for _ in range(max_attempts):
        solution = generate_full_board()
        puzzle = _carve(solution, profile["clues"])
        if grade_puzzle(puzzle) == profile["grade"]:
            break
    return puzzle, solution


if __name__ == "__main__":
    # Tiny manual sanity check when run directly.
    for level in ("Easy", "Medium", "Hard"):
        p, s = generate_puzzle(level)
        given = sum(1 for r in range(SIZE) for c in range(SIZE) if p[r][c])
        assert count_solutions(p) == 1
        solved = copy.deepcopy(p)
        solve(solved)
        assert solved == s
        print(f"{level:<6} clues={given}  grade={grade_puzzle(p)}  unique=yes")
