"""
test_tutor.py
-------------
Tests for Learn mode: the deterministic tutor (tutor.py, lessons.py), its
integration in the game rules (game.py), the HTTP routes (app.py) and the
optional AI wording layer (tutor_llm.py, always driven through a FAKE transport:
nothing here talks to a real model).

The rules these tests defend:
  * switching modes changes nothing about the game, and the clock keeps running
  * the tutor can explain for free what the board already shows, but anything
    that points at an unsolved cell is a hint and spends the shared allowance
  * no reply ever tells the player a digit that belongs in an empty cell
  * the tutor never writes to the board
  * AI failure, or unsafe AI output, falls back to the built-in wording
  * the API key never reaches the browser

Run with:  python -m unittest test_tutor
"""

import json
import random
import unittest

import app as web
import game as gm
import hints as hintlib
import lessons
import sudoku_logic as sl
import tutor
import tutor_llm


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_game(level="Easy", seed=1, mode="learn", **kw):
    random.seed(seed)
    puzzle, solution = sl.generate_puzzle(level)
    kw.setdefault("clock", FakeClock())
    g = gm.Game(puzzle, solution, level, mode=mode, **kw)
    if mode == "learn":
        g.set_mode("learn")
    return g


def editable(g):
    return [(r, c) for r in range(9) for c in range(9) if not g.puzzle[r][c]]


def shown(g):
    return [[g._value_at(r, c) for c in range(9)] for r in range(9)]


def peers_digits(g, r, c):
    board = shown(g)
    out = set()
    for u in tutor.units_of((r, c)):
        out |= {board[a][b] for a, b in sl.UNITS[u] if board[a][b]}
    return out


def clash_digit(g, r, c):
    """A wrong digit that visibly repeats one already in the cell's row/column/box
    (None when nothing suitable is on the board)."""
    ds = sorted(d for d in peers_digits(g, r, c) if d != g.solution[r][c])
    return ds[0] if ds else None


def guess_digit(g, r, c):
    """A wrong digit that clashes with nothing on the board (so only the solution rejects it)."""
    taken = peers_digits(g, r, c)
    for d in range(1, 10):
        if d != g.solution[r][c] and d not in taken:
            return d
    return None


def cell_with_guess(g):
    for r, c in editable(g):
        if guess_digit(g, r, c):
            return r, c, guess_digit(g, r, c)
    raise AssertionError("no cell with a non-clashing wrong digit")


def texts(msgs):
    """All visible words of tutor messages: text, steps, quiz."""
    out = []
    for m in msgs:
        out.append(m.get("text", ""))
        out.extend(m.get("steps") or [])
        q = m.get("quiz")
        if q:
            out.append(q["question"])
            out.extend(q["options"])
    return "\n".join(out)


def last_tutor(g):
    return [m for m in g.tutor_messages(0) if m["role"] == "tutor"][-1]


def ask(g, intent=None, text=None, selected=None, **params):
    return g.tutor_ask(text=text, intent=intent, params=params or None, selected=selected)


def snapshot(g):
    return {"entries": shown(g), "revealed": dict(g.revealed), "mistakes": g.mistakes,
            "hints": g.hints_used, "status": g.status, "puzzle": [r[:] for r in g.puzzle]}


FREE_INTENTS = [("explain_cell", {}), ("why_not", {"digit": 5}), ("missing", {}),
                ("why_wrong", {}), ("better", {}), ("overview", {}), ("technique", {}),
                ("lesson", {"name": "scanning"}), ("review", {}), ("help", {})]


