"""
test_hints.py
-------------
Tests for the logic-based hint engine (hints.py) and for the difficulty
grading that the generator uses (sudoku_logic.grade_puzzle).
"""

import copy
import random
import unittest

import hints as hl
import sudoku_logic as sl


def open_cells(puzzle):
    return [(r, c) for r in range(9) for c in range(9) if puzzle[r][c] == 0]


class TestHintEngine(unittest.TestCase):
    def test_hints_are_sound_and_target_open_cells(self):
        random.seed(11)
        for level in ("Easy", "Medium", "Hard"):
            for _ in range(8):
                puzzle, solution = sl.generate_puzzle(level)
                hint = hl.find_hint(puzzle, solution, open_cells(puzzle))
                r, c = hint.cell
                self.assertEqual(puzzle[r][c], 0, "hint must target an empty cell")
                self.assertEqual(hint.digit, solution[r][c], "hint digit must be correct")
                self.assertIn(hint.digit, hint.candidates)
                self.assertIn(hint.cell, sl.UNITS[hint.unit], "region must contain the cell")
                if hint.technique == hl.NAKED:
                    self.assertEqual(len(hint.candidates), 1)

    def test_no_open_cells_gives_no_hint(self):
        puzzle, solution = sl.generate_puzzle("Easy")
        self.assertIsNone(hl.find_hint(puzzle, solution, []))

    def test_prefer_picks_the_cell_the_player_is_looking_at(self):
        random.seed(5)
        puzzle, solution = sl.generate_puzzle("Easy")
        masks = sl.candidate_masks(puzzle)
        singles = [cell for cell, m in masks.items() if bin(m).count("1") == 1]
        self.assertGreater(len(singles), 2)
        for cell in singles[:5]:
            hint = hl.find_hint(puzzle, solution, open_cells(puzzle), prefer=cell)
            self.assertEqual(hint.cell, cell)

    def test_wrong_entries_are_ignored_not_trusted(self):
        """A wrong digit sitting in the grid must not corrupt the deduction."""
        random.seed(8)
        puzzle, solution = sl.generate_puzzle("Easy")
        cells = open_cells(puzzle)
        known = copy.deepcopy(puzzle)                  # wrong entry NOT in known
        poisoned = cells[0]
        targets = [c for c in cells if c != poisoned]  # poisoned cell is not open
        hint = hl.find_hint(known, solution, targets)
        self.assertNotEqual(hint.cell, poisoned)
        self.assertEqual(hint.digit, solution[hint.cell[0]][hint.cell[1]])

    def test_following_hints_solves_each_level_with_the_graded_technique(self):
        """The hint engine and the difficulty grader must agree."""
        random.seed(21)
        expect = {"Easy": {hl.NAKED},
                  "Medium": {hl.NAKED, hl.HIDDEN}}
        for level in ("Easy", "Medium", "Hard"):
            puzzle, solution = sl.generate_puzzle(level)
            known = copy.deepcopy(puzzle)
            used = []
            while open_cells(known):
                hint = hl.find_hint(known, solution, open_cells(known))
                used.append(hint.technique)
                r, c = hint.cell
                known[r][c] = hint.digit
            self.assertEqual(known, solution, f"{level}: hints must complete the puzzle")
            if level in expect:
                self.assertTrue(set(used) <= expect[level], f"{level} used {set(used)}")
            if level == "Medium":
                self.assertIn(hl.HIDDEN, used, "Medium must need a hidden single")
            if level == "Hard":
                self.assertIn(hl.NARROW, used, "Hard must get stuck on singles")

    def test_levels_never_leak_the_digit_early(self):
        random.seed(3)
        for level in ("Easy", "Medium", "Hard"):
            puzzle, solution = sl.generate_puzzle(level)
            hint = hl.find_hint(puzzle, solution, open_cells(puzzle))
            cell = hl.cell_label(hint.cell)
            level1 = hl.describe(hint, 1)
            self.assertNotIn(cell, level1, "level 1 must not name the cell")
            self.assertNotIn(f"= {hint.digit}", level1)
            level2 = hl.describe(hint, 2)
            self.assertIn(cell, level2, "level 2 names the cell")
            if hint.technique != hl.NARROW:     # 'narrow' lists its candidate digits
                self.assertNotIn(f"= {hint.digit}", level2)
            self.assertIn(f"{cell} = {hint.digit}", hl.describe(hint, 3))

    def test_labels(self):
        self.assertEqual(hl.unit_label(0), "Row 1")
        self.assertEqual(hl.unit_label(8), "Row 9")
        self.assertEqual(hl.unit_label(9), "Column 1")
        self.assertEqual(hl.unit_label(17), "Column 9")
        self.assertEqual(hl.unit_label(18), "Box 1")
        self.assertEqual(hl.unit_label(26), "Box 9")
        self.assertEqual(hl.cell_label((0, 0)), "R1C1")
        self.assertEqual(hl.cell_label((8, 8)), "R9C9")


class TestDifficultyGrading(unittest.TestCase):
    def test_every_level_generates_its_graded_unique_puzzle(self):
        random.seed(4)
        for level, profile in sl.DIFFICULTY_PROFILE.items():
            for _ in range(10):
                puzzle, solution = sl.generate_puzzle(level)
                self.assertEqual(sl.count_solutions(puzzle), 1)
                self.assertEqual(sl.grade_puzzle(puzzle), profile["grade"],
                                 f"{level} puzzle has the wrong technique grade")

    def test_levels_are_ordered_by_technique_and_clues(self):
        ranks = {"naked": 0, "singles": 1, "hard": 2}
        order = [ranks[sl.DIFFICULTY_PROFILE[l]["grade"]] for l in ("Easy", "Medium", "Hard")]
        self.assertEqual(order, sorted(set(order)))
        clues = [sl.DIFFICULTY_PROFILE[l]["clues"] for l in ("Easy", "Medium", "Hard")]
        self.assertEqual(clues, sorted(clues, reverse=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
