"""
test_game.py
------------
Tests for the authoritative game state machine (game.py): the mistake model,
the finite hint allowance, pause/resume timing and scoring.

A fake clock replaces real time, so every timing assertion is exact and the
suite needs no sleeping.
"""

import random
import threading
import unittest

import game as gm
import sudoku_logic as sl


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_game(level="Easy", seed=1, **kw):
    random.seed(seed)
    puzzle, solution = sl.generate_puzzle(level)
    kw.setdefault("clock", FakeClock())
    return gm.Game(puzzle, solution, level, **kw)


def editable(g):
    return [(r, c) for r in range(9) for c in range(9) if not g.puzzle[r][c]]


def wrong_digit(g, r, c, avoid=()):
    return next(d for d in range(1, 10)
                if d != g.solution[r][c] and d not in avoid)


def fill_correct(g, skip=()):
    for r, c in editable(g):
        if (r, c) not in skip and (r, c) not in g.revealed:
            g.move(r, c, g.solution[r][c])


class TestMistakeModel(unittest.TestCase):
    def test_wrong_digit_is_charged_once_per_cell_and_digit(self):
        g = make_game()
        r, c = editable(g)[0]
        w = wrong_digit(g, r, c)

        self.assertEqual(g.move(r, c, w)["type"], "mistake")
        self.assertEqual(g.mistakes, 1)

        self.assertEqual(g.move(r, c, w)["type"], "noop")        # same digit again
        self.assertEqual(g.mistakes, 1)

        g.move(r, c, 0)                                          # erase ...
        self.assertEqual(g.move(r, c, w)["type"], "repeat_mistake")  # ... re-type
        self.assertEqual(g.mistakes, 1)

    def test_undo_never_refunds_and_replaying_is_not_recharged(self):
        g = make_game()
        r, c = editable(g)[0]
        w = wrong_digit(g, r, c)
        g.move(r, c, w)
        g.undo()
        self.assertEqual(g.mistakes, 1, "undo must not refund a mistake")
        self.assertEqual(g.move(r, c, w)["type"], "repeat_mistake")
        self.assertEqual(g.mistakes, 1, "undo + retype must not be charged twice")

    def test_a_different_wrong_digit_in_the_same_cell_is_a_new_mistake(self):
        g = make_game()
        r, c = editable(g)[0]
        w1 = wrong_digit(g, r, c)
        w2 = wrong_digit(g, r, c, avoid=(w1,))
        g.move(r, c, w1)
        g.move(r, c, w2)
        self.assertEqual(g.mistakes, 2)

    def test_correcting_is_free_and_never_refunds(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, wrong_digit(g, r, c))
        self.assertEqual(g.wrong_cells(), [(r, c)])
        self.assertEqual(g.move(r, c, g.solution[r][c])["type"], "correct")
        self.assertEqual(g.mistakes, 1)
        self.assertEqual(g.wrong_cells(), [])

    def test_correct_entries_and_erasing_cost_nothing(self):
        g = make_game()
        for r, c in editable(g)[:10]:
            g.move(r, c, g.solution[r][c])
            g.move(r, c, 0)
        self.assertEqual(g.mistakes, 0)

    def test_every_distinct_wrong_cell_is_charged(self):
        g = make_game()
        for r, c in editable(g)[:3]:
            g.move(r, c, wrong_digit(g, r, c))
        self.assertEqual(g.mistakes, 3)

    def test_reaching_the_limit_loses_and_freezes_everything(self):
        clock = FakeClock()
        g = make_game(clock=clock)
        limit = g.rules.max_mistakes
        for r, c in editable(g)[:limit]:
            clock.advance(2)
            g.move(r, c, wrong_digit(g, r, c))
        self.assertEqual(g.status, gm.LOST)
        self.assertEqual(g.mistakes, limit)
        frozen = g.elapsed()
        clock.advance(500)
        self.assertEqual(g.elapsed(), frozen, "time must stop when the game is lost")
        r, c = editable(g)[limit]
        with self.assertRaises(gm.GameError) as ctx:
            g.move(r, c, g.solution[r][c])
        self.assertEqual(ctx.exception.code, "finished")

    def test_clue_and_revealed_cells_are_locked(self):
        g = make_game()
        clue = next((r, c) for r in range(9) for c in range(9) if g.puzzle[r][c])
        with self.assertRaises(gm.GameError) as ctx:
            g.move(*clue, 5)
        self.assertEqual(ctx.exception.code, "locked")
        for _ in range(3):
            g.hint()
        (rr, cc), = g.revealed
        with self.assertRaises(gm.GameError) as ctx:
            g.move(rr, cc, 0)
        self.assertEqual(ctx.exception.code, "locked")

    def test_bad_input_is_rejected(self):
        g = make_game()
        r, c = editable(g)[0]
        for args in ((r, c, 10), (r, c, -1), (r, c, "x"), (9, 0, 1), (0, -1, 1), ("a", 0, 1),
                     (r, c, 0.5), (r, c, True), (r, c, None), (r, c, "5"), (True, 0, 1)):
            with self.assertRaises(gm.GameError) as ctx:
                g.move(*args)
            self.assertEqual(ctx.exception.code, "bad_request", args)
        self.assertEqual(g.mistakes, 0)

    def test_rules_get_stricter_with_difficulty(self):
        e, m, h = (gm.RULES[x] for x in ("Easy", "Medium", "Hard"))
        self.assertGreater(e.max_mistakes, m.max_mistakes)
        self.assertGreater(e.max_hints, m.max_hints)
        self.assertGreater(m.max_hints, h.max_hints)
        self.assertLess(e.multiplier, m.multiplier)
        self.assertLess(m.multiplier, h.multiplier)