# --------------------------------------------------------------------------- #
# The question router
# --------------------------------------------------------------------------- #
class TestRouter(unittest.TestCase):
    CASES = [
        ("What should I do next?", "next_move"),
        ("Can you give me a small hint?", "next_move"),
        ("I'm stuck", "next_move"),
        ("Why can't I put 5 in this cell?", "why_not"),
        ("why is 3 not allowed here", "why_not"),
        ("What am I missing in this row?", "missing"),
        ("Why is this move incorrect?", "why_wrong"),
        ("What did I do wrong", "why_wrong"),
        ("Is there a better approach?", "better"),
        ("Explain how to solve this puzzle without revealing the answer", "overview"),
        ("explain this cell", "explain_cell"),
        ("Teach me a technique", "technique"),
        ("teach me hidden singles", "lesson"),
        ("how am I doing", "review"),
        ("hello", "help"),
        ("Give me the answer", "refuse_solve"),
        ("solve it for me", "refuse_solve"),
        ("tell me a joke about cats", "unknown"),
    ]

    def test_example_questions_route_to_the_right_intent(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(tutor.route(text)[0], want)

    def test_why_not_extracts_the_digit_and_row_names_extract_the_unit(self):
        self.assertEqual(tutor.route("Why can't I put 5 in this cell?"), ("why_not", {"digit": 5}))
        self.assertEqual(tutor.route("what is missing in row 3"), ("missing", {"unit": 2}))
        self.assertEqual(tutor.route("what is missing in column 4"), ("missing", {"unit": 12}))
        self.assertEqual(tutor.route("what is missing in box 9"), ("missing", {"unit": 26}))

    def test_without_revealing_is_not_mistaken_for_a_request_to_solve(self):
        self.assertNotEqual(
            tutor.route("show me the whole thing without giving the answer")[0], "refuse_solve")

    def test_client_parameters_are_validated(self):
        bad = [("why_not", {}), ("why_not", {"digit": 0}), ("why_not", {"digit": 10}),
               ("why_not", {"digit": True}), ("why_not", {"digit": "5"}),
               ("missing", {"unit": 27}), ("missing", {"unit": -1}),
               ("lesson", {"name": "nope"}), ("lesson", {}),
               ("lesson_answer", {"name": "scanning", "choice": 3}),
               ("lesson_answer", {"name": "scanning", "choice": True})]
        for intent, params in bad:
            with self.subTest(intent=intent, params=params), self.assertRaises(ValueError):
                tutor.clean_params(intent, params)
        self.assertEqual(tutor.clean_params("missing", None), {})


# --------------------------------------------------------------------------- #
# Hand-written content
# --------------------------------------------------------------------------- #
class TestLessons(unittest.TestCase):
    def test_every_lesson_is_complete_and_its_quiz_answer_is_in_range(self):
        for name, lesson in lessons.LESSONS.items():
            with self.subTest(lesson=name):
                for key in ("title", "intro", "steps", "example", "quiz"):
                    self.assertTrue(lesson[key])
                q = lesson["quiz"]
                self.assertTrue(0 <= q["answer"] < len(q["options"]))
                self.assertTrue(q["explain"])
        self.assertEqual(set(lessons.ORDER), set(lessons.LESSONS))

    def test_naked_single_quiz_is_arithmetically_right(self):
        # row 2,5,9 + column 1,3,8 + box 4,6 leaves exactly 7
        q = lessons.LESSONS["naked_single"]["quiz"]
        left = set(range(1, 10)) - {2, 5, 9, 1, 3, 8, 4, 6}
        self.assertEqual(left, {7})
        self.assertEqual(q["options"][q["answer"]], "7")

    def test_hidden_single_quiz_is_logically_right(self):
        # R7C2 and R7C5 are ruled out by columns 2 and 5, so R7C8 is the only spot.
        q = lessons.LESSONS["hidden_single"]["quiz"]
        self.assertEqual(q["options"][q["answer"]], "R7C8")

    def test_scanning_quiz_is_logically_right(self):
        # box rows 1-3, cols 7-9; rows 2,3 and cols 7,8 blocked -> only R1C9.
        q = lessons.LESSONS["scanning"]["quiz"]
        free = [(r, c) for r in (1, 2, 3) for c in (7, 8, 9) if r == 1 and c == 9]
        self.assertEqual(free, [(1, 9)])
        self.assertEqual(q["options"][q["answer"]], "R1C9")

    def test_lessons_never_look_at_the_board(self):
        g = make_game()
        before = snapshot(g)
        for name in lessons.LESSONS:
            ask(g, "lesson", name=name)
        self.assertEqual(snapshot(g), before)

    def test_pairs_lesson_admits_the_tutor_cannot_spot_pairs(self):
        self.assertIn("can't spot pairs", lessons.LESSONS["naked_pair"]["note"])

    def test_find_lesson_prefers_the_longest_phrase(self):
        self.assertEqual(lessons.find_lesson("what is a naked pair"), "naked_pair")
        self.assertEqual(lessons.find_lesson("hidden singles please"), "hidden_single")
        self.assertIsNone(lessons.find_lesson("hello"))


# --------------------------------------------------------------------------- #
# Move analysis
# --------------------------------------------------------------------------- #
class TestMoveAnalysis(unittest.TestCase):
    def test_a_wrong_digit_that_repeats_a_peer_is_classed_as_conflict(self):
        g = make_game()
        r, c = editable(g)[0]
        d = clash_digit(g, r, c)
        g.move(r, c, d)
        rec = g.moves[-1]
        self.assertEqual((rec.kind, rec.cause), ("mistake", "conflict"))
        self.assertTrue(rec.clashes)
        for (a, b) in rec.clashes:
            self.assertEqual(g.puzzle[a][b] or g._value_at(a, b), d)

    def test_a_wrong_digit_that_clashes_with_nothing_is_a_guess(self):
        g = make_game()
        r, c, d = cell_with_guess(g)
        g.move(r, c, d)
        self.assertEqual((g.moves[-1].kind, g.moves[-1].cause), ("mistake", "guess"))

    def test_clashing_only_with_your_own_wrong_entry_is_a_cascade(self):
        g = make_game()
        r, c, d = cell_with_guess(g)
        g.move(r, c, d)                               # wrong, clashes with nothing
        # find another empty cell sharing a unit with (r, c) and put the same digit there
        other = next(x for x in editable(g)
                     if x != (r, c) and set(tutor.units_of(x)) & set(tutor.units_of((r, c)))
                     and g.solution[x[0]][x[1]] != d)
        g.move(other[0], other[1], d)
        rec = g.moves[-1]
        self.assertEqual(rec.cause, "cascade")
        self.assertIn((r, c), rec.clashes)

    def test_correct_move_in_a_forced_cell_is_recorded_with_its_technique(self):
        g = make_game()
        known = g._known_board()
        forced = [(c, tutor.forced_technique(known, c, g.solution[c[0]][c[1]]))
                  for c in editable(g)]
        cell, (tech, unit) = next(x for x in forced if x[1][0])
        self.assertIn(tech, (tutor.NAKED, tutor.HIDDEN))
        g.move(cell[0], cell[1], g.solution[cell[0]][cell[1]])
        self.assertEqual(g.moves[-1].forced, tech)

    @staticmethod
    def oracle(board, cell, digit):
        """Plain-loop re-derivation of the two single techniques, independent of tutor.py."""
        def cands(r, c):
            seen = set()
            for k in range(9):
                seen.add(board[r][k]); seen.add(board[k][c])
            br, bc = r // 3 * 3, c // 3 * 3
            for a in range(br, br + 3):
                for b in range(bc, bc + 3):
                    seen.add(board[a][b])
            return [d for d in range(1, 10) if d not in seen]
        r, c = cell
        if cands(r, c) == [digit]:
            return "naked_single"
        row = [(r, k) for k in range(9)]
        col = [(k, c) for k in range(9)]
        box = [(a, b) for a in range(r // 3 * 3, r // 3 * 3 + 3) for b in range(c // 3 * 3, c // 3 * 3 + 3)]
        for unit in (row, col, box):
            if any(board[a][b] == digit for a, b in unit):
                continue
            if [x for x in unit if board[x[0]][x[1]] == 0 and digit in cands(*x)] == [cell]:
                return "hidden_single"
        return None

    def test_forced_technique_agrees_with_an_independent_derivation_on_real_boards(self):
        seen = set()
        for seed, level in ((1, "Easy"), (2, "Medium"), (3, "Hard"), (4, "Hard"), (9, "Medium")):
            g = make_game(level, seed=seed)
            known = g._known_board()
            for (r, c) in editable(g):
                for digit in range(1, 10):
                    got = tutor.forced_technique(known, (r, c), digit)[0]
                    want = self.oracle(known, (r, c), digit)
                    self.assertEqual(got, want, (level, seed, r, c, digit))
                    if digit == g.solution[r][c]:
                        seen.add(got)
        self.assertEqual(seen, {"naked_single", "hidden_single", None})        # all three outcomes were exercised

    def test_every_cell_of_an_easy_puzzle_is_a_single_at_some_point(self):
        """Easy is defined as 'naked singles only', so the analysis must agree."""
        g = make_game("Easy")
        guard = 0
        while editable(g) and any(g._value_at(r, c) == 0 for r, c in editable(g)):
            guard += 1
            self.assertLess(guard, 100)
            known = g._known_board()
            progressed = False
            for (r, c) in editable(g):
                if g._value_at(r, c):
                    continue
                tech, _ = tutor.forced_technique(known, (r, c), g.solution[r][c])
                if tech:
                    g.move(r, c, g.solution[r][c])
                    progressed = True
                    break
            self.assertTrue(progressed, "analysis found no single on an Easy board")
        self.assertEqual(g.status, gm.WON)

    def test_undo_marks_the_record_so_review_ignores_it(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, g.solution[r][c])
        g.undo()
        self.assertTrue(g.moves[-1].undone)

    def test_moves_are_recorded_even_in_play_mode_so_review_can_cover_them(self):
        g = make_game(mode="play")
        r, c = editable(g)[0]
        g.move(r, c, g.solution[r][c])
        self.assertEqual(len(g.moves), 1)
        self.assertEqual(g.tutor_messages(0), [])      # but nothing is said


# --------------------------------------------------------------------------- #
# Play mode vs Learn mode
# --------------------------------------------------------------------------- #
class TestModes(unittest.TestCase):
    def test_play_mode_has_no_coaching_and_refuses_the_tutor(self):
        g = make_game(mode="play")
        r, c = editable(g)[0]
        g.move(r, c, clash_digit(g, r, c))             # a mistake: silent in play mode
        g.hint()
        self.assertEqual(g.tutor_messages(0), [])
        with self.assertRaises(gm.GameError) as cm:
            ask(g, "overview")
        self.assertEqual(cm.exception.code, "play_mode")
        self.assertEqual(g.tutor_questions, 0)

    def test_switching_modes_changes_nothing_but_the_mode(self):
        clock = FakeClock()
        g = make_game(mode="play", clock=clock)
        a, b, c3 = editable(g)[:3]
        g.move(*a, g.solution[a[0]][a[1]])
        g.move(*b, clash_digit(g, *b))
        g.hint()
        clock.advance(10)
        before = snapshot(g)
        elapsed, rev_hint = g.elapsed(), g.hint_seq

        g.set_mode("learn")
        self.assertEqual(g.mode, "learn")
        for key, val in before.items():
            self.assertEqual(snapshot(g)[key], val, key)
        self.assertEqual(g.hint_seq, rev_hint)
        self.assertEqual(g.elapsed(), elapsed)

        g.set_mode("play")
        for key, val in before.items():
            self.assertEqual(snapshot(g)[key], val, key)
        self.assertTrue(g.learn_used)                  # remembered for the summary

    def test_the_clock_keeps_running_across_a_mode_switch(self):
        clock = FakeClock()
        g = make_game(mode="play", clock=clock)
        clock.advance(30)
        g.set_mode("learn")
        clock.advance(20)
        g.set_mode("play")
        clock.advance(5)
        self.assertEqual(int(g.elapsed()), 55)
        self.assertEqual(g.status, gm.PLAYING)

    def test_a_mode_switch_while_paused_neither_resumes_nor_changes_the_clock(self):
        clock = FakeClock()
        g = make_game(mode="play", clock=clock)
        clock.advance(12)
        g.pause()
        clock.advance(100)
        g.set_mode("learn")
        self.assertEqual(g.status, gm.PAUSED)
        clock.advance(100)
        self.assertEqual(int(g.elapsed()), 12)
        g.resume()
        clock.advance(8)
        self.assertEqual(int(g.elapsed()), 20)

    def test_tutor_is_refused_while_paused_and_nothing_is_logged_or_charged(self):
        g = make_game()
        g.pause()
        n = len(g.tutor_messages(0))
        with self.assertRaises(gm.GameError) as cm:
            ask(g, "next_move")
        self.assertEqual(cm.exception.code, "paused")
        self.assertEqual((len(g.tutor_messages(0)), g.hints_used, g.tutor_questions), (n, 0, 0))

    def test_unknown_modes_are_rejected(self):
        g = make_game()
        for bad in ("", "cheat", None, 1, "LEARN"):
            with self.subTest(mode=bad), self.assertRaises(gm.GameError):
                g.set_mode(bad)
        self.assertEqual(g.mode, "learn")


# --------------------------------------------------------------------------- #
# Reactions to moves
# --------------------------------------------------------------------------- #
class TestReactions(unittest.TestCase):
    def test_a_conflicting_move_names_the_clash_and_the_constraint(self):
        g = make_game()
        r, c = editable(g)[0]
        d = clash_digit(g, r, c)
        g.move(r, c, d)
        msg = last_tutor(g)
        self.assertEqual(msg["kind"], "feedback")
        self.assertIn(f"{d} can't go in {tutor.cell_label((r, c))}", msg["text"])
        self.assertIn(f"already has a {d}", msg["text"])
        self.assertEqual(msg["cells"][0], [r, c])
        self.assertGreater(len(msg["cells"]), 1)           # the clashing cell is highlighted too

    def test_reply_does_not_shame_the_player(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, clash_digit(g, r, c))
        low = last_tutor(g)["text"].lower()
        for word in ("stupid", "careless", "obviously", "idiot", "wrong again", "silly"):
            self.assertNotIn(word, low)

    def test_the_same_mistake_is_not_scolded_twice(self):
        g = make_game()
        r, c = editable(g)[0]
        d = clash_digit(g, r, c)
        g.move(r, c, d)
        n = len(g.tutor_messages(0))
        g.move(r, c, 0)
        self.assertEqual(g.move(r, c, d)["type"], "repeat_mistake")
        self.assertEqual(len(g.tutor_messages(0)), n)       # erase and repeat are silent

    def test_a_guess_is_explained_without_saying_which_digit_is_right(self):
        g = make_game()
        r, c, d = cell_with_guess(g)
        g.move(r, c, d)
        msg = last_tutor(g)
        self.assertIn("doesn't clash", msg["text"])
        self.assertFalse(tutor.mentions_digit(msg["text"].replace(str(d), "x"), g.solution[r][c]))

    def test_cascade_tells_the_player_to_fix_the_red_cell_first(self):
        g = make_game()
        r, c, d = cell_with_guess(g)
        g.move(r, c, d)
        other = next(x for x in editable(g)
                     if x != (r, c) and set(tutor.units_of(x)) & set(tutor.units_of((r, c)))
                     and g.solution[x[0]][x[1]] != d)
        g.move(other[0], other[1], d)
        self.assertIn("your own wrong entry", last_tutor(g)["text"])

    def test_first_naked_single_is_praised_once_and_explained(self):
        g = make_game()
        known = g._known_board()
        picks = [c for c in editable(g)
                 if tutor.forced_technique(known, c, g.solution[c[0]][c[1]])[0] == tutor.NAKED]
        self.assertTrue(picks)
        r, c = picks[0]
        g.move(r, c, g.solution[r][c])
        self.assertIn("naked single", last_tutor(g)["text"])
        count = len(g.tutor_messages(0))
        # a second naked single must not repeat the praise
        known = g._known_board()
        again = [x for x in editable(g) if not g._value_at(*x)
                 and tutor.forced_technique(known, x, g.solution[x[0]][x[1]])[0] == tutor.NAKED]
        if again:
            g.move(again[0][0], again[0][1], g.solution[again[0][0]][again[0][1]])
            self.assertEqual(len(g.tutor_messages(0)), count)

    def test_pattern_note_appears_once_after_repeated_conflicts(self):
        g = make_game()
        done = 0
        for r, c in editable(g):
            d = clash_digit(g, r, c)
            if d and not g._value_at(r, c):
                g.move(r, c, d)
                done += 1
            if done == 2:
                break
        self.assertEqual(done, 2)
        self.assertIn("Pattern: 2 of your 2 mistakes", texts(g.tutor_messages(0)))
        self.assertEqual(texts(g.tutor_messages(0)).count("Pattern:"), 1)

    def test_correct_unforced_entries_get_a_gentle_note_only_occasionally(self):
        """A run of unforced correct entries must not produce a message each time."""
        g = make_game("Hard", seed=3)
        known0 = g._known_board()
        unforced = [c for c in editable(g)
                    if tutor.forced_technique(known0, c, g.solution[c[0]][c[1]])[0] is None]
        if len(unforced) < 4:
            self.skipTest("this seed has too few unforced cells")
        before = len(g.tutor_messages(0))
        placed = 0
        for r, c in unforced[:6]:
            if tutor.forced_technique(g._known_board(), (r, c), g.solution[r][c])[0] is None:
                g.move(r, c, g.solution[r][c])
                placed += 1
        feedback = len(g.tutor_messages(0)) - before
        self.assertLess(feedback, placed)

    def test_winning_and_losing_post_a_review(self):
        g = make_game()
        for r, c in editable(g):
            g.move(r, c, g.solution[r][c])
        self.assertEqual(g.status, gm.WON)
        self.assertEqual(last_tutor(g)["kind"], "review")
        self.assertIn("Puzzle solved", last_tutor(g)["text"])

        g = make_game()
        r, c, d = cell_with_guess(g)
        for r, c in editable(g):
            if g.status != gm.PLAYING:
                break
            if guess_digit(g, r, c):
                g.move(r, c, guess_digit(g, r, c))
        self.assertEqual(g.status, gm.LOST)
        self.assertEqual(last_tutor(g)["kind"], "review")


# --------------------------------------------------------------------------- #
# Free explanations
# --------------------------------------------------------------------------- #
class TestFreeExplanations(unittest.TestCase):
    def test_explain_cell_uses_the_selected_cell_and_lists_what_its_units_hold(self):
        g = make_game()
        r, c = editable(g)[5]
        ask(g, "explain_cell", selected=[r, c])
        msg = last_tutor(g)
        self.assertIn(tutor.cell_label((r, c)), msg["text"])
        self.assertEqual(msg["cells"], [[r, c]])
        board = shown(g)
        for u, line in zip(tutor.units_of((r, c)), msg["steps"]):
            self.assertIn(hintlib.unit_label(u), line)
            for d in tutor.digits_in(board, u):
                self.assertIn(str(d), line)
            for d in set(range(1, 10)) - tutor.digits_in(board, u):
                self.assertNotIn(str(d), line.replace(hintlib.unit_label(u), ""))

    def test_explain_cell_without_a_selection_asks_for_one_and_costs_nothing(self):
        g = make_game()
        ask(g, "explain_cell")
        self.assertIn("Select a cell", last_tutor(g)["text"])
        self.assertEqual(g.hints_used, 0)

    def test_explain_cell_follows_a_correction(self):
        g = make_game()
        r, c = editable(g)[0]
        bad = clash_digit(g, r, c)
        g.move(r, c, bad)
        ask(g, "explain_cell", selected=[r, c])
        self.assertIn(f"{bad} can't go in", last_tutor(g)["text"])
        g.move(r, c, g.solution[r][c])                       # the player fixes it
        ask(g, "explain_cell", selected=[r, c])
        self.assertIn(f"{tutor.cell_label((r, c))} = {g.solution[r][c]}", last_tutor(g)["text"])
        self.assertIn("correct", last_tutor(g)["text"])

    def test_why_not_explains_a_real_clash_and_only_a_real_one(self):
        g = make_game()
        r, c = editable(g)[0]
        d = clash_digit(g, r, c)
        ask(g, "why_not", selected=[r, c], digit=d)
        text = last_tutor(g)["text"]
        self.assertIn(f"{d} can't go in {tutor.cell_label((r, c))}", text)
        # every cell the reply cites really holds d
        board = shown(g)
        clashes = tutor.find_clashes(board, (r, c), d)[0]
        self.assertTrue(clashes)
        for a, b in clashes[:2]:
            self.assertIn(tutor.cell_label((a, b)), text)
            self.assertEqual(board[a][b], d)

    def test_why_not_is_not_an_oracle_for_the_right_digit(self):
        """For an allowed digit the reply must be identical whether or not it is correct."""
        g = make_game()
        for r, c in editable(g)[:12]:
            ok = [d for d in range(1, 10) if d not in peers_digits(g, r, c)]
            if len(ok) < 2:
                continue
            right = g.solution[r][c]
            wrong = next(d for d in ok if d != right)
            ask(g, "why_not", selected=[r, c], digit=right)
            a = last_tutor(g)["text"]
            ask(g, "why_not", selected=[r, c], digit=wrong)
            b = last_tutor(g)["text"]
            self.assertEqual(a.replace(str(right), "#"), b.replace(str(wrong), "#"))
            return
        self.fail("no suitable cell found")

    def test_why_not_on_a_clue_or_without_selection_is_handled(self):
        g = make_game()
        r, c = next((r, c) for r in range(9) for c in range(9) if g.puzzle[r][c])
        ask(g, "why_not", selected=[r, c], digit=4)
        self.assertIn("locked", last_tutor(g)["text"])
        ask(g, "why_not", digit=4)
        self.assertIn("Select a cell", last_tutor(g)["text"])

    def test_missing_lists_exactly_the_digits_a_unit_lacks(self):
        g = make_game()
        for u in (0, 9, 18, 13, 26):
            ask(g, "missing", unit=u)
            steps = last_tutor(g)["steps"]
            present = tutor.digits_in(g._known_board(), u)
            needs = [s for s in steps if s.startswith("It still needs")][0]
            listed = {int(x) for x in needs if x.isdigit()}
            self.assertEqual(listed, set(range(1, 10)) - present)

    def test_missing_defaults_to_the_selected_cells_row(self):
        g = make_game()
        r, c = editable(g)[3]
        ask(g, "missing", selected=[r, c])
        self.assertIn(f"Row {r + 1}", texts([last_tutor(g)]))

    def test_why_wrong_finds_the_latest_wrong_entry_without_a_selection(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, clash_digit(g, r, c))
        ask(g, "why_wrong")
        self.assertIn(tutor.cell_label((r, c)), last_tutor(g)["text"])
        g.move(r, c, g.solution[r][c])
        ask(g, "why_wrong")
        self.assertIn("Nothing on your board is marked wrong", last_tutor(g)["text"])

    def test_why_wrong_ignores_a_selected_cell_that_is_not_wrong(self):
        g = make_game()
        clue = next((r, c) for r in range(9) for c in range(9) if g.puzzle[r][c])
        ask(g, "why_wrong", selected=list(clue))
        self.assertIn("Nothing on your board is marked wrong", last_tutor(g)["text"])
        r, c = editable(g)[0]
        ask(g, "why_wrong", selected=[r, c])                     # empty and fine
        self.assertIn("Nothing on your board is marked wrong", last_tutor(g)["text"])
        g.move(r, c, g.solution[r][c])
        ask(g, "why_wrong", selected=[r, c])                     # correct entry
        self.assertIn("Nothing on your board is marked wrong", last_tutor(g)["text"])
        # with a real mistake elsewhere, a clue selection still points at the mistake, not the clue
        r2, c2 = editable(g)[1]
        g.move(r2, c2, clash_digit(g, r2, c2))
        ask(g, "why_wrong", selected=list(clue))
        text = last_tutor(g)["text"]
        self.assertIn(tutor.cell_label((r2, c2)), text)
        self.assertNotIn(tutor.cell_label(clue) + ":", text)

    def test_better_puts_fixing_errors_first(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, clash_digit(g, r, c))
        ask(g, "better")
        self.assertTrue(last_tutor(g)["steps"][0].startswith("Fix first"))

    def test_overview_matches_the_difficulty_and_costs_nothing(self):
        for level, phrase in (("Easy", "naked singles alone"), ("Medium", "singles, naked and hidden"),
                              ("Hard", "Singles alone won't finish")):
            g = make_game(level)
            ask(g, "overview")
            self.assertIn(phrase, last_tutor(g)["text"])
            self.assertEqual(g.hints_used, 0)

    def test_review_counts_what_really_happened(self):
        g = make_game()
        known = g._known_board()
        singles = [c for c in editable(g)
                   if tutor.forced_technique(known, c, g.solution[c[0]][c[1]])[0]][:2]
        for r, c in singles:
            g.move(r, c, g.solution[r][c])
        r, c = next(x for x in editable(g) if not g._value_at(*x))
        g.move(r, c, clash_digit(g, r, c))
        ask(g, "review")
        steps = last_tutor(g)["steps"]
        self.assertIn(f"{len(singles)} correct entries", steps[0])
        self.assertIn("1 mistake", steps[1])

    def test_review_with_no_moves_says_so(self):
        g = make_game()
        ask(g, "review")
        self.assertIn("Nothing to review", last_tutor(g)["text"])

    def test_lesson_answers_are_graded_correctly(self):
        g = make_game()
        q = lessons.LESSONS["hidden_single"]["quiz"]
        ask(g, "lesson_answer", name="hidden_single", choice=q["answer"])
        self.assertTrue(last_tutor(g)["text"].startswith("Correct."))
        wrong = (q["answer"] + 1) % len(q["options"])
        ask(g, "lesson_answer", name="hidden_single", choice=wrong)
        self.assertTrue(last_tutor(g)["text"].startswith("Not quite."))
        self.assertIn(q["options"][q["answer"]], last_tutor(g)["text"])

    def test_asking_to_solve_is_refused_kindly_and_points_to_hints(self):
        g = make_game()
        before = snapshot(g)
        ask(g, text="just give me the answer")
        msg = last_tutor(g)
        self.assertIn("won't fill the board", msg["text"])
        self.assertEqual(snapshot(g), before)


# --------------------------------------------------------------------------- #
# The assistance rule: what is free, what costs a hint, what is never given
# --------------------------------------------------------------------------- #
class TestAssistanceRule(unittest.TestCase):
    def test_free_intents_never_spend_a_hint_or_touch_the_board(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, clash_digit(g, r, c))
        before = snapshot(g)
        for intent, params in FREE_INTENTS * 3:
            for sel in (None, [r, c], [0, 0], [8, 8]):
                ask(g, intent, selected=sel, **params)
        for q in ("tell me a joke", "give me the answer", "hello", "explain this cell"):
            ask(g, text=q, selected=[r, c])
        self.assertEqual(snapshot(g), before)
        self.assertEqual(g.hints_used, 0)

    def test_next_move_is_a_real_hint_and_spends_the_shared_allowance(self):
        g = make_game("Easy")
        left = g.rules.max_hints
        ev = ask(g, "next_move")
        self.assertEqual(ev["cost"], 1)
        self.assertEqual(g.hints_used, 1)
        self.assertEqual(last_tutor(g)["kind"], "hint")
        self.assertEqual(last_tutor(g)["note"], f"{left - 1} hints left")
        # the Hint button and the tutor draw on the same pool
        g.hint()
        self.assertEqual(g.hints_used, 2)

    def test_the_tutors_hint_is_the_hint_buttons_hint(self):
        a, b = make_game("Medium", seed=5), make_game("Medium", seed=5)
        a.hint()
        ask(b, "next_move")
        self.assertEqual(a._active_hint[0].cell, b._active_hint[0].cell)
        self.assertEqual(last_tutor(b)["text"], hintlib.describe(a._active_hint[0], 1))

    def test_asking_repeatedly_cannot_exceed_the_allowance(self):
        g = make_game("Hard")
        allowance = g.rules.max_hints
        for _ in range(allowance + 15):
            try:
                ask(g, "next_move")
            except gm.GameError:
                pass
            if g.status != gm.PLAYING:
                break
        self.assertLessEqual(g.hints_used, allowance)
        self.assertEqual(g.hints_used, allowance)
        n = len(g.tutor_messages(0))
        ask(g, "next_move")                                 # none left
        self.assertEqual(g.hints_used, allowance)
        self.assertIn("used every hint", last_tutor(g)["text"])
        self.assertGreater(len(g.tutor_messages(0)), n)

    def test_typed_requests_for_a_hint_cost_the_same_as_the_button(self):
        g = make_game()
        for phrase in ("what should I do next?", "give me a hint", "I'm stuck"):
            before = g.hints_used
            ask(g, text=phrase)
            self.assertEqual(g.hints_used, before + 1, phrase)

    def test_a_replayed_next_move_request_is_not_charged_twice(self):
        g = make_game()
        seq = g.hint_seq
        g.tutor_ask(intent="next_move", params=None, seq=seq)
        used = g.hints_used
        again = g.tutor_ask(intent="next_move", params=None, seq=seq)      # stale duplicate
        self.assertTrue(again.get("stale"))
        self.assertEqual(g.hints_used, used)
        self.assertEqual([m["text"] for m in g.tutor_messages(0) if m["role"] == "user"].count(
            "next_move"), 1)

    def test_free_replies_about_empty_cells_never_state_their_digit(self):
        """The core no-spoiler property, checked on every empty cell of two puzzles."""
        for seed, level in ((1, "Easy"), (7, "Hard")):
            g = make_game(level, seed=seed)
            for (r, c) in editable(g):
                sol = g.solution[r][c]
                label = tutor.cell_label((r, c))
                placed = peers_digits(g, r, c)
                self.assertNotIn(sol, placed)
                for intent, params in (("explain_cell", {}), ("why_wrong", {}),
                                       ("better", {}), ("overview", {}),
                                       ("why_not", {"digit": (sol % 9) + 1})):
                    ask(g, intent, selected=[r, c], **params)
                    body = texts([last_tutor(g)])
                    # No statement of the form "R3C5 = 7" / "R3C5 is 7" for this cell.
                    self.assertNotIn(f"{label} = {sol}", body)
                    self.assertNotIn(f"{label} is {sol}", body)
                ask(g, "explain_cell", selected=[r, c])
                self.assertFalse(tutor.mentions_digit(texts([last_tutor(g)]), sol), (level, label))
                for line in last_tutor(g)["steps"][:3]:
                    listed = {int(ch) for ch in line.split(" already has ")[1] if ch.isdigit()}
                    self.assertTrue(listed <= placed, (level, label, line))
                    self.assertNotIn(sol, listed)

    def test_every_suggestion_that_would_spend_a_hint_says_so(self):
        g = make_game("Medium", seed=2)
        messages = list(g.tutor_messages(0))
        for intent, params in FREE_INTENTS:
            ask(g, intent, **params)
        ask(g, text="give me the answer")
        for _ in range(3):
            ask(g, "next_move")
        messages = g.tutor_messages(0)
        seen = 0
        for m in messages:
            for chip in m.get("chips") or []:
                spends = chip["intent"] == "next_move"
                self.assertEqual("uses a hint" in chip["label"], spends, chip["label"])
                seen += spends
        self.assertGreater(seen, 3)

    def test_level_one_and_two_hints_do_not_name_the_digit(self):
        g = make_game("Medium", seed=2)
        ask(g, "next_move")
        sol = lambda: g.solution[g._active_hint[0].cell[0]][g._active_hint[0].cell[1]]
        self.assertFalse(tutor.mentions_digit(last_tutor(g)["text"], sol()))
        ask(g, "next_move")
        # level 2 names the cell and technique but not the digit
        self.assertFalse(tutor.mentions_digit(last_tutor(g)["text"], sol()))
        self.assertEqual(last_tutor(g)["level"], 2)
        self.assertNotIn(g._active_hint[0].cell, g.revealed)
        ask(g, "next_move")
        self.assertIn(g._active_hint[0].cell, g.revealed)             # only level 3 reveals

    def test_no_sequence_of_tutor_questions_fills_the_board(self):
        g = make_game("Easy")
        for _ in range(3):
            for intent, params in FREE_INTENTS:
                for r, c in editable(g)[:20]:
                    ask(g, intent, selected=[r, c], **params)
        self.assertEqual(sum(1 for r, c in editable(g) if g._value_at(r, c)), 0)
        self.assertEqual(g.revealed, {})
        self.assertEqual(g.status, gm.PLAYING)

    def test_clues_cannot_be_changed_through_the_tutor_or_the_game(self):
        g = make_game()
        clues = [(r, c) for r in range(9) for c in range(9) if g.puzzle[r][c]]
        for r, c in clues[:10]:
            ask(g, "explain_cell", selected=[r, c])
            self.assertIn("printed clue", last_tutor(g)["text"])
            with self.assertRaises(gm.GameError):
                g.move(r, c, (g.puzzle[r][c] % 9) + 1)
        self.assertEqual(g.puzzle, make_game().puzzle)

    def test_open_facts_for_the_ai_never_contain_the_solution_or_the_board(self):
        g = make_game()
        r, c = editable(g)[0]
        facts = g.tutor_open_facts([r, c])
        blob = json.dumps(facts)
        self.assertNotIn("".join(str(x) for x in g.solution[0]), blob)
        self.assertNotIn("board", facts)
        self.assertNotIn("candidates", blob)
        self.assertNotIn("solution", blob)
        self.assertNotIn(g.solution[r][c], [x for v in facts.get("digits_already_in_its_units", {}).values() for x in v])


# --------------------------------------------------------------------------- #
# Contradictory and awkward boards
# --------------------------------------------------------------------------- #
class TestAwkwardBoards(unittest.TestCase):
    def contradictory(self):
        g = make_game()
        r, c, d = cell_with_guess(g)
        g.move(r, c, d)
        other = next(x for x in editable(g)
                     if x != (r, c) and set(tutor.units_of(x)) & set(tutor.units_of((r, c)))
                     and g.solution[x[0]][x[1]] != d)
        g.move(other[0], other[1], d)                         # two equal digits in one unit
        return g, (r, c), other, d

    def test_every_tutor_request_survives_a_board_with_duplicates(self):
        g, a, b, d = self.contradictory()
        self.assertEqual(len(find_dups(shown(g))), 1)
        for intent, params in FREE_INTENTS:
            for r, c in (a, b, (0, 0), (8, 8)):
                ask(g, intent, selected=[r, c], **params)
                self.assertTrue(last_tutor(g)["text"])
        for u in range(27):
            ask(g, "missing", unit=u)

    def test_hints_on_a_contradictory_board_ignore_the_wrong_entries(self):
        g, a, b, d = self.contradictory()
        ask(g, "next_move")
        target = g._active_hint[0].cell
        self.assertNotIn(target, (a, b))
        self.assertTrue(g.solution[target[0]][target[1]])

    def test_explain_cell_on_a_wrong_entry_blames_the_right_thing(self):
        g, a, b, d = self.contradictory()
        ask(g, "explain_cell", selected=list(b))
        text = last_tutor(g)["text"]
        self.assertIn("your own wrong entry", text)

    def test_tutor_still_works_after_the_game_is_lost(self):
        g = make_game()
        for r, c in editable(g):
            if g.status != gm.PLAYING:
                break
            if guess_digit(g, r, c):
                g.move(r, c, guess_digit(g, r, c))
        self.assertEqual(g.status, gm.LOST)
        used = g.hints_used
        ask(g, "next_move")
        self.assertIn("game is over", last_tutor(g)["text"])
        self.assertEqual(g.hints_used, used)                  # nothing charged after the end
        ask(g, "review")
        self.assertEqual(last_tutor(g)["kind"], "review")
        ask(g, "lesson", name="scanning")
        self.assertEqual(last_tutor(g)["kind"], "lesson")
        ask(g, "explain_cell", selected=[0, 0])
        self.assertIn("game is over", last_tutor(g)["text"])

    def test_tutor_after_a_win(self):
        g = make_game()
        for r, c in editable(g):
            g.move(r, c, g.solution[r][c])
        ask(g, "review")
        self.assertIn("Puzzle solved", last_tutor(g)["text"])

    def test_junk_selected_values_are_ignored(self):
        g = make_game()
        for sel in (None, [], [1], ["a", "b"], [99, 99], [-1, 0], "x", {"r": 1}, [True, 1.5], [None, None]):
            with self.subTest(sel=sel):
                ask(g, "explain_cell", selected=sel)
                self.assertIn("Select a cell", last_tutor(g)["text"])


def find_dups(board):
    out = []
    for u in range(27):
        vals = [board[r][c] for r, c in sl.UNITS[u] if board[r][c]]
        out += [v for v in set(vals) if vals.count(v) > 1]
    return out


# --------------------------------------------------------------------------- #
# Chat housekeeping and input validation
# --------------------------------------------------------------------------- #
class TestChat(unittest.TestCase):
    def test_chat_starts_with_an_intro_that_offers_choices(self):
        g = make_game()
        first = g.tutor_messages(0)[0]
        self.assertEqual(first["role"], "tutor")
        self.assertTrue(first["chips"])
        self.assertEqual(len(g.tutor_messages(0)), 1)

    def test_messages_have_increasing_ids_and_after_filters(self):
        g = make_game()
        ask(g, "help")
        ask(g, "overview")
        ids = [m["id"] for m in g.tutor_messages(0)]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertEqual([m["id"] for m in g.tutor_messages(ids[2])], ids[3:])
        self.assertEqual(g.tutor_messages(None), [])

    def test_clearing_the_chat_leaves_the_puzzle_alone(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, clash_digit(g, r, c))
        g.hint()
        before = snapshot(g)
        ask(g, "overview")
        g.tutor_clear()
        self.assertEqual(snapshot(g), before)
        msgs = g.tutor_messages(0)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "tutor")

    def test_the_log_is_capped(self):
        g = make_game()
        for _ in range(gm.MAX_TUTOR_LOG):
            ask(g, "help")
        self.assertLessEqual(len(g.tutor_messages(0)), gm.MAX_TUTOR_LOG)
        self.assertEqual(last_tutor(g)["kind"], "info")

    def test_bad_input_is_rejected_with_400_and_logs_nothing(self):
        g = make_game()
        n = len(g.tutor_messages(0))
        for kw in ({"text": ""}, {"text": "   "}, {"text": None}, {"text": "x" * 301},
                   {"text": 123}, {"intent": "cheat"}, {"intent": "unknown"},
                   {"intent": "refuse_solve"}, {"intent": "why_not"},
                   {"intent": "why_not", "params": {"digit": 12}}):
            with self.subTest(kw=kw), self.assertRaises(gm.GameError) as cm:
                g.tutor_ask(**kw)
            self.assertEqual(cm.exception.http_status, 400)
        self.assertEqual(len(g.tutor_messages(0)), n)
        self.assertEqual(g.tutor_questions, 0)

    def test_summary_records_learn_usage_separately_from_hints(self):
        g = make_game(mode="play")
        for r, c in editable(g):
            g.move(r, c, g.solution[r][c])
        self.assertFalse(g.summary["learn_used"])
        self.assertEqual(g.summary["tutor_questions"], 0)

        g = make_game(mode="play")
        g.set_mode("learn")
        ask(g, "overview")
        ask(g, "next_move")
        g.set_mode("play")
        for r, c in editable(g):
            if not g._value_at(r, c):
                g.move(r, c, g.solution[r][c])
        self.assertTrue(g.summary["learn_used"])
        self.assertEqual(g.summary["tutor_questions"], 2)
        self.assertEqual(g.summary["hints_used"], 1)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
SECRET = "sk-ant-TEST-SECRET-0123456789"


def ok_transport(text="A friendly rewording."):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout})
        return 200, json.dumps({"content": [{"type": "text", "text": text}]}).encode()
    transport.calls = calls
    return transport


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        self._llm = web.LLM
        web.LLM = None

    def tearDown(self):
        web.LLM = self._llm

    def post(self, path, **body):
        resp = self.client.post(path, json=body)
        return resp.status_code, resp.get_json()

    def new(self, mode="learn", level="Easy", **extra):
        status, body = self.post("/api/new", difficulty=level, mode=mode, **extra)
        self.assertEqual(status, 200, body)
        self.last = body["tutor"][-1]["id"] if body["tutor"] else 0
        return body

    def game(self, body):
        return web.STORE.get(body["state"]["game_id"])

    def tutor(self, body, **kw):
        kw.setdefault("tutor_after", self.last)
        status, out = self.post("/api/tutor", game_id=body["state"]["game_id"], **kw)
        if status == 200 and out["tutor"]:
            self.last = out["tutor"][-1]["id"]
        return status, out


