"""
test_sudoku.py
--------------
Unit tests for the Sudoku engine in sudoku_logic.py.

Run with:  python -m unittest test_sudoku      (or: python test_sudoku.py)
"""

import copy
import unittest

import sudoku_logic as sl


class TestValidation(unittest.TestCase):
    def test_is_safe_row_conflict(self):
        board = [[0] * 9 for _ in range(9)]
        board[0][0] = 5
        self.assertFalse(sl.is_safe(board, 0, 4, 5))
        self.assertTrue(sl.is_safe(board, 0, 4, 6))

    def test_is_safe_col_conflict(self):
        board = [[0] * 9 for _ in range(9)]
        board[0][0] = 5
        self.assertFalse(sl.is_safe(board, 4, 0, 5))

    def test_is_safe_box_conflict(self):
        board = [[0] * 9 for _ in range(9)]
        board[0][0] = 5
        self.assertFalse(sl.is_safe(board, 2, 2, 5))  # same 3x3 box
        self.assertTrue(sl.is_safe(board, 3, 3, 5))   # different box

    def test_is_safe_ignores_the_cell_itself(self):
        board = [[0] * 9 for _ in range(9)]
        board[0][0] = 5
        # The value already in (0,0) should not count as its own conflict.
        self.assertTrue(sl.is_safe(board, 0, 0, 5))

    def test_find_conflicts(self):
        board = [[0] * 9 for _ in range(9)]
        board[0][0] = 5
        board[0][1] = 5  # duplicate in row
        conflicts = sl.find_conflicts(board)
        self.assertIn((0, 0), conflicts)
        self.assertIn((0, 1), conflicts)

    def test_complete_and_valid(self):
        board = sl.generate_full_board()
        self.assertTrue(sl.is_complete_and_valid(board))
        board[0][0] = 0
        self.assertFalse(sl.is_complete_and_valid(board))


class TestSolver(unittest.TestCase):
    def test_solve_known_puzzle(self):
        puzzle = [
            [5, 3, 0, 0, 7, 0, 0, 0, 0],
            [6, 0, 0, 1, 9, 5, 0, 0, 0],
            [0, 9, 8, 0, 0, 0, 0, 6, 0],
            [8, 0, 0, 0, 6, 0, 0, 0, 3],
            [4, 0, 0, 8, 0, 3, 0, 0, 1],
            [7, 0, 0, 0, 2, 0, 0, 0, 6],
            [0, 6, 0, 0, 0, 0, 2, 8, 0],
            [0, 0, 0, 4, 1, 9, 0, 0, 5],
            [0, 0, 0, 0, 8, 0, 0, 7, 9],
        ]
        work = copy.deepcopy(puzzle)
        self.assertTrue(sl.solve(work))
        self.assertTrue(sl.is_complete_and_valid(work))
        # Given clues must be preserved in the solution.
        for r in range(9):
            for c in range(9):
                if puzzle[r][c] != 0:
                    self.assertEqual(work[r][c], puzzle[r][c])

    def test_count_solutions_unique(self):
        puzzle, _ = sl.generate_puzzle("Easy")
        self.assertEqual(sl.count_solutions(puzzle), 1)

    def test_count_solutions_multiple(self):
        # An almost-empty board has many solutions; count stops at the limit.
        board = [[0] * 9 for _ in range(9)]
        self.assertGreaterEqual(sl.count_solutions(board, limit=2), 2)

    def test_count_solutions_does_not_mutate(self):
        puzzle, _ = sl.generate_puzzle("Medium")
        before = copy.deepcopy(puzzle)
        sl.count_solutions(puzzle)
        self.assertEqual(puzzle, before)


class TestGeneration(unittest.TestCase):
    def test_full_board_valid(self):
        for _ in range(5):
            self.assertTrue(sl.is_complete_and_valid(sl.generate_full_board()))

    def test_puzzle_is_unique_and_matches_solution(self):
        for level in ("Easy", "Medium", "Hard"):
            puzzle, solution = sl.generate_puzzle(level)
            self.assertTrue(sl.is_complete_and_valid(solution))
            self.assertEqual(sl.count_solutions(puzzle), 1,
                             f"{level} puzzle not unique")
            solved = copy.deepcopy(puzzle)
            sl.solve(solved)
            self.assertEqual(solved, solution,
                             f"{level} unique solution != generated solution")

    def test_difficulty_clue_counts_ordered(self):
        # Easy should give more clues than Medium, which gives more than Hard.
        counts = {}
        for level in ("Easy", "Medium", "Hard"):
            puzzle, _ = sl.generate_puzzle(level)
            counts[level] = sum(1 for r in range(9) for c in range(9)
                                if puzzle[r][c] != 0)
        self.assertGreater(counts["Easy"], counts["Medium"])
        self.assertGreater(counts["Medium"], counts["Hard"])

    def test_clue_count_near_target(self):
        # Generator should reach at or very near the target clue count.
        for level in ("Easy", "Medium", "Hard"):
            puzzle, _ = sl.generate_puzzle(level)
            given = sum(1 for r in range(9) for c in range(9)
                        if puzzle[r][c] != 0)
            target = sl.DIFFICULTY_CLUES[level]
            # Uniqueness constraint can leave a few extra clues; allow a margin.
            self.assertLessEqual(given, target + 8)

    def test_invalid_difficulty_raises(self):
        with self.assertRaises(ValueError):
            sl.generate_puzzle("Impossible")


if __name__ == "__main__":
    unittest.main(verbosity=2)
