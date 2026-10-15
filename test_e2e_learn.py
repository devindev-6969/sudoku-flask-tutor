"""
test_e2e_learn.py
-----------------
Real-browser tests for Learn mode (the Sudoku Tutor), driven with genuine clicks
and key presses in Chromium. They share the harness in test_e2e.py.

Skipped automatically when Playwright or Chromium is not available.
Run with:  python -m unittest test_e2e_learn -v
"""

import json
import re
import unittest

import app as web
import game as gm
import hints as hintlib
import sudoku_logic as sl
import tutor_llm
from test_e2e import BrowserTestCase

SECRET = "sk-ant-E2E-SECRET-9876543210"
FAST_STUCK = "window.STUCK_AFTER_MS = 700;"


class LearnCase(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self._llm = web.LLM
        web.LLM = None

    def tearDown(self):
        web.LLM = self._llm
        super().tearDown()

    # -- helpers ------------------------------------------------------- #
    def learn(self, page, level="Easy"):
        self.start(page, level)
        page.click('#mode [data-mode="learn"]')
        page.wait_for_function("S.mode === 'learn'")
        self.settle(page)
        if page.evaluate("!WIDE.matches"):
            page.wait_for_function("document.getElementById('tutor').dataset.open === 'true'")
        return page

    def play_mode(self, page):
        page.click('#mode [data-mode="play"]')
        page.wait_for_function("S.mode === 'play'")
        self.settle(page)

    def say(self, page, text):
        page.fill("#tutor-input", text)
        page.press("#tutor-input", "Enter")
        page.wait_for_function("!tutorBusy")
        self.settle(page)

    def chip(self, page, label):
        page.locator("#tutor-chips .chip", has_text=label).first.click()
        page.wait_for_function("!tutorBusy")
        self.settle(page)

    def tutor_texts(self, page):
        return page.locator("#tutor-log .msg.tutor-msg:not(.typing-row)").all_inner_texts()

    def last_tutor_text(self, page):
        return self.tutor_texts(page)[-1]

    def clash_digit(self, game, r, c):
        board = [[game._value_at(a, b) for b in range(9)] for a in range(9)]
        peers = set()
        for a in range(9):
            for b in range(9):
                if board[a][b] and (a == r or b == c or (a // 3 == r // 3 and b // 3 == c // 3)):
                    peers.add(board[a][b])
        peers.discard(game.solution[r][c])
        return sorted(peers)[0]

    def snapshot(self, page):
        s = self.state(page)
        return {k: s[k] for k in ("board", "puzzle", "mistakes", "hints_used", "hints_left",
                                  "wrong", "status", "difficulty", "game_id", "max_mistakes",
                                  "max_hints", "revealed", "attempt")}


# --------------------------------------------------------------------------- #
class TestModeSwitch(LearnCase):
    def test_play_is_the_default_and_there_is_no_tutor_in_play_mode(self):
        page = self.open()
        self.assertEqual(page.evaluate("S.mode"), "play")
        self.assertEqual(page.evaluate("document.body.dataset.mode"), "play")
        self.assertFalse(page.locator("#tutor").is_visible())
        self.assertFalse(page.locator("#tutor-open").is_visible())
        self.assertEqual(page.locator('#mode [data-mode="play"]').get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator('#mode [data-mode="learn"]').get_attribute("aria-pressed"), "false")

    def test_learn_mode_shows_the_tutor_panel_with_its_controls(self):
        page = self.open()
        self.learn(page)
        self.assertTrue(page.locator("#tutor").is_visible())
        self.assertEqual(page.locator("#tutor-title").inner_text(), "Sudoku Tutor")
        self.assertEqual(page.locator('#mode [data-mode="learn"]').get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator("#tutor-log .msg.tutor-msg").count(), 1)       # the introduction
        labels = page.locator("#tutor-chips .chip").all_inner_texts()
        for want in ("Help me find my next move", "Explain this cell", "Teach me a technique"):
            self.assertTrue(any(want in l for l in labels), (want, labels))
        self.assertTrue(page.locator("#tutor-input").is_enabled())
        self.assertTrue(page.locator("#tutor-send").is_visible())
        self.assertIn("No AI model is connected", page.locator("#tutor-foot").inner_text())

    def test_switching_modes_back_and_forth_changes_nothing_in_the_game(self):
        page = self.open()
        self.start(page, "Easy")
        g = self.game(page)
        a, b = self.editable(g)[:2]
        self.enter(page, *a, g.solution[a[0]][a[1]])
        self.enter(page, *b, self.wrong_digit(g, *b))
        page.click("#hint"); self.settle(page)
        page.click("#theme"); self.settle(page)
        theme = page.evaluate("document.documentElement.dataset.theme")
        before = self.snapshot(page)
        sel_before = page.evaluate("sel")

        self.play_mode(page)                                   # (already play) no-op safety
        page.click('#mode [data-mode="learn"]'); page.wait_for_function("S.mode === 'learn'"); self.settle(page)
        self.assertEqual(self.snapshot(page), before)
        self.play_mode(page)
        self.assertEqual(self.snapshot(page), before)
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), theme)
        self.assertEqual(page.evaluate("sel"), sel_before)
        self.assertEqual(self.game(page).mistakes, 1)
        self.assertEqual(self.game(page).hints_used, 1)

    def test_the_clock_keeps_running_across_mode_switches(self):
        page = self.open()
        self.start(page, "Easy")
        t0 = page.evaluate("currentElapsed()")
        page.click('#mode [data-mode="learn"]'); page.wait_for_function("S.mode === 'learn'")
        page.wait_for_timeout(1300)
        t1 = page.evaluate("currentElapsed()")
        self.assertGreaterEqual(t1 - t0, 1.2)
        self.assertEqual(page.evaluate("S.status"), "playing")
        self.assertTrue(page.evaluate("S.running"))
        self.play_mode(page)
        page.wait_for_timeout(700)
        self.assertGreaterEqual(page.evaluate("currentElapsed()") - t1, 0.6)
        self.assertEqual(page.evaluate("S.status"), "playing")

    def test_the_clock_is_independent_of_the_mode_while_paused(self):
        page = self.open()
        self.learn(page)
        page.click("#pause"); self.settle(page)
        e0 = page.evaluate("S.elapsed")
        self.play_mode(page)
        page.click('#mode [data-mode="learn"]'); page.wait_for_function("S.mode === 'learn'")
        page.wait_for_timeout(900)
        self.assertEqual(page.evaluate("S.status"), "paused")
        self.assertEqual(page.evaluate("S.elapsed"), e0)
        self.assertTrue(page.locator("#pause-overlay").is_visible())
        self.assertTrue(page.locator("#tutor-input").is_disabled())          # no chatting while paused
        self.assertEqual(page.locator("#tutor-input").get_attribute("placeholder"), "Resume the game to chat")
        page.click("#resume"); self.settle(page)
        self.assertEqual(page.evaluate("S.status"), "playing")
        self.assertTrue(page.locator("#tutor-input").is_enabled())

    def test_new_games_keep_the_chosen_mode_and_start_a_fresh_chat(self):
        page = self.open()
        self.learn(page)
        self.say(page, "hello")
        n = page.locator("#tutor-log .msg").count()
        self.assertGreater(n, 1)
        page.click("#new"); page.wait_for_function("S.mistakes === 0")
        self.settle(page)
        self.assertEqual(page.evaluate("S.mode"), "learn")
        self.assertEqual(page.locator("#tutor-log .msg").count(), 1)

    def test_a_reload_restores_the_mode_and_the_conversation(self):
        page = self.open()
        self.learn(page)
        self.say(page, "what is a naked pair")
        count = page.locator("#tutor-log .msg").count()
        page.reload()
        page.wait_for_function("S !== null && S.puzzle")
        page.wait_for_function("document.querySelectorAll('#tutor-log .msg').length > 0")
        self.assertEqual(page.evaluate("S.mode"), "learn")
        self.assertEqual(page.locator("#tutor-log .msg").count(), count)
        self.assertTrue(page.locator("#tutor").is_visible())


# --------------------------------------------------------------------------- #
class TestTutorConversation(LearnCase):
    def test_a_mistake_gets_an_explanation_naming_the_cell_and_the_clash(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        d = self.clash_digit(g, r, c)
        self.enter(page, r, c, d)
        text = self.last_tutor_text(page)
        self.assertIn(f"{d} can't go in R{r + 1}C{c + 1}", text)
        self.assertIn(f"already has a {d}", text)
        self.assertTrue(page.locator(".cell.tutor-cell").count() >= 1)              # board highlight
        self.assertIn("tutor-cell", page.locator(f'.cell[data-r="{r}"][data-c="{c}"]').get_attribute("class"))
        self.assertEqual(self.state(page)["mistakes"], 1)

    def test_highlighting_cells_never_changes_their_size_or_the_board_shape(self):
        """Regression: the highlight class once collided with the panel's own class and
        stretched a cell to 360px wide, so clicks landed on the wrong cell."""
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.enter(page, r, c, self.clash_digit(g, r, c))
        self.assertGreaterEqual(page.locator(".cell.tutor-cell").count(), 2)
        widths = set(page.evaluate("[...document.querySelectorAll('.cell')].map(c => Math.round(c.getBoundingClientRect().width))"))
        self.assertEqual(len(widths), 1)
        self.assertEqual(page.evaluate("document.getElementById('board').getBoundingClientRect().width"),
                         page.evaluate("document.getElementById('frame').clientWidth"))

    def test_the_same_mistake_is_not_repeated_in_the_chat(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        d = self.clash_digit(g, r, c)
        self.enter(page, r, c, d)
        n = page.locator("#tutor-log .msg.tutor-msg").count()
        page.keyboard.press("Backspace"); self.settle(page)
        page.keyboard.press(str(d)); self.settle(page)
        self.assertEqual(self.state(page)["mistakes"], 1)
        self.assertEqual(page.locator("#tutor-log .msg.tutor-msg").count(), n)

    def test_a_correct_move_is_confirmed_only_when_it_teaches_something(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        import tutor as t
        known = g._known_board()
        picks = [x for x in self.editable(g) if t.forced_technique(known, x, g.solution[x[0]][x[1]])[0] == "naked_single"]
        r, c = picks[0]
        self.enter(page, r, c, g.solution[r][c])
        self.assertIn("naked single", self.last_tutor_text(page))
        n = page.locator("#tutor-log .msg.tutor-msg").count()
        # an ordinary erase says nothing
        page.keyboard.press("Backspace"); self.settle(page)
        self.assertEqual(page.locator("#tutor-log .msg.tutor-msg").count(), n)

    def test_tutor_reads_the_selected_cell_and_the_latest_board(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[3]
        self.cell(page, r, c).click()
        self.chip(page, "Explain this cell")
        self.assertIn(f"R{r + 1}C{c + 1}", self.last_tutor_text(page))
        self.assertIn("is empty", self.last_tutor_text(page))
        self.assertEqual(page.locator("#tutor-log .msg.user").last.inner_text(), "Explain this cell")
        # the player fills the cell correctly, asks again, and the tutor has the new board
        self.enter(page, r, c, g.solution[r][c])
        self.chip(page, "Explain this cell")
        self.assertIn(f"R{r + 1}C{c + 1} = {g.solution[r][c]}", self.last_tutor_text(page))

    def test_explain_cell_with_nothing_selected_asks_for_a_selection(self):
        page = self.open()
        self.learn(page)
        self.chip(page, "Explain this cell")
        self.assertIn("Select a cell", self.last_tutor_text(page))

    def test_typed_questions_are_understood_and_checked_against_the_board(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[1]
        d = self.clash_digit(g, r, c)
        self.cell(page, r, c).click()
        self.say(page, f"Why can't I put {d} in this cell?")
        text = self.last_tutor_text(page)
        self.assertIn(f"{d} can't go in R{r + 1}C{c + 1}", text)
        board = [[g._value_at(a, b) for b in range(9)] for a in range(9)]
        for a, b in re.findall(r"R(\d)C(\d)", text)[1:]:
            self.assertEqual(board[int(a) - 1][int(b) - 1], d)          # every cited clash is real
        self.assertEqual(page.locator("#tutor-log .msg.user").last.inner_text(), f"Why can't I put {d} in this cell?")

    def test_what_am_i_missing_lists_the_rows_missing_digits(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.cell(page, r, c).click()
        self.say(page, "What am I missing in this row?")
        row = {g._value_at(r, x) for x in range(9)} - {0}
        txt = self.last_tutor_text(page)
        needs = re.search(r"It still needs ([\d, ]+)", txt).group(1)
        self.assertEqual({int(x) for x in re.findall(r"\d", needs)}, set(range(1, 10)) - row)

    def test_asking_for_the_whole_answer_is_declined_and_nothing_changes(self):
        page = self.open()
        self.learn(page)
        before = self.snapshot(page)
        self.say(page, "just give me the answer to everything")
        self.assertIn("won't fill the board", self.last_tutor_text(page))
        self.assertEqual(self.snapshot(page), before)

    def test_the_tutor_never_fills_cells_whatever_is_asked(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        before = self.snapshot(page)
        for q in ("solve the puzzle", "fill in cell R1C1", "what should I do next", "give me a hint",
                  "explain how to solve this puzzle without revealing the answer", "teach me hidden singles",
                  "what is the answer", "is there a better approach"):
            self.say(page, q)
        page.locator("#tutor-chips .chip").first.click()
        page.wait_for_function("!tutorBusy"); self.settle(page)
        after = self.snapshot(page)
        user_cells = [(r, c) for r in range(9) for c in range(9) if after["board"][r][c] and not after["puzzle"][r][c]]
        revealed = {tuple(x) for x in after["revealed"]}
        self.assertTrue(set(user_cells) <= revealed)                     # only hint reveals, never silent fills
        self.assertEqual(after["mistakes"], before["mistakes"])
        self.assertLessEqual(after["hints_used"], g.rules.max_hints)

    def test_a_cell_name_in_a_reply_selects_that_cell(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        d = self.clash_digit(g, r, c)
        self.enter(page, r, c, d)
        ref = page.locator("#tutor-log .ref", has_text=re.compile(r"R\d+C\d+")).nth(1)
        label = ref.inner_text()
        ref.click()
        rr, cc = int(label[1]) - 1, int(label[3]) - 1
        self.assertEqual(page.evaluate("sel"), [rr, cc])

    def test_typing_in_the_tutor_box_does_not_enter_digits_on_the_board(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.cell(page, r, c).click()
        page.click("#tutor-input")
        page.keyboard.type("5 4 3 hello")
        self.assertEqual(page.locator("#tutor-input").input_value(), "5 4 3 hello")
        self.assertEqual(self.state(page)["board"][r][c], 0)
        self.assertEqual(self.state(page)["mistakes"], 0)

    def test_new_conversation_clears_the_chat_but_not_the_game(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.enter(page, r, c, self.clash_digit(g, r, c))
        page.click("#hint"); self.settle(page)
        self.say(page, "hello")
        before = self.snapshot(page)
        page.click("#tutor-clear"); self.settle(page)
        self.assertEqual(page.locator("#tutor-log .msg").count(), 1)
        self.assertEqual(self.snapshot(page), before)
        self.assertEqual(self.game(page).mistakes, 1)

    def test_a_lesson_quiz_can_be_answered_once_and_is_graded(self):
        page = self.open()
        self.learn(page)
        self.say(page, "teach me hidden singles")
        self.assertIn("Hidden singles", page.locator("#tutor-log .m-title").last.inner_text())
        page.locator("#tutor-log .quiz button", has_text="R7C8").click()
        page.wait_for_function("!tutorBusy"); self.settle(page)
        self.assertTrue(self.last_tutor_text(page).startswith("Correct."))
        self.assertTrue(page.locator("#tutor-log .quiz button").first.is_disabled())

    def test_the_clue_cells_cannot_be_edited_through_the_tutor(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = next((r, c) for r in range(9) for c in range(9) if g.puzzle[r][c])
        self.cell(page, r, c).click()
        self.chip(page, "Explain this cell")
        self.assertIn("printed clue", self.last_tutor_text(page))
        self.assertEqual(self.state(page)["board"][r][c], g.puzzle[r][c])

    def test_a_loading_indicator_shows_while_the_server_is_slow(self):
        page = self.open()
        self.learn(page)
        page.route("**/api/tutor", lambda route: (page.wait_for_timeout(600), route.continue_()))
        page.fill("#tutor-input", "hello")
        page.press("#tutor-input", "Enter")
        page.wait_for_selector("#tutor-log .typing-row", timeout=2000)
        self.assertEqual(page.locator("#tutor-log").get_attribute("aria-busy"), "true")
        self.assertTrue(page.locator("#tutor-send").is_disabled())
        page.wait_for_function("!tutorBusy")
        self.assertEqual(page.locator("#tutor-log .typing-row").count(), 0)


# --------------------------------------------------------------------------- #
class TestHintsInLearnMode(LearnCase):
    def test_next_move_is_a_real_hint_and_escalates_like_the_button(self):
        page = self.open()
        self.learn(page)
        self.chip(page, "Help me find my next move")
        self.assertEqual(self.state(page)["hints_used"], 1)
        self.assertIn("Look at", self.last_tutor_text(page))
        self.assertIn("Used 1 hint", page.locator("#tutor-log .meta").last.inner_text())
        g = self.game(page)
        target = g._active_hint[0].cell
        region = g._active_hint[0].unit
        self.assertIn(target, sl.UNITS[region])                            # the named unit really holds the target
        self.chip(page, "Explain why")
        self.assertEqual(self.state(page)["hints_used"], 2)
        self.assertIn(f"R{target[0] + 1}C{target[1] + 1}", self.last_tutor_text(page))
        self.assertNotIn(target, g.revealed)                                # still not revealed at level 2
        self.chip(page, "Reveal the digit")
        self.assertEqual(self.state(page)["hints_used"], 3)                 # nudge + explanation + reveal
        self.assertEqual(g.revealed[target], g.solution[target[0]][target[1]])
        self.assertIn(f"= {g.solution[target[0]][target[1]]}", self.last_tutor_text(page))

    def test_every_chip_that_spends_a_hint_says_so_and_free_ones_do_not(self):
        page = self.open()
        self.learn(page)

        def labels():
            return page.locator("#tutor-chips .chip").all_inner_texts()
        for label in labels():
            self.assertEqual("uses a hint" in label, "next move" in label, label)
        self.chip(page, "Help me find my next move")
        self.assertTrue(any(l.startswith("Explain why") and "uses a hint" in l for l in labels()), labels())
        self.chip(page, "Explain why")
        self.assertTrue(any(l.startswith("Reveal the digit") and "uses a hint" in l for l in labels()), labels())
        free = [l for l in labels() if "uses a hint" not in l]
        self.assertIn("Explain this cell", free)
        # A reply with no follow-ups of its own falls back to the page's standing chips:
        # they must be honest about cost too.
        g = self.game(page)
        r, c = self.editable(g)[5]
        self.cell(page, r, c).click()
        self.say(page, f"why can't I put {self.clash_digit(g, r, c)} in this cell")
        self.assertEqual(self.state(page)["hints_used"], 2)
        self.assertIsNone(page.evaluate("tutorLog[tutorLog.length - 1].chips || null"))
        standing = labels()
        self.assertTrue(any("next move" in l and "uses a hint" in l for l in standing), standing)

    def test_the_hint_button_and_the_tutor_share_one_allowance(self):
        page = self.open()
        self.learn(page, "Hard")
        allowance = self.state(page)["max_hints"]
        page.click("#hint"); self.settle(page)
        self.chip(page, "Explain why")                      # the tutor continues the hint the button began
        self.assertEqual(self.state(page)["hints_used"], 2)
        self.assertEqual(self.state(page)["hints_left"], allowance - 2)
        # the button's hint also shows up in the chat
        self.assertGreaterEqual(page.locator("#tutor-log .msg.hint").count(), 2)

    def test_no_hints_left_disables_the_chip_and_typed_requests_cannot_bypass_it(self):
        page = self.open()
        self.learn(page, "Hard")
        for _ in range(self.state(page)["max_hints"]):
            if page.locator("#tutor-chips .chip", has_text="uses a hint").first.is_enabled():
                page.locator("#tutor-chips .chip", has_text="uses a hint").first.click()
                page.wait_for_function("!tutorBusy"); self.settle(page)
        self.assertEqual(self.state(page)["hints_left"], 0)
        used = self.state(page)["hints_used"]
        self.assertTrue(page.locator("#tutor-chips .chip", has_text="Help me find my next move").is_disabled())
        for q in ("give me a hint", "what should I do next", "I'm stuck", "nudge please"):
            self.say(page, q)
            self.assertEqual(self.state(page)["hints_used"], used, q)
        self.assertIn("used every hint", self.last_tutor_text(page))

    def test_double_clicking_a_hint_chip_charges_once(self):
        page = self.open()
        self.learn(page)
        chip = page.locator("#tutor-chips .chip", has_text="Help me find my next move")
        chip.dblclick()
        page.wait_for_function("!tutorBusy"); self.settle(page)
        self.assertEqual(self.state(page)["hints_used"], 1)
        self.assertEqual(self.game(page).hints_used, 1)

    def test_a_replayed_request_cannot_charge_twice(self):
        page = self.open()
        self.learn(page)
        seq = page.evaluate("S.hint_seq")
        gid = page.evaluate("S.game_id")
        js = """async ([gid, seq]) => {
          const post = () => fetch('/api/tutor', {method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({game_id: gid, intent: 'next_move', seq})}).then(r => r.json());
          const a = await post(); const b = await post(); const c = await post();
          return [a.state.hints_used, b.state.hints_used, c.state.hints_used];
        }"""
        self.assertEqual(page.evaluate(js, [gid, seq]), [1, 1, 1])
        self.assertEqual(self.game(page).hints_used, 1)

    def test_free_questions_never_cost_anything(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.cell(page, r, c).click()
        for q in ("explain this cell", "what am I missing in this row", "teach me a technique",
                  "is there a better approach", "how am I doing", "tell me something odd"):
            self.say(page, q)
        self.assertEqual(self.state(page)["hints_used"], 0)
        self.assertEqual(self.state(page)["mistakes"], 0)


# --------------------------------------------------------------------------- #
class TestUnattendedStates(LearnCase):
    def lose(self, page):
        g = self.game(page)
        done = 0
        for r, c in self.editable(g):
            if page.evaluate("S.status") != "playing":
                break
            taken = {g._value_at(a, b) for a in range(9) for b in range(9)
                     if a == r or b == c or (a // 3 == r // 3 and b // 3 == c // 3)}
            d = next((x for x in range(1, 10) if x != g.solution[r][c] and x not in taken), None)
            if d:
                page.evaluate("([r, c, d]) => send('/api/move', {game_id: S.game_id, row: r, col: c, value: d})", [r, c, d])
                done += 1
        self.settle(page)
        self.assertEqual(page.evaluate("S.status"), "lost")

    def test_after_the_game_is_lost_the_tutor_reviews_and_stays_useful(self):
        page = self.open()
        self.learn(page)
        self.lose(page)
        page.wait_for_selector("#dialog[open]")
        page.click('#dlg-actions .btn:has-text("Review board")')
        self.assertIn("Game over", " ".join(self.tutor_texts(page)))
        used = self.state(page)["hints_used"]
        self.assertTrue(page.locator("#tutor-chips .chip", has_text="Help me find my next move").is_disabled())
        self.say(page, "what should I do next")
        self.assertIn("game is over", self.last_tutor_text(page))
        self.assertEqual(self.state(page)["hints_used"], used)
        self.say(page, "how am I doing")
        self.assertIn("mistake", " ".join(page.locator("#tutor-log .msg.tutor-msg li").all_inner_texts()))
        self.say(page, "teach me hidden singles")
        self.assertIn("Hidden singles", page.locator("#tutor-log .m-title").last.inner_text())

    def test_winning_with_the_tutor_posts_a_review_and_scores_apart_from_unaided_games(self):
        page = self.open()
        self.learn(page)
        self.chip(page, "Explain this cell")
        g = self.game(page)
        for r, c in self.editable(g):
            page.evaluate("([r, c, d]) => send('/api/move', {game_id: S.game_id, row: r, col: c, value: d})",
                          [r, c, g.solution[r][c]])
        self.settle(page)
        self.assertEqual(page.evaluate("S.status"), "won")
        page.wait_for_selector("#dialog[open]")
        body = page.locator("#dlg-body").inner_text()
        self.assertIn("Tutor", body)
        self.assertIn("Best on Easy (Learn)", body)
        best = json.loads(page.evaluate("localStorage.getItem('sudoku-best')"))
        self.assertIn("Easy (Learn)", best)
        self.assertNotIn("Easy", [k for k in best if "Learn" not in k])
        page.click('#dlg-actions .btn:has-text("Close")')
        self.assertIn("Puzzle solved", " ".join(self.tutor_texts(page)))

    def test_an_unaided_win_keeps_its_own_best_score(self):
        page = self.open()
        self.start(page, "Easy")
        g = self.game(page)
        for r, c in self.editable(g):
            page.evaluate("([r, c, d]) => send('/api/move', {game_id: S.game_id, row: r, col: c, value: d})",
                          [r, c, g.solution[r][c]])
        self.settle(page)
        page.wait_for_selector("#dialog[open]")
        self.assertNotIn("Tutor", page.locator("#dlg-body").inner_text())
        self.assertIn("Easy", json.loads(page.evaluate("localStorage.getItem('sudoku-best')")))

    def test_giving_up_in_learn_mode_leaves_the_tutor_available_for_review_only(self):
        page = self.open()
        self.learn(page)
        page.click("#solve"); page.click('#dlg-actions .btn:has-text("Give up")'); self.settle(page)
        self.assertEqual(page.evaluate("S.status"), "gave_up")
        self.say(page, "hint please")
        self.assertIn("game is over", self.last_tutor_text(page))
        self.assertEqual(self.state(page)["hints_used"], 0)

    def test_a_quiet_player_gets_one_gentle_prompt_not_a_stream_of_them(self):
        page = self.open(init_scripts=[FAST_STUCK])
        self.learn(page)
        page.wait_for_function("document.querySelectorAll('#tutor-log .msg.tutor-msg').length >= 2", timeout=5000)
        page.wait_for_timeout(2200)
        texts = self.tutor_texts(page)
        self.assertEqual(sum("Stuck for a while" in t for t in texts), 1)
        self.assertEqual(self.state(page)["hints_used"], 0)            # it offers, it never spends
        # a move re-arms it, still just one more prompt
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.enter(page, r, c, g.solution[r][c])
        page.wait_for_timeout(2200)
        self.assertEqual(sum("Stuck for a while" in t for t in self.tutor_texts(page)), 2)

    def test_no_stuck_prompt_in_play_mode_or_while_paused(self):
        page = self.open(init_scripts=[FAST_STUCK])
        self.start(page, "Easy")
        page.wait_for_timeout(1800)
        self.assertEqual(page.evaluate("tutorLog.length"), 0)
        page.click('#mode [data-mode="learn"]'); page.wait_for_function("S.mode === 'learn'")
        page.click("#pause"); self.settle(page)
        page.wait_for_timeout(1800)
        self.assertFalse(any("Stuck" in t for t in self.tutor_texts(page)))


# --------------------------------------------------------------------------- #
class TestPlayModeStaysQuiet(LearnCase):
    def test_play_mode_gives_no_coaching_and_the_server_refuses_the_tutor(self):
        page = self.open()
        self.start(page, "Easy")
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.enter(page, r, c, self.clash_digit(g, r, c))
        page.click("#hint"); self.settle(page)
        self.assertEqual(page.evaluate("tutorLog.length"), 0)
        self.assertEqual(g.tutor_messages(0), [])
        self.allow_http_errors = True
        code = page.evaluate("""() => fetch('/api/tutor', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({game_id: S.game_id, intent: 'next_move'})}).then(r => r.status)""")
        self.assertEqual(code, 409)
        self.assertEqual(g.hints_used, 1)                                 # only the button's hint

    def test_existing_limits_still_apply_in_learn_mode(self):
        page = self.open()
        self.learn(page)
        g = self.game(page)
        for i, (r, c) in enumerate(self.editable(g)[:g.rules.max_mistakes]):
            self.enter(page, r, c, self.wrong_digit(g, r, c))
        self.assertEqual(page.evaluate("S.status"), "lost")
        self.assertEqual(self.state(page)["mistakes"], g.rules.max_mistakes)


# --------------------------------------------------------------------------- #
class TestAIFailureAndSecrets(LearnCase):
    def test_when_the_ai_fails_the_tutor_still_answers_and_says_so(self):
        def boom(*a):
            raise tutor_llm.LLMError("network error")
        web.LLM = tutor_llm.Client(SECRET, transport=boom)
        page = self.open()
        self.learn(page)
        self.say(page, "how do I solve this without the answer")
        self.assertIn("naked singles", self.last_tutor_text(page))
        self.assertIn("didn't answer", page.locator("#tutor-log .meta").last.inner_text())
        # the rest of the app is unaffected
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.enter(page, r, c, g.solution[r][c])
        self.assertEqual(self.state(page)["board"][r][c], g.solution[r][c])
        self.chip(page, "Help me find my next move")
        self.assertEqual(self.state(page)["hints_used"], 1)

    def test_a_working_ai_rewords_free_answers_only(self):
        calls = []

        def ok(url, headers, body, timeout):
            calls.append(json.loads(body))
            return 200, json.dumps({"content": [{"type": "text", "text": "Here is a friendly version."}]}).encode()
        web.LLM = tutor_llm.Client(SECRET, transport=ok)
        page = self.open()
        self.learn(page)
        self.say(page, "how do I solve this without the answer")
        self.assertIn("Here is a friendly version.", self.last_tutor_text(page))
        self.assertIn("Worded by the AI model", page.locator("#tutor-log .meta").last.inner_text())
        n = len(calls)
        self.chip(page, "Help me find my next move")
        self.assertEqual(len(calls), n)                                  # hints never go to the model
        self.assertIn("An AI model only rewords them", page.locator("#tutor-foot").inner_text())

    def test_the_api_key_never_reaches_the_browser(self):
        web.LLM = tutor_llm.Client(SECRET, transport=lambda *a: (200, json.dumps(
            {"content": [{"type": "text", "text": "Ok."}]}).encode()))
        seen = []
        page = self.open()
        page.on("response", lambda r: seen.append((r.url, r.text() if r.request.resource_type in
                                                   ("fetch", "xhr", "document", "script", "stylesheet") else "")))
        page.reload()
        page.wait_for_function("S !== null && S.puzzle")
        self.learn(page)
        self.say(page, "tell me something unusual")
        self.chip(page, "Explain this cell")
        blob = "\n".join(t for _, t in seen)
        self.assertGreater(len(seen), 4)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("sk-ant", blob)
        self.assertNotIn(SECRET, page.content())
        self.assertNotIn(SECRET, page.evaluate("JSON.stringify(Object.keys(localStorage).map(k => localStorage.getItem(k)))"))
        self.assertNotIn(SECRET, page.evaluate("JSON.stringify([S, tutorLog])"))

    def test_losing_the_connection_shows_a_clear_message_and_the_game_goes_on(self):
        page = self.open()
        self.allow_http_errors = True                       # the browser logs the aborted request itself
        self.learn(page)
        page.route("**/api/tutor", lambda route: route.abort())
        self.say(page, "hello there")
        text = self.last_tutor_text(page)
        self.assertIn("couldn't reach the game server", text)
        self.assertIn("Your puzzle is safe", text)
        page.unroute("**/api/tutor")
        g = self.game(page)
        r, c = self.editable(g)[0]
        self.enter(page, r, c, g.solution[r][c])                         # play carries on
        self.assertEqual(self.state(page)["board"][r][c], g.solution[r][c])
        self.say(page, "hello there")
        self.assertNotIn("couldn't reach", self.last_tutor_text(page))

    def test_the_page_shows_no_internal_error_markers_in_the_chat(self):
        page = self.open()
        self.learn(page)
        for q in ("hello", "xyzzy", "explain this cell", "", "   "):
            if q.strip():
                self.say(page, q)
        text = " ".join(page.locator("#tutor-log").all_inner_texts())
        for bad in ("undefined", "null", "NaN", "[object", "Traceback"):
            self.assertNotIn(bad, text)


# --------------------------------------------------------------------------- #
class TestLayouts(LearnCase):
    def rect(self, page, selector):
        return page.evaluate("(s) => { const r = document.querySelector(s).getBoundingClientRect();"
                             " return {l: r.left, t: r.top, r: r.right, b: r.bottom}; }", selector)

    def no_hscroll(self, page):
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),
                             page.evaluate("window.innerWidth"))

    def test_desktop_puts_the_tutor_in_its_own_column_beside_the_board(self):
        page = self.open(1366, 800)
        self.learn(page)
        board, tut, ctrl = self.rect(page, "#board"), self.rect(page, "#tutor"), self.rect(page, ".controls")
        self.assertGreaterEqual(tut["l"], ctrl["r"])
        self.assertGreaterEqual(tut["l"], board["r"])
        self.assertLessEqual(tut["r"], 1366)
        self.assertLessEqual(tut["b"], 800)
        self.assertFalse(page.locator("#tutor-open").is_visible())
        self.assertFalse(page.locator("#tutor-close").is_visible())
        self.no_hscroll(page)
        self.assertEqual(page.evaluate("document.documentElement.scrollHeight"), 800)

    def test_mid_width_uses_a_drawer_that_leaves_the_board_and_numpad_visible(self):
        page = self.open(1000, 800)
        self.learn(page)
        self.assertEqual(page.locator("#tutor").get_attribute("data-open"), "true")
        board, tut, pad = self.rect(page, "#board"), self.rect(page, "#tutor"), self.rect(page, "#numpad")
        self.assertGreaterEqual(tut["l"], board["r"])
        self.assertGreaterEqual(tut["l"], pad["r"])
        self.assertTrue(page.locator("#numpad .num").first.is_visible())
        page.click("#tutor-close")
        page.locator("#tutor").wait_for(state="hidden")
        self.assertTrue(page.locator("#tutor-open").is_visible())
        page.click("#tutor-open")
        page.locator("#tutor").wait_for(state="visible")
        self.no_hscroll(page)

    def test_phone_uses_a_bottom_sheet_and_the_board_does_not_move(self):
        page = self.open(375, 667)
        self.start(page, "Easy")
        page.evaluate("window.scrollTo(0, 0)")
        play_board = self.rect(page, "#board")
        page.click('#mode [data-mode="learn"]'); page.wait_for_function("S.mode === 'learn'"); self.settle(page)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(350)                                          # let the sheet finish sliding up
        sheet = self.rect(page, "#tutor")
        self.assertEqual(self.rect(page, "#board"), play_board)           # nothing shifted
        self.assertGreaterEqual(sheet["t"], play_board["t"] + 150)       # the top of the board stays in view
        self.assertAlmostEqual(sheet["b"], 667, delta=1)
        self.assertLessEqual(sheet["b"] - sheet["t"], 667 * 0.45)
        self.assertTrue(page.locator("#tutor-input").is_visible())
        self.assertTrue(page.locator("#tutor-send").is_visible())
        self.no_hscroll(page)
        # Escape and the close button both close it; the header button reopens it
        page.keyboard.press("Escape")
        page.locator("#tutor").wait_for(state="hidden")
        page.click("#tutor-open")
        page.locator("#tutor").wait_for(state="visible")
        page.click("#tutor-close")
        page.locator("#tutor").wait_for(state="hidden")
        page.evaluate("window.scrollTo(0, 0)")
        self.assertEqual(self.rect(page, "#board"), play_board)

    def test_phone_header_fits_on_the_narrowest_common_screens(self):
        for w in (320, 360, 375, 414):
            page = self.open(w, 700)
            self.learn(page)
            self.no_hscroll(page)
            for sel in ("#theme", "#tutor-open", '#mode [data-mode="learn"]', ".wordmark"):
                r = self.rect(page, sel)
                self.assertLessEqual(r["r"], w, (w, sel))
                self.assertGreaterEqual(r["l"], 0, (w, sel))

    def test_unread_replies_show_a_badge_on_the_header_button_until_opened(self):
        page = self.open(375, 667)
        self.learn(page)
        page.click("#tutor-close")
        page.locator("#tutor").wait_for(state="hidden")
        self.assertFalse(page.locator("#tutor-badge").is_visible())
        page.click("#hint"); self.settle(page)                             # a hint arrives while the sheet is shut
        self.assertTrue(page.locator("#tutor-badge").is_visible())
        self.assertEqual(page.locator("#tutor-badge").inner_text(), "1")
        self.assertIn("1 new message", page.locator("#tutor-open").get_attribute("aria-label"))
        page.click("#tutor-open")
        self.assertFalse(page.locator("#tutor-badge").is_visible())
        self.assertIn("Look at", self.last_tutor_text(page))

    def test_the_drawer_is_out_of_the_tab_order_and_hidden_from_assistive_tech_when_closed(self):
        page = self.open(375, 667)
        self.learn(page)
        page.click("#tutor-close")
        page.locator("#tutor").wait_for(state="hidden")
        self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('tutor')).visibility"), "hidden")
        self.assertEqual(page.locator("#tutor-open").get_attribute("aria-expanded"), "false")
        page.click("#tutor-open")
        self.assertEqual(page.locator("#tutor-open").get_attribute("aria-expanded"), "true")
        self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('tutor')).visibility"), "visible")

    def test_dark_theme_renders_the_tutor_readably(self):
        page = self.open(1366, 800, color_scheme="dark")
        self.learn(page)
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), "dark")
        bubble = page.evaluate("""() => { const b = document.querySelector('.msg.tutor-msg .bubble');
            const s = getComputedStyle(b); return [s.color, s.backgroundColor]; }""")
        self.assertNotEqual(bubble[0], bubble[1])

    def test_the_whole_page_still_fits_a_desktop_window_in_learn_mode(self):
        page = self.open(1280, 720)
        self.learn(page)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollHeight"), 720 + 80)
        self.no_hscroll(page)


if __name__ == "__main__":
    unittest.main()