class TestApi(ApiCase):
    def test_new_learn_game_comes_with_the_intro_and_mode(self):
        body = self.new()
        self.assertEqual(body["state"]["mode"], "learn")
        self.assertEqual(len(body["tutor"]), 1)
        self.assertTrue(body["state"]["learn_used"])

    def test_new_play_game_has_no_tutor_traffic(self):
        body = self.new(mode="play")
        self.assertEqual(body["state"]["mode"], "play")
        self.assertEqual(body["tutor"], [])
        status, out = self.post("/api/tutor", game_id=body["state"]["game_id"], intent="help")
        self.assertEqual((status, out["code"]), (409, "play_mode"))
        self.assertIn("state", out)

    def test_default_mode_is_play_and_bad_modes_are_rejected(self):
        status, body = self.post("/api/new", difficulty="Easy")
        self.assertEqual(body["state"]["mode"], "play")
        self.assertEqual(self.post("/api/new", difficulty="Easy", mode="god")[0], 400)

    def test_retry_keeps_the_mode(self):
        body = self.new()
        status, again = self.post("/api/new", retry_of=body["state"]["game_id"])
        self.assertEqual(again["state"]["mode"], "learn")
        self.assertEqual(again["state"]["puzzle"], body["state"]["puzzle"])

    def test_mode_endpoint_switches_without_touching_progress(self):
        body = self.new(mode="play")
        gid = body["state"]["game_id"]
        sol = self.game(body).solution
        r, c = next((r, c) for r in range(9) for c in range(9) if not body["state"]["puzzle"][r][c])
        self.post("/api/move", game_id=gid, row=r, col=c, value=sol[r][c])
        self.post("/api/hint", game_id=gid)
        s1 = self.client.get(f"/api/state?game_id={gid}").get_json()["state"]
        status, out = self.post("/api/mode", game_id=gid, mode="learn")
        self.assertEqual(status, 200)
        s2 = out["state"]
        for key in ("board", "mistakes", "hints_used", "hints_left", "status", "wrong", "puzzle",
                    "max_hints", "max_mistakes", "difficulty", "game_id"):
            self.assertEqual(s1[key], s2[key], key)
        self.assertEqual(s2["mode"], "learn")
        self.assertEqual(self.post("/api/mode", game_id=gid, mode="bogus")[0], 400)
        self.assertEqual(self.post("/api/mode", game_id="nope", mode="play")[0], 404)

    def test_tutor_replies_ride_along_on_moves_using_tutor_after(self):
        body = self.new()
        gid = body["state"]["game_id"]
        g = self.game(body)
        r, c = editable(g)[0]
        d = clash_digit(g, r, c)
        status, out = self.post("/api/move", game_id=gid, row=r, col=c, value=d, tutor_after=self.last)
        self.assertEqual(status, 200)
        self.assertEqual([m["kind"] for m in out["tutor"]], ["feedback"])
        self.last = out["tutor"][-1]["id"]
        status, out = self.post("/api/move", game_id=gid, row=r, col=c, value=0, tutor_after=self.last)
        self.assertEqual(out["tutor"], [])                         # nothing new, nothing sent
        status, out = self.post("/api/move", game_id=gid, row=r, col=c, value=g.solution[r][c])
        self.assertEqual(out["tutor"], [])                         # no tutor_after -> none sent

    def test_tutor_receives_the_latest_board_after_a_correction(self):
        body = self.new()
        gid, g = body["state"]["game_id"], self.game(body)
        r, c = editable(g)[0]
        bad = clash_digit(g, r, c)
        self.post("/api/move", game_id=gid, row=r, col=c, value=bad)
        _, out = self.tutor(body, intent="explain_cell", selected=[r, c])
        self.assertIn(tutor.cell_label((r, c)), out["event"]["reply"]["text"])
        self.assertIn(f"{bad} can't go in", out["event"]["reply"]["text"])
        self.post("/api/move", game_id=gid, row=r, col=c, value=g.solution[r][c])
        _, out = self.tutor(body, intent="explain_cell", selected=[r, c])
        self.assertIn(f"{tutor.cell_label((r, c))} = {g.solution[r][c]}", out["event"]["reply"]["text"])

    def test_tutor_next_move_spends_a_hint_visible_in_state(self):
        body = self.new()
        status, out = self.tutor(body, intent="next_move", label="Help me find my next move",
                                 seq=body["state"]["hint_seq"])
        self.assertEqual(status, 200)
        self.assertEqual(out["state"]["hints_used"], 1)
        self.assertEqual(out["event"]["cost"], 1)
        kinds = [m["kind"] for m in out["tutor"]]
        self.assertEqual(kinds, ["user", "hint"])
        self.assertEqual(out["state"]["active_hint"]["level"], 1)

    def test_replayed_next_move_is_not_charged_and_adds_nothing(self):
        body = self.new()
        seq = body["state"]["hint_seq"]
        self.tutor(body, intent="next_move", seq=seq)
        status, out = self.tutor(body, intent="next_move", seq=seq)
        self.assertEqual(status, 200)
        self.assertEqual(out["state"]["hints_used"], 1)
        self.assertEqual(out["tutor"], [])
        self.assertIsNone(out["event"]["reply"])

    def test_paused_game_refuses_the_tutor(self):
        body = self.new()
        gid = body["state"]["game_id"]
        self.post("/api/pause", game_id=gid)
        status, out = self.post("/api/tutor", game_id=gid, intent="help")
        self.assertEqual((status, out["code"]), (409, "paused"))
        self.assertFalse(out["state"]["running"])

    def test_history_and_clear(self):
        body = self.new()
        gid = body["state"]["game_id"]
        self.tutor(body, intent="overview")
        status, out = self.client.get(f"/api/tutor/history?game_id={gid}").status_code, \
            self.client.get(f"/api/tutor/history?game_id={gid}").get_json()
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(out["messages"]), 3)
        self.assertEqual(out["ai"], {"enabled": False, "model": None, "provider": None})
        status, out = self.post("/api/tutor/clear", game_id=gid, tutor_after=0)
        self.assertEqual(len(out["tutor"]), 1)
        self.assertEqual(out["state"]["mistakes"], 0)

    def test_the_solution_never_appears_in_any_tutor_response(self):
        body = self.new()
        gid, g = body["state"]["game_id"], self.game(body)
        sol_rows = ["".join(map(str, row)) for row in g.solution]
        blobs = [json.dumps(body)]
        for intent, params in FREE_INTENTS:
            _, out = self.tutor(body, intent=intent, params=params, selected=[0, 0])
            blobs.append(json.dumps(out))
        _, out = self.tutor(body, intent="next_move", seq=body["state"]["hint_seq"])
        blobs.append(json.dumps(out))
        blob = "\n".join(blobs)
        self.assertNotIn("solution", blob)
        for row in sol_rows:
            self.assertNotIn(row, blob.replace(", ", "").replace(",", "").replace("[", "").replace("]", "").replace(" ", ""))

    def test_missing_game_gives_404(self):
        self.assertEqual(self.post("/api/tutor", game_id="nope", intent="help")[0], 404)

    def test_oversized_requests_are_refused(self):
        body = self.new()
        status, _ = self.post("/api/tutor", game_id=body["state"]["game_id"], text="x" * 20000)
        self.assertEqual(status, 413)