class TestHintAllowance(unittest.TestCase):
    def test_a_hint_escalates_nudge_explain_reveal_on_one_target(self):
        g = make_game("Medium")
        views = []
        for level in (1, 2, 3):
            ev = g.hint()
            self.assertEqual(ev["level"], level)
            views.append(g.public_state())
        self.assertEqual(g.hints_used, 3)

        self.assertNotIn("cell", views[0]["active_hint"], "level 1 must not name the cell")
        r, c = views[1]["active_hint"]["cell"]
        self.assertEqual(views[1]["board"][r][c], 0, "level 2 must not fill the cell")
        self.assertEqual(views[2]["active_hint"]["cell"], [r, c], "same target throughout")
        self.assertEqual(views[2]["board"][r][c], g.solution[r][c], "level 3 reveals it")
        self.assertEqual(g.revealed, {(r, c): g.solution[r][c]})

    def test_first_hint_never_completes_a_cell(self):
        for seed in range(6):
            g = make_game("Easy", seed=seed)
            before = g.public_state()["board"]
            g.hint()
            self.assertEqual(g.public_state()["board"], before)

    def test_allowance_is_a_hard_cap(self):
        for level in ("Easy", "Medium", "Hard"):
            g = make_game(level, seed=2)
            cap = g.rules.max_hints
            blocked = 0
            for _ in range(cap * 4):
                try:
                    g.hint()
                except gm.GameError as exc:
                    self.assertEqual(exc.code, "no_hints")
                    blocked += 1
            self.assertEqual(g.hints_used, cap)
            self.assertEqual(g.public_state()["hints_left"], 0)
            self.assertEqual(blocked, cap * 3)
            self.assertLessEqual(len(g.revealed), cap // 3)

    def test_spamming_hints_can_never_solve_the_puzzle(self):
        for level in ("Easy", "Medium", "Hard"):
            g = make_game(level, seed=9)
            for _ in range(500):
                try:
                    g.hint()
                except gm.GameError:
                    pass
            self.assertEqual(g.status, gm.PLAYING)
            empties = sum(1 for row in g.public_state()["board"] for v in row if v == 0)
            self.assertGreater(empties, 20, f"{level}: hints must not play the game for you")

    def test_duplicate_requests_with_the_same_sequence_are_charged_once(self):
        g = make_game()
        seq = g.hint_seq
        self.assertEqual(g.hint(seq=seq)["type"], "hint")
        self.assertEqual(g.hint(seq=seq)["type"], "stale")
        self.assertEqual(g.hint(seq=seq)["type"], "stale")
        self.assertEqual(g.hints_used, 1)

    def test_concurrent_double_clicks_are_charged_once(self):
        g = make_game()
        results, barrier = [], threading.Barrier(30)

        def click():
            barrier.wait()
            results.append(g.hint(seq=0)["type"])

        threads = [threading.Thread(target=click) for _ in range(30)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count("hint"), 1)
        self.assertEqual(g.hints_used, 1)

    def test_hints_only_target_empty_cells_and_never_overwrite(self):
        g = make_game()
        cells = editable(g)
        for r, c in cells[:6]:
            g.move(r, c, g.solution[r][c])
        wrong_cell = cells[6]
        g.move(*wrong_cell, wrong_digit(g, *wrong_cell))
        before = g.public_state()["board"]
        for _ in range(3):
            g.hint()
        after = g.public_state()
        r, c = after["active_hint"]["cell"]
        self.assertEqual(before[r][c], 0, "the target must have been empty")
        self.assertNotEqual((r, c), wrong_cell)
        for rr in range(9):
            for cc in range(9):
                if before[rr][cc]:
                    self.assertEqual(after["board"][rr][cc], before[rr][cc])

    def test_a_wrong_entry_cannot_corrupt_the_deduction(self):
        """Put a wrong digit where it would make a two-candidate neighbour look
        like a (false) single. The hint must still be sound."""
        found = 0
        for seed in range(60):
            g = make_game("Easy", seed=seed)
            masks = sl.candidate_masks(g.puzzle)
            scenario = None
            for y, m in masks.items():
                if bin(m).count("1") != 2:
                    continue
                w = g.solution[y[0]][y[1]]
                for x, mx in masks.items():
                    peer = (x[0] == y[0] or x[1] == y[1]
                            or (x[0] // 3, x[1] // 3) == (y[0] // 3, y[1] // 3))
                    if x != y and peer and g.solution[x[0]][x[1]] != w and mx >> (w - 1) & 1:
                        scenario = (x, y, w)
                        break
                if scenario:
                    break
            if not scenario:
                continue
            x, y, w = scenario
            g.move(*x, w)                              # wrong, and a peer of y
            g.hint(prefer=list(y))
            hint = g._active_hint[0]
            r, c = hint.cell
            self.assertEqual(hint.digit, g.solution[r][c],
                             "a wrong entry must never change the deduction")
            found += 1
            if found >= 6:
                break
        self.assertGreaterEqual(found, 3, "test scenario should be constructible")

    def test_with_only_wrong_entries_left_there_is_nothing_to_hint_and_no_charge(self):
        rules = gm.Rules(max_mistakes=10 ** 6, max_hints=5, par_seconds=480, multiplier=1)
        g = make_game(rules=rules)
        for r, c in editable(g):
            g.move(r, c, wrong_digit(g, r, c))
        with self.assertRaises(gm.GameError) as ctx:
            g.hint()
        self.assertEqual(ctx.exception.code, "no_target")
        self.assertEqual(g.hints_used, 0)

    def test_a_reveal_that_fills_the_last_cell_wins(self):
        g = make_game()
        last = editable(g)[-1]
        fill_correct(g, skip={last})
        self.assertEqual(g.status, gm.PLAYING)
        for _ in range(3):
            g.hint()
        self.assertEqual(g.status, gm.WON)
        self.assertEqual(g.summary["hints_used"], 3)

    def test_selecting_a_cell_steers_the_hint(self):
        g = make_game("Easy", seed=5)
        masks = sl.candidate_masks(g.puzzle)
        singles = sorted(c for c, m in masks.items() if bin(m).count("1") == 1)
        target = singles[-1]
        g.hint(prefer=list(target))
        g.hint()
        self.assertEqual(tuple(g.public_state()["active_hint"]["cell"]), target)

    def test_acting_on_the_target_clears_the_hint(self):
        g = make_game()
        g.hint()
        g.hint()
        r, c = g.public_state()["active_hint"]["cell"]
        g.move(r, c, g.solution[r][c])
        self.assertIsNone(g.public_state()["active_hint"])

    def test_bad_focus_cell_is_ignored(self):
        g = make_game()
        self.assertEqual(g.hint(prefer=[99, -4])["type"], "hint")
        self.assertEqual(g.hint(prefer="nonsense")["type"], "hint")


class TestPauseAndTimer(unittest.TestCase):
    def test_timer_starts_when_the_puzzle_appears(self):
        clock = FakeClock()
        g = make_game(clock=clock)
        clock.advance(7)
        self.assertAlmostEqual(g.elapsed(), 7)

    def test_pause_freezes_time_and_resume_excludes_the_paused_span(self):
        clock = FakeClock()
        g = make_game(clock=clock)
        clock.advance(10)
        self.assertTrue(g.pause())
        for _ in range(5):
            clock.advance(60)
            self.assertAlmostEqual(g.elapsed(), 10, msg="paused time must not count")
            g.public_state()                               # reading never resumes
        self.assertEqual(g.status, gm.PAUSED)
        self.assertTrue(g.resume())
        clock.advance(4)
        self.assertAlmostEqual(g.elapsed(), 14)

    def test_every_puzzle_action_is_blocked_while_paused(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, g.solution[r][c])
        g.pause()
        state_before = g.public_state()
        actions = {
            "move":    lambda: g.move(r, c, 0),
            "undo":    lambda: g.undo(),
            "hint":    lambda: g.hint(),
            "reset":   lambda: g.reset(),
            "check":   lambda: g.check(),
            "give_up": lambda: g.give_up(),
        }
        for name, act in actions.items():
            with self.assertRaises(gm.GameError, msg=name) as ctx:
                act()
            self.assertEqual(ctx.exception.code, "paused", name)
        self.assertEqual(g.public_state(), state_before, "a blocked action changes nothing")

    def test_pause_and_resume_change_nothing_but_the_clock(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, wrong_digit(g, r, c))
        g.hint()
        keep = {k: g.public_state()[k] for k in
                ("board", "mistakes", "hints_used", "hints_left", "wrong", "active_hint")}
        g.pause()
        g.resume()
        after = g.public_state()
        for k, v in keep.items():
            self.assertEqual(after[k], v, k)

    def test_pause_and_resume_are_idempotent(self):
        g = make_game()
        self.assertFalse(g.resume(), "resuming a running game is a no-op")
        self.assertTrue(g.pause())
        self.assertFalse(g.pause(), "pausing twice is a no-op")
        self.assertEqual(g.pause_reason, "manual")
        self.assertTrue(g.resume())
        self.assertFalse(g.resume())

    def test_away_pause_still_needs_an_explicit_resume(self):
        clock = FakeClock()
        g = make_game(clock=clock)
        g.pause("away")
        self.assertEqual(g.public_state()["pause_reason"], "away")
        clock.advance(300)
        self.assertEqual(g.status, gm.PAUSED)
        g.pause("manual")                                   # no effect, still the same pause
        self.assertEqual(g.pause_reason, "away")

    def test_finished_games_cannot_be_paused_or_resumed(self):
        g = make_game()
        g.give_up()
        self.assertFalse(g.pause())
        self.assertFalse(g.resume())
        self.assertEqual(g.status, gm.GAVE_UP)

    def test_pause_does_not_help_a_player_beat_the_clock(self):
        """Time spent paused is excluded from the final score time."""
        clock = FakeClock()
        g = make_game(clock=clock)
        clock.advance(30)
        g.pause()
        clock.advance(10 ** 6)
        g.resume()
        clock.advance(30)
        fill_correct(g)
        self.assertEqual(g.status, gm.WON)
        self.assertAlmostEqual(g.summary["time"], 60, delta=0.5)


class TestLifecycleAndScoring(unittest.TestCase):
    def test_reset_clears_entries_but_keeps_every_cost(self):
        clock = FakeClock()
        g = make_game("Medium", clock=clock)
        cells = editable(g)
        g.move(*cells[0], g.solution[cells[0][0]][cells[0][1]])
        g.move(*cells[1], wrong_digit(g, *cells[1]))
        for _ in range(3):
            g.hint()
        revealed = dict(g.revealed)
        clock.advance(25)
        before = g.public_state()
        g.reset()
        after = g.public_state()
        self.assertEqual(g.entries, [[0] * 9 for _ in range(9)])
        self.assertEqual(g.revealed, revealed, "paid-for reveals stay")
        self.assertEqual(after["mistakes"], before["mistakes"])
        self.assertEqual(after["hints_used"], before["hints_used"])
        self.assertAlmostEqual(g.elapsed(), 25)
        self.assertFalse(after["can_undo"])
        # the same wrong digit is still on the ledger: reset is not a refund
        g.move(*cells[1], wrong_digit(g, *cells[1]))
        self.assertEqual(g.mistakes, 1)

    def test_undo_restores_the_previous_value(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, g.solution[r][c])
        g.move(r, c, 0)
        g.undo()
        self.assertEqual(g.entries[r][c], g.solution[r][c])
        g.undo()
        self.assertEqual(g.entries[r][c], 0)
        with self.assertRaises(gm.GameError) as ctx:
            g.undo()
        self.assertEqual(ctx.exception.code, "nothing_to_undo")

    def test_winning_records_an_accurate_summary(self):
        clock = FakeClock()
        g = make_game("Medium", clock=clock)
        cells = editable(g)
        g.move(*cells[0], wrong_digit(g, *cells[0]))     # 1 mistake
        g.hint()                                          # 1 hint
        g.hint()                                          # 2 hints
        clock.advance(120)
        fill_correct(g)
        self.assertEqual(g.status, gm.WON)
        s = g.public_state()["summary"]
        self.assertEqual((s["mistakes"], s["hints_used"]), (1, 2))
        self.assertAlmostEqual(s["time"], 120, delta=0.5)
        self.assertEqual(s["score"], round((1000 - 100 - 120) * 1.5))
        self.assertEqual(s["stars"], 2)
        self.assertFalse(s["flawless"])

    def test_score_rules(self):
        clock = FakeClock()
        g = make_game("Hard", clock=clock)
        clock.advance(60)
        fill_correct(g)
        s = g.summary
        self.assertEqual(s["score"], 2000)               # 1000 x 2.0, no penalties
        self.assertEqual(s["stars"], 3)
        self.assertTrue(s["flawless"])

        slow = make_game("Easy", clock=clock)
        clock.advance(480 + 3000)                         # far over par
        fill_correct(slow)
        self.assertEqual(slow.summary["score"], 0, "score never goes negative")
        self.assertEqual(slow.summary["stars"], 1)

    def test_give_up_reveals_the_solution_and_scores_nothing(self):
        g = make_game()
        r, c = editable(g)[0]
        g.move(r, c, wrong_digit(g, r, c))
        g.give_up()
        state = g.public_state()
        self.assertEqual(state["status"], gm.GAVE_UP)
        self.assertEqual(state["board"], g.solution)
        self.assertIsNone(state["summary"])
        self.assertEqual(state["wrong"], [])
        self.assertIn([r, c], state["auto_solved"])
        with self.assertRaises(gm.GameError):
            g.move(r, c, 1)

    def test_solution_is_only_ever_sent_when_giving_up(self):
        g = make_game()
        before = g.public_state()
        self.assertNotEqual(before["board"], g.solution)
        self.assertNotIn("solution", before)
        for _ in range(3):
            g.hint()
        self.assertNotIn("solution", g.public_state())

    def test_check_reports_without_revealing_or_charging(self):
        g = make_game()
        self.assertEqual(g.check()["status"], "ok")
        r, c = editable(g)[0]
        g.move(r, c, wrong_digit(g, r, c))
        res = g.check()
        self.assertEqual((res["status"], res["wrong"]), ("wrong", 1))
        self.assertEqual(g.mistakes, 1)

    def test_the_rev_counter_only_moves_on_real_changes(self):
        g = make_game()
        r, c = editable(g)[0]
        rev = g.rev
        g.move(r, c, g.solution[r][c])
        self.assertEqual(g.rev, rev + 1)
        g.move(r, c, g.solution[r][c])                    # noop
        self.assertEqual(g.rev, rev + 1)
        g.check()
        self.assertEqual(g.rev, rev + 1)

    def test_conflicting_entries_are_reported(self):
        g = make_game()
        r, c = editable(g)[0]
        clash = next(g.puzzle[r][k] for k in range(9) if g.puzzle[r][k])
        g.move(r, c, clash)
        self.assertIn([r, c], g.public_state()["conflicts"])


class TestStore(unittest.TestCase):
    def test_oldest_games_are_evicted(self):
        store = gm.GameStore(max_games=3)
        games = [make_game(seed=i) for i in range(5)]
        for g in games:
            store.add(g)
        self.assertEqual(len(store), 3)
        self.assertIsNone(store.get(games[0].id))
        self.assertIs(store.get(games[4].id), games[4])

    def test_unknown_ids_are_none(self):
        store = gm.GameStore()
        for bad in (None, 5, "nope", ["x"]):
            self.assertIsNone(store.get(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