# --------------------------------------------------------------------------- #
# Optional AI wording (fake transport only)
# --------------------------------------------------------------------------- #
class TestLLMAdapter(unittest.TestCase):
    def test_off_without_a_key_and_when_disabled(self):
        self.assertIsNone(tutor_llm.from_env({}))
        self.assertIsNone(tutor_llm.from_env({"ANTHROPIC_API_KEY": "  "}))
        self.assertIsNone(tutor_llm.from_env({"ANTHROPIC_API_KEY": SECRET, "TUTOR_AI": "off"}))
        client = tutor_llm.from_env({"ANTHROPIC_API_KEY": SECRET, "TUTOR_MODEL": "m-1"})
        self.assertEqual(client.model, "m-1")
        self.assertEqual(tutor_llm.from_env({"ANTHROPIC_API_KEY": SECRET}).model, tutor_llm.DEFAULT_MODEL)

    def test_the_key_stays_out_of_repr_and_status(self):
        client = tutor_llm.Client(SECRET)
        self.assertNotIn(SECRET, repr(client))
        self.assertNotIn(SECRET, json.dumps(tutor_llm.status(client)))
        self.assertEqual(tutor_llm.status(None), {"enabled": False, "model": None, "provider": None})

    def test_request_shape(self):
        t = ok_transport()
        tutor_llm.Client(SECRET, "m-1", t).complete("sys", "hello")
        call = t.calls[0]
        self.assertEqual(call["url"], tutor_llm.API_URL)
        self.assertEqual(call["headers"]["x-api-key"], SECRET)
        self.assertEqual(call["body"]["model"], "m-1")
        self.assertEqual(call["body"]["messages"], [{"role": "user", "content": "hello"}])
        self.assertLessEqual(call["timeout"], 10)

    # ---- OpenAI as the second provider (fake transport only: the endpoint is not reachable here) ----
    def test_provider_is_chosen_from_whichever_key_is_set(self):
        self.assertEqual(tutor_llm.from_env({"OPENAI_API_KEY": SECRET}).provider, "openai")
        self.assertEqual(tutor_llm.from_env({"ANTHROPIC_API_KEY": SECRET}).provider, "anthropic")
        both = {"ANTHROPIC_API_KEY": SECRET, "OPENAI_API_KEY": SECRET + "x"}
        self.assertEqual(tutor_llm.from_env(both).provider, "anthropic")                # default order
        self.assertEqual(tutor_llm.from_env({**both, "TUTOR_PROVIDER": "openai"}).provider, "openai")
        self.assertIsNone(tutor_llm.from_env({"ANTHROPIC_API_KEY": SECRET, "TUTOR_PROVIDER": "openai"}))
        self.assertIsNone(tutor_llm.from_env({"OPENAI_API_KEY": SECRET, "TUTOR_PROVIDER": "nonsense"}))
        self.assertIsNone(tutor_llm.from_env({"OPENAI_API_KEY": SECRET, "TUTOR_AI": "off"}))
        self.assertEqual(tutor_llm.from_env({"OPENAI_API_KEY": SECRET}).model, "gpt-4o-mini")
        self.assertEqual(tutor_llm.from_env({"OPENAI_API_KEY": SECRET, "TUTOR_MODEL": "gpt-x"}).model, "gpt-x")

    def test_dotenv_file_is_read_without_overriding_real_variables(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write("# comment\n\nOPENAI_API_KEY=\"sk-from-file\"\nexport TUTOR_MODEL='m-9'\n"
                         "TUTOR_PROVIDER=openai # trailing note\nBROKEN LINE\nKEEP=file\n")
            env = {"KEEP": "real"}
            self.assertTrue(tutor_llm.load_dotenv(path, env))
            self.assertEqual(env, {"KEEP": "real", "OPENAI_API_KEY": "sk-from-file",
                                   "TUTOR_MODEL": "m-9", "TUTOR_PROVIDER": "openai"})
            client = tutor_llm.from_env(env)
            self.assertEqual((client.provider, client.model), ("openai", "m-9"))
            self.assertFalse(tutor_llm.load_dotenv(os.path.join(d, "missing.env"), {}))
            self.assertFalse(tutor_llm.load_dotenv(d, {}))                  # a directory is not a file

    def test_the_env_file_is_never_served_and_the_example_holds_no_key(self):
        client = web.app.test_client()
        for path in ("/.env", "/static/.env", "/static/../.env", "/.env.example", "/app.py"):
            self.assertEqual(client.get(path).status_code, 404, path)
        import pathlib
        example = pathlib.Path(__file__).with_name(".env.example").read_text()
        for line in example.splitlines():
            if line.startswith(("OPENAI_API_KEY=", "ANTHROPIC_API_KEY=", "GEMINI_API_KEY=",
                                "GOOGLE_API_KEY=")):
                self.assertEqual(line.split("=", 1)[1].strip(), "")

    def test_openai_request_shape_and_reply_parsing(self):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body), timeout))
            return 200, json.dumps({"choices": [{"message": {"role": "assistant", "content": " Hello there. "}}]}).encode()
        client = tutor_llm.Client(SECRET, "gpt-x", transport, provider="openai")
        self.assertEqual(client.complete("be kind", "hi"), "Hello there.")
        url, headers, body, timeout = calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["authorization"], f"Bearer {SECRET}")
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(body["model"], "gpt-x")
        self.assertEqual(body["messages"], [{"role": "system", "content": "be kind"},
                                            {"role": "user", "content": "hi"}])
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)
        self.assertLessEqual(timeout, 10)
        self.assertNotIn(SECRET, repr(client))
        self.assertNotIn(SECRET, json.dumps(tutor_llm.status(client)))
        self.assertEqual(tutor_llm.status(client)["provider"], "openai")

    def test_openai_errors_and_odd_replies_raise_llmerror(self):
        for status, raw in ((401, b"{}"), (429, b""), (500, b"x"), (200, b"not json"), (200, b'{"choices": []}'),
                            (200, b'{"choices": [{"message": {}}]}'), (200, b'{"choices": 5}')):
            client = tutor_llm.Client(SECRET, transport=lambda *a, st=status, r=raw: (st, r), provider="openai")
            with self.subTest(status=status, raw=raw), self.assertRaises(tutor_llm.LLMError):
                client.complete("s", "u")

    def test_the_same_output_guard_protects_openai_answers(self):
        client = tutor_llm.Client(SECRET, transport=lambda *a: (200, json.dumps(
            {"choices": [{"message": {"content": "R3C5 = 7"}}]}).encode()), provider="openai")
        self.assertIsNone(tutor_llm.reword(client, "Row 3 has 1, 2.", "why"))
        self.assertIsNone(tutor_llm.answer_open(client, "what goes in R3C5", {"difficulty": "Easy"}))

    # ---- Gemini as the third provider (fake transport only: the endpoint is not reachable here) ----
    def test_gemini_is_chosen_from_its_key_and_model_defaults(self):
        self.assertEqual(tutor_llm.from_env({"GEMINI_API_KEY": SECRET}).provider, "gemini")
        self.assertEqual(tutor_llm.from_env({"GOOGLE_API_KEY": SECRET}).provider, "gemini")
        self.assertEqual(tutor_llm.from_env({"GEMINI_API_KEY": SECRET}).model, "gemini-3.5-flash-lite")
        self.assertEqual(tutor_llm.from_env({"GEMINI_API_KEY": SECRET, "TUTOR_MODEL": "gemini-x"}).model,
                         "gemini-x")
        self.assertEqual(tutor_llm.from_env({"GEMINI_API_KEY": SECRET, "TUTOR_MODEL": "models/gemini-x"}).model,
                         "gemini-x")
        many = {"OPENAI_API_KEY": SECRET + "o", "GEMINI_API_KEY": SECRET}
        self.assertEqual(tutor_llm.from_env(many).provider, "openai")                  # default order
        self.assertEqual(tutor_llm.from_env({**many, "TUTOR_PROVIDER": "gemini"}).provider, "gemini")
        self.assertIsNone(tutor_llm.from_env({"OPENAI_API_KEY": SECRET, "TUTOR_PROVIDER": "gemini"}))
        self.assertIsNone(tutor_llm.from_env({"GEMINI_API_KEY": SECRET, "TUTOR_AI": "off"}))
        self.assertIsNone(tutor_llm.from_env({"GEMINI_API_KEY": "   "}))

    def test_a_bad_gemini_model_name_turns_ai_off_instead_of_crashing(self):
        for bad in ("a/b", "x?key=1", "../x", "a b", "gemini:generateContent#"):
            with self.subTest(model=bad):
                self.assertIsNone(tutor_llm.from_env({"GEMINI_API_KEY": SECRET, "TUTOR_MODEL": bad}))

    def test_gemini_request_shape_and_reply_parsing(self):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body), timeout))
            return 200, json.dumps({"candidates": [{"content": {"role": "model", "parts": [
                {"text": "secret reasoning", "thought": True},
                {"text": " Hello "}, {"text": "there. "}]}}]}).encode()
        client = tutor_llm.Client(SECRET, "gemini-x", transport, provider="gemini")
        self.assertEqual(client.complete("be kind", "hi"), "Hello there.")
        url, headers, body, timeout = calls[0]
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent")
        self.assertEqual(headers["x-goog-api-key"], SECRET)
        self.assertNotIn(SECRET, url)                                  # the key is never in the URL
        self.assertNotIn("authorization", headers)
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "be kind"}]})
        self.assertEqual(body["contents"], [{"role": "user", "parts": [{"text": "hi"}]}])
        self.assertGreaterEqual(body["generationConfig"]["maxOutputTokens"], 1000)
        self.assertNotIn("thinkingConfig", body["generationConfig"])   # some models reject it
        self.assertLessEqual(timeout, 10)
        self.assertNotIn(SECRET, repr(client))
        self.assertNotIn(SECRET, json.dumps(tutor_llm.status(client)))
        self.assertEqual(tutor_llm.status(client),
                         {"enabled": True, "model": "gemini-x", "provider": "gemini"})

    def test_gemini_errors_and_odd_replies_raise_llmerror(self):
        cases = ((400, b"{}"), (401, b""), (403, b"x"), (404, b"{}"), (429, b'{"error": {"message": "quota"}}'),
                 (500, b"x"), (200, b"not json"), (200, b"{}"), (200, b'{"candidates": []}'),
                 (200, b'{"candidates": [{}]}'),                                   # blocked: no content
                 (200, b'{"candidates": [{"content": {}}]}'),
                 (200, b'{"candidates": [{"content": {"parts": 5}}]}'),
                 (200, b'{"promptFeedback": {"blockReason": "SAFETY"}}'))
        for status, raw in cases:
            client = tutor_llm.Client(SECRET, transport=lambda *a, st=status, r=raw: (st, r), provider="gemini")
            with self.subTest(status=status, raw=raw), self.assertRaises(tutor_llm.LLMError):
                client.complete("s", "u")

    def test_a_reply_that_is_all_thinking_gives_no_text(self):
        raw = json.dumps({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [
            {"text": "thinking...", "thought": True}]}}]}).encode()
        client = tutor_llm.Client(SECRET, transport=lambda *a: (200, raw), provider="gemini")
        self.assertEqual(client.complete("s", "u"), "")
        self.assertIsNone(tutor_llm.reword(client, "Row 3 has 1, 2.", "why"))        # falls back

    def test_error_detail_helps_diagnosis_but_never_leaks_the_key(self):
        def fail(body):
            return tutor_llm.Client(SECRET, transport=lambda *a: (429, body), provider="gemini")
        with self.assertRaises(tutor_llm.LLMError) as cm:
            fail(json.dumps({"error": {"code": 429, "message": "Quota  exceeded\nfor this project"}}).encode()
                 ).complete("s", "u")
        self.assertEqual(str(cm.exception), "HTTP 429: Quota exceeded for this project")
        with self.assertRaises(tutor_llm.LLMError) as cm:
            fail(json.dumps({"error": {"message": f"bad key {SECRET} given " + "x" * 500}}).encode()
                 ).complete("s", "u")
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertLessEqual(len(str(cm.exception)), 220)
        for body in (b"", b"not json", b'{"error": 5}', b'{"error": {}}', b"[]"):
            with self.subTest(body=body), self.assertRaises(tutor_llm.LLMError) as cm:
                fail(body).complete("s", "u")
            self.assertEqual(str(cm.exception), "HTTP 429" if body != b'{"error": 5}' else "HTTP 429: 5")

    def test_the_same_output_guard_protects_gemini_answers(self):
        client = tutor_llm.Client(SECRET, transport=lambda *a: (200, json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "R3C5 = 7"}]}}]}).encode()), provider="gemini")
        self.assertIsNone(tutor_llm.reword(client, "Row 3 has 1, 2.", "why"))
        self.assertIsNone(tutor_llm.answer_open(client, "what goes in R3C5", {"difficulty": "Easy"}))

    def test_dotenv_with_a_gemini_key_selects_gemini(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write("GEMINI_API_KEY=AIza-from-file\nTUTOR_PROVIDER=gemini\nOPENAI_API_KEY=sk-old\n")
            env = {}
            tutor_llm.load_dotenv(path, env)
            client = tutor_llm.from_env(env)
            self.assertEqual((client.provider, client.model), ("gemini", "gemini-3.5-flash-lite"))

    # ---- telling the player why the AI is off (a typo in .env must be visible) ----
    def test_a_botched_env_file_is_explained_in_plain_words(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:               # the real mistake: two settings on one line
                fh.write(f"GEMINI_API_KEY={SECRET}\nTUTOR_PROVIDER=TUTOR_MODEL=gemini-x\n")
            env = {}
            tutor_llm.load_dotenv(path, env)
            self.assertIsNone(tutor_llm.from_env(env))
            why = tutor_llm.explain_off(env, dotenv_found=True)
            self.assertIn("TUTOR_PROVIDER is 'tutor_model=gemini-x'", why)
            self.assertIn("must be one of", why)
            self.assertNotIn(SECRET, why)

    def test_every_reason_for_ai_being_off_is_named_and_never_leaks_a_key(self):
        cases = [
            ({"GEMINI_API_KEY": SECRET, "TUTOR_AI": "off"}, "TUTOR_AI"),
            ({}, "no API key"),
            ({"GEMINI_API_KEY": "  "}, "no API key"),
            ({"OPENAI_API_KEY": SECRET, "TUTOR_PROVIDER": "gemini"}, "GEMINI_API_KEY is empty or missing"),
            ({"GEMINI_API_KEY": SECRET, "TUTOR_PROVIDER": "gemin"}, "must be one of"),
            ({"GEMINI_API_KEY": SECRET, "TUTOR_PROVIDER": f"x{SECRET}x"}, "must be one of"),
            ({"GEMINI_API_KEY": SECRET, "TUTOR_MODEL": "a/b"}, "not a usable model name"),
        ]
        for env, expect in cases:
            with self.subTest(env=sorted(env), expect=expect):
                why = tutor_llm.explain_off(env)
                self.assertIn(expect, why)
                self.assertNotIn(SECRET, why)
                self.assertLessEqual(len(why), 400)
        self.assertIn("no .env file was found", tutor_llm.explain_off({}, dotenv_found=False))
        self.assertIn("holds no key", tutor_llm.explain_off({}, dotenv_found=True))
        self.assertEqual(tutor_llm.explain_off({"GEMINI_API_KEY": SECRET}), "")      # nothing to explain
        self.assertEqual(tutor_llm.explain_off({"GOOGLE_API_KEY": SECRET}), "")

    def test_the_startup_line_names_the_provider_or_the_reason(self):
        import os
        from unittest import mock
        saved = web.LLM
        try:
            web.LLM = tutor_llm.Client(SECRET, "gemini-x", provider="gemini")
            self.assertEqual(web.startup_line(), " * Tutor AI: gemini / gemini-x")
            web.LLM = None
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": SECRET, "TUTOR_PROVIDER": "TUTOR_MODEL=m"},
                                 clear=True), mock.patch.object(web, "DOTENV_FOUND", True):
                line = web.startup_line()
            self.assertIn("off", line)
            self.assertIn("must be one of", line)
            self.assertNotIn(SECRET, line)
        finally:
            web.LLM = saved

    def test_http_errors_and_garbage_raise_llmerror(self):
        for transport in (lambda *a: (500, b"oops"), lambda *a: (200, b"not json"),
                          lambda *a: (200, b'{"content": 5}'), lambda *a: (429, b"")):
            with self.assertRaises(tutor_llm.LLMError):
                tutor_llm.Client(SECRET, transport=transport).complete("s", "u")

    def test_output_guard_rejects_answers_and_instructions(self):
        bad = ["R3C5 = 7", "R3C5 is 7.", "R3C5 must be 7", "The answer is 4.", "the answer is 4",
               "Put 4 in R3C5.", "Place a 4 into R3C5", "Enter 9 at R1C1", "x" * 601, "", "   ", None,
               "The digit for R1C1 is 4"]
        for text in bad:
            with self.subTest(text=text):
                self.assertIsNone(tutor_llm.accept(text))

    def test_output_guard_accepts_ordinary_teaching(self):
        good = ["Look at Row 4: it already has a 6, so R4C2 can't be 6.",
                "A hidden single is a digit with only one place left in a unit.",
                "Nice work! Try scanning for the digit that appears most often."]
        for text in good:
            with self.subTest(text=text):
                self.assertEqual(tutor_llm.accept(text), text)

    def test_output_guard_strips_markdown(self):
        self.assertEqual(tutor_llm.accept("**Good** _job_ `now`"), "Good job now")


class TestApiWithAI(ApiCase):
    def use(self, transport):
        web.LLM = tutor_llm.Client(SECRET, "m-1", transport)

    def test_free_explanations_can_be_reworded_by_the_model(self):
        t = ok_transport("Here is a friendlier take on that.")
        self.use(t)
        body = self.new()
        _, out = self.tutor(body, intent="overview", text=None)
        reply = out["event"]["reply"]
        self.assertEqual(reply["text"], "Here is a friendlier take on that.")
        self.assertEqual(reply["source"], "ai")
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(self.game(body).tutor_messages(0)[-1]["text"], reply["text"])
        # the steps (the verified content) are untouched by rewording
        self.assertTrue(reply["steps"])

    def test_hints_never_go_through_the_model(self):
        t = ok_transport()
        self.use(t)
        body = self.new()
        seq = body["state"]["hint_seq"]
        _, out = self.tutor(body, intent="next_move", seq=seq)
        self.assertEqual(t.calls, [])
        self.assertEqual(out["tutor"][-1]["source"], "rules")

    def test_what_the_model_is_sent_contains_no_solution_and_no_board(self):
        t = ok_transport()
        self.use(t)
        body = self.new()
        g = self.game(body)
        r, c = editable(g)[0]
        self.tutor(body, text="tell me something about my progress please", selected=[r, c])
        self.tutor(body, intent="overview")
        self.tutor(body, intent="explain_cell", selected=[r, c])
        self.assertGreaterEqual(len(t.calls), 2)
        for call in t.calls:
            blob = json.dumps(call["body"])
            for row in g.solution:
                self.assertNotIn("".join(map(str, row)), blob)
                self.assertNotIn(json.dumps(row), blob)
            self.assertNotIn(json.dumps(g.puzzle[0]), blob)
            self.assertNotIn(SECRET, blob)                     # the key is a header, not content

    def test_open_questions_are_answered_from_limited_facts(self):
        t = ok_transport("Cats are great, but let's get back to the puzzle.")
        self.use(t)
        body = self.new()
        _, out = self.tutor(body, text="tell me a joke about cats")
        self.assertEqual(out["event"]["intent"], "unknown")
        self.assertEqual(out["event"]["reply"]["source"], "ai")
        prompt = t.calls[0]["body"]["messages"][0]["content"]
        facts = json.loads(prompt.split("\n", 1)[1].split("\n\nPlayer question", 1)[0])
        self.assertLessEqual(set(facts), {"difficulty", "mistakes", "max_mistakes", "status",
                                          "selected_cell", "selected_cell_is_empty",
                                          "digits_already_in_its_units"})

    def test_a_failing_model_falls_back_to_the_built_in_answer_and_the_game_goes_on(self):
        def boom(*a):
            raise tutor_llm.LLMError("network error: TimeoutError")
        self.use(boom)
        body = self.new()
        gid = body["state"]["game_id"]
        status, out = self.tutor(body, intent="overview")
        self.assertEqual(status, 200)
        reply = out["event"]["reply"]
        self.assertIn("Here's a way to work through it", reply["text"])
        self.assertTrue(reply["ai_error"])
        self.assertEqual(reply["source"], "rules")
        # nothing else is affected
        s, out = self.tutor(body, intent="next_move", seq=out["state"]["hint_seq"])
        self.assertEqual(s, 200)
        g = self.game(body)
        r, c = editable(g)[0]
        s, out = self.post("/api/move", game_id=gid, row=r, col=c, value=g.solution[r][c])
        self.assertEqual(s, 200)

    def test_http_error_from_the_model_also_falls_back(self):
        self.use(lambda *a: (503, b"unavailable"))
        body = self.new()
        _, out = self.tutor(body, intent="overview")
        self.assertTrue(out["event"]["reply"]["ai_error"])

    def _stderr_of_an_overview(self, transport):
        import contextlib
        import io
        self.use(transport)
        body = self.new()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _, out = self.tutor(body, intent="overview")
        return buf.getvalue(), out

    def test_the_server_terminal_says_why_the_model_was_not_used(self):
        err, out = self._stderr_of_an_overview(lambda *a: (
            429, json.dumps({"error": {"message": "Quota exceeded for this project"}}).encode()))
        self.assertIn("HTTP 429: Quota exceeded for this project", err)
        self.assertIn(web.LLM.provider, err)
        self.assertTrue(out["event"]["reply"]["ai_error"])
        err, _ = self._stderr_of_an_overview(ok_transport("x" * 700))
        self.assertIn("over 600 characters", err)
        err, _ = self._stderr_of_an_overview(lambda *a: 1 / 0)
        self.assertIn("ZeroDivisionError", err)
        err, out = self._stderr_of_an_overview(ok_transport("A friendly wording."))
        self.assertEqual(err, "")                                  # silence when it works
        self.assertEqual(out["event"]["reply"]["source"], "ai")

    def test_the_terminal_message_never_contains_the_key(self):
        err, _ = self._stderr_of_an_overview(lambda *a: (
            400, json.dumps({"error": {"message": f"API key {SECRET} is not valid"}}).encode()))
        self.assertIn("not valid", err)
        self.assertNotIn(SECRET, err)

    def test_unexpected_exceptions_in_the_transport_cannot_break_the_request(self):
        def bad(*a):
            raise ZeroDivisionError("bug")
        self.use(bad)
        body = self.new()
        status, out = self.tutor(body, intent="overview")
        self.assertEqual(status, 200)
        self.assertTrue(out["event"]["reply"]["ai_error"])

    def test_openai_provider_works_end_to_end_through_the_api(self):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body)))
            return 200, json.dumps({"choices": [{"message": {"content": "A friendly OpenAI wording."}}]}).encode()
        web.LLM = tutor_llm.Client(SECRET, "gpt-x", transport, provider="openai")
        body = self.new()
        _, out = self.tutor(body, intent="overview")
        self.assertEqual(out["event"]["reply"]["text"], "A friendly OpenAI wording.")
        self.assertEqual(out["event"]["reply"]["source"], "ai")
        self.assertEqual(calls[0][0], tutor_llm.OPENAI_URL)
        hist = self.client.get(f"/api/tutor/history?game_id={body['state']['game_id']}").get_json()
        self.assertEqual(hist["ai"]["provider"], "openai")
        # hints still never reach the model, and the key never reaches the browser
        n = len(calls)
        _, out = self.tutor(body, intent="next_move", seq=out["state"]["hint_seq"])
        self.assertEqual(len(calls), n)
        blob = json.dumps(out) + json.dumps(hist) + self.client.get("/").get_data(as_text=True)
        self.assertNotIn(SECRET, blob)

    def test_gemini_provider_works_end_to_end_through_the_api(self):
        calls = []

        def transport(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body)))
            return 200, json.dumps({"candidates": [{"content": {"parts": [
                {"text": "A friendly Gemini wording."}]}}]}).encode()
        web.LLM = tutor_llm.Client(SECRET, "gemini-x", transport, provider="gemini")
        body = self.new()
        _, out = self.tutor(body, intent="overview")
        self.assertEqual(out["event"]["reply"]["text"], "A friendly Gemini wording.")
        self.assertEqual(out["event"]["reply"]["source"], "ai")
        self.assertIn("gemini-x:generateContent", calls[0][0])
        hist = self.client.get(f"/api/tutor/history?game_id={body['state']['game_id']}").get_json()
        self.assertEqual(hist["ai"]["provider"], "gemini")
        # an open question is answered from limited facts only: no board, no solution
        _, out = self.tutor(body, text="why is the sky blue?")
        prompt = calls[-1][2]["contents"][0]["parts"][0]["text"]
        self.assertNotIn("solution", prompt.lower())
        # hints still never reach the model, and the key never reaches the browser
        n = len(calls)
        _, out = self.tutor(body, intent="next_move", seq=out["state"]["hint_seq"])
        self.assertEqual(len(calls), n)
        blob = json.dumps(out) + json.dumps(hist) + self.client.get("/").get_data(as_text=True)
        self.assertNotIn(SECRET, blob)

    def test_unsafe_model_output_is_discarded(self):
        for evil in ("R3C5 = 7", "The answer is 4.", "Put 4 in R3C5.", "x" * 700):
            with self.subTest(evil=evil[:20]):
                self.use(ok_transport(evil))
                body = self.new()
                _, out = self.tutor(body, intent="overview")
                self.assertNotEqual(out["event"]["reply"]["text"], evil)
                self.assertTrue(out["event"]["reply"]["ai_error"])

    def test_the_model_cannot_change_the_board_or_the_allowance(self):
        self.use(ok_transport("Sure!"))
        body = self.new()
        g = self.game(body)
        before = snapshot(g)
        for intent, params in FREE_INTENTS:
            self.tutor(body, intent=intent, params=params, selected=[0, 0])
        self.tutor(body, text="what is the meaning of life")
        self.assertEqual(snapshot(g), before)

    def test_ai_calls_are_capped_per_game(self):
        t = ok_transport()
        self.use(t)
        body = self.new()
        for _ in range(tutor_llm.MAX_CALLS_PER_GAME + 10):
            self.tutor(body, intent="overview")
        self.assertEqual(len(t.calls), tutor_llm.MAX_CALLS_PER_GAME)
        # past the cap the built-in wording is used and the game still answers
        _, out = self.tutor(body, intent="overview")
        self.assertEqual(out["event"]["reply"]["source"], "rules")
        self.assertIn("Here's a way to work through it", out["event"]["reply"]["text"])

    def test_the_key_is_never_sent_to_the_browser(self):
        self.use(ok_transport())
        body = self.new()
        gid = body["state"]["game_id"]
        seen = [json.dumps(body)]
        for intent, params in FREE_INTENTS:
            seen.append(json.dumps(self.tutor(body, intent=intent, params=params)[1]))
        seen.append(self.client.get(f"/api/tutor/history?game_id={gid}").get_data(as_text=True))
        seen.append(self.client.get(f"/api/state?game_id={gid}").get_data(as_text=True))
        seen.append(self.client.get("/").get_data(as_text=True))
        for path in ("/static/script.js", "/static/style.css"):
            seen.append(self.client.get(path).get_data(as_text=True))
        for blob in seen:
            self.assertNotIn(SECRET, blob)
            self.assertNotIn("ANTHROPIC_API_KEY", blob)
            self.assertNotIn("x-api-key", blob.lower())
        hist = self.client.get(f"/api/tutor/history?game_id={gid}").get_json()
        self.assertEqual(hist["ai"], {"enabled": True, "model": "m-1", "provider": "anthropic"})

    def test_the_key_is_not_in_any_served_source_file_either(self):
        for path in ("/static/script.js", "/static/style.css", "/"):
            text = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("sk-ant", text)
            self.assertNotIn("api.anthropic.com", text)


if __name__ == "__main__":
    unittest.main()
