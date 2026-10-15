"""
test_e2e.py
-----------
Real-browser tests: the page is driven with genuine clicks and key presses in
Chromium (via Playwright) while the Flask server runs in-process, so each test
can compare what the browser shows with what the server believes.

Skipped automatically when Playwright or a Chromium build is not available:
    pip install playwright && playwright install chromium
Run with:  python -m unittest test_e2e -v
"""

import threading
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                   # pragma: no cover
    sync_playwright = None

from werkzeug.serving import make_server

import app as web
import game as gm
import sudoku_logic as sl

COUNT_INTERVALS = """
window.__intervals = 0;
const realSetInterval = window.setInterval;
window.setInterval = function (...args) { window.__intervals++; return realSetInterval.apply(this, args); };
"""
RECORD_THEME_AT_PARSE = """
document.addEventListener('DOMContentLoaded', () => {
  window.__themeAtParse = document.documentElement.getAttribute('data-theme');
  window.__bgAtParse = getComputedStyle(document.body).backgroundColor;
});
"""


class BrowserTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("playwright is not installed")
        cls.server = make_server("127.0.0.1", 0, web.app, threaded=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:                      # pragma: no cover
            cls.pw.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chromium is not available: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()

    # Tests that deliberately provoke a 409/404 (raw API abuse, an expired saved game)
    # set this: Chromium logs every non-2xx response as a console "error" by itself.
    allow_http_errors = False

    def setUp(self):
        self.errors = []
        self.contexts = []
        self.allow_http_errors = False

    def tearDown(self):
        for ctx in self.contexts:
            ctx.close()
        errors = self.errors
        if self.allow_http_errors:
            errors = [e for e in errors if not e.startswith("console.error: Failed to load resource")]
        self.assertEqual(errors, [], "the page logged JavaScript errors")

    # -- helpers -------------------------------------------------------- #
    def open(self, width=1280, height=800, init_scripts=(), **ctx_kwargs):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, **ctx_kwargs)
        self.contexts.append(ctx)
        for script in init_scripts:
            ctx.add_init_script(script)
        page = ctx.new_page()
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: self.errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.goto(self.url)
        page.wait_for_function("S !== null && S.puzzle")
        return page

    def settle(self, page):
        page.evaluate("chain.then(() => true)")       # every queued request has finished
        page.wait_for_timeout(40)

    def game(self, page):
        return web.STORE.get(page.evaluate("S.game_id"))

    def state(self, page):
        return page.evaluate("S")

    def cell(self, page, r, c):
        return page.locator(f'.cell[data-r="{r}"][data-c="{c}"]')

    def editable(self, game):
        return [(r, c) for r in range(9) for c in range(9) if not game.puzzle[r][c]]

    def wrong_digit(self, game, r, c, avoid=()):
        return next(d for d in range(1, 10) if d != game.solution[r][c] and d not in avoid)

    def enter(self, page, r, c, digit):
        self.cell(page, r, c).click()
        page.keyboard.press(str(digit))
        self.settle(page)

    def start(self, page, level):
        page.select_option("#difficulty", level)
        page.click("#new")
        page.wait_for_function(f"S.difficulty === '{level}' && S.mistakes === 0")
        self.settle(page)

    def timer_seconds(self, page):
        mm, ss = page.inner_text("#timer").split(":")[-2:]
        return int(mm) * 60 + int(ss)

    def gameplay_controls_disabled(self, page):
        ids = ["undo", "erase", "hint", "check", "reset", "solve"]
        flags = {i: page.locator(f"#{i}").is_disabled() for i in ids}
        flags["numpad"] = all(page.locator(f'.num[data-d="{d}"]').is_disabled() for d in range(1, 10))
        return flags


# =========================================================================== #
class TestMistakeCounter(BrowserTestCase):
    def test_wrong_entry_updates_the_counter_immediately(self):                 # check 1
        page = self.open()
        game = self.game(page)
        r, c = self.editable(game)[0]
        self.assertEqual(page.inner_text("#mistakes-text"), "0/3")
        self.enter(page, r, c, self.wrong_digit(game, r, c))
        self.assertEqual(page.inner_text("#mistakes-text"), "1/3")
        self.assertEqual(page.locator("#mistake-boxes .box.struck").count(), 1)
        self.assertIn("doesn't belong", page.inner_text("#status"))
        self.assertIn("wrong", self.cell(page, r, c).get_attribute("class"))
        self.assertEqual(game.mistakes, 1)

    def test_reselecting_and_retyping_do_not_inflate_the_counter(self):         # check 2
        page = self.open()
        game = self.game(page)
        cells = self.editable(game)
        (r, c), other = cells[0], cells[1]
        w = self.wrong_digit(game, r, c)
        self.enter(page, r, c, w)
        for _ in range(8):                                  # select away and back, again and again
            self.cell(page, *other).click()
            self.cell(page, r, c).click()
        page.keyboard.press(str(w))                          # same digit onto itself
        self.settle(page)
        page.keyboard.press("Backspace")                     # erase, then type it again
        self.settle(page)
        page.keyboard.press(str(w))
        self.settle(page)
        page.click("#undo")                                  # undo and type again
        self.settle(page)
        page.keyboard.press(str(w))
        self.settle(page)
        self.assertEqual(page.inner_text("#mistakes-text"), "1/3")
        self.assertEqual(page.locator("#mistake-boxes .box.struck").count(), 1)
        self.assertEqual(game.mistakes, 1)

    def test_correcting_follows_the_documented_rules(self):                     # check 3
        page = self.open()
        game = self.game(page)
        (r, c), (r2, c2) = self.editable(game)[:2]
        w1 = self.wrong_digit(game, r, c)
        self.enter(page, r, c, w1)
        self.assertEqual(game.mistakes, 1)
        # changing a wrong digit to another wrong digit is a new mistake
        self.enter(page, r, c, self.wrong_digit(game, r, c, avoid=(w1,)))
        self.assertEqual(game.mistakes, 2)
        # replacing it with the right digit is free, and nothing is refunded
        self.enter(page, r, c, game.solution[r][c])
        self.assertEqual(page.inner_text("#mistakes-text"), "2/3")
        self.assertNotIn("wrong", self.cell(page, r, c).get_attribute("class"))
        # correct entries elsewhere cost nothing
        self.enter(page, r2, c2, game.solution[r2][c2])
        self.assertEqual(game.mistakes, 2)

    def test_the_last_life_is_flagged_without_relying_on_colour_alone(self):
        page = self.open()
        game = self.game(page)
        cells = self.editable(game)
        for r, c in cells[:2]:
            self.enter(page, r, c, self.wrong_digit(game, r, c))
        self.assertIn("One mistake left", page.inner_text("#status"))
        self.assertIn("danger", page.get_attribute("#frame", "class"))
        self.assertEqual(page.inner_text("#mistakes-text"), "2/3")


class TestHints(BrowserTestCase):
    def test_spamming_hint_cannot_exceed_the_allowance(self):                   # check 4
        self.allow_http_errors = True            # the raw API calls below are meant to be refused
        page = self.open()
        game = self.game(page)
        cap = game.rules.max_hints
        # 30 clicks in the same instant (a frantic double-click, times fifteen)
        page.evaluate("for (let i = 0; i < 30; i++) document.getElementById('hint').click()")
        self.settle(page)
        self.assertEqual(game.hints_used, 1, "simultaneous clicks must cost one hint")
        # now click as fast as the button allows, far more often than the allowance
        for _ in range(cap * 5):
            page.evaluate("document.getElementById('hint').click()")
            self.settle(page)
        self.assertEqual(game.hints_used, cap)
        self.assertEqual(self.state(page)["hints_left"], 0)
        # bypass the disabled button entirely: call the page function and the raw API
        page.evaluate("async () => { for (let i = 0; i < 10; i++) await requestHint(); }")
        statuses = page.evaluate("""async (gid) => {
            const out = [];
            for (let i = 0; i < 15; i++) {
              const r = await fetch('/api/hint', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({game_id: gid})});
              out.push(r.status);
            }
            return out; }""", game.id)
        self.assertEqual(set(statuses), {409})
        self.assertEqual(game.hints_used, cap)
        empties = sum(1 for row in self.state(page)["board"] for v in row if v == 0)
        self.assertGreater(empties, 20, "hints must not play the game for you")

    def test_exhausted_hints_disable_the_control_and_stop_generating_hints(self):   # check 5
        page = self.open()
        game = self.game(page)
        for _ in range(game.rules.max_hints):
            page.click("#hint")
            self.settle(page)
        self.assertTrue(page.locator("#hint").is_disabled())
        self.assertIn("0 left", page.inner_text("#hints-text"))
        self.assertEqual(page.locator("#hint-dots .dot.used").count(), game.rules.max_hints)
        before = (game.hints_used, game.hint_seq, self.state(page)["active_hint"])
        page.evaluate("requestHint()")
        self.settle(page)
        self.assertEqual((game.hints_used, game.hint_seq, self.state(page)["active_hint"]), before)
        self.assertIn("No hints left", page.inner_text("#status"))

    def test_hints_escalate_and_explain_instead_of_just_answering(self):
        page = self.open()
        game = self.game(page)
        before = self.state(page)["board"]
        page.click("#hint")
        self.settle(page)
        s1 = self.state(page)
        self.assertEqual(s1["board"], before, "the first hint places nothing")
        self.assertEqual(s1["active_hint"]["level"], 1)
        self.assertNotIn("cell", s1["active_hint"])
        self.assertTrue(page.is_visible("#hint-note"))
        self.assertGreaterEqual(page.locator(".cell.region").count(), 9)
        page.click("#hint")
        self.settle(page)
        s2 = self.state(page)
        r, c = s2["active_hint"]["cell"]
        self.assertEqual(s2["board"][r][c], 0, "the explanation does not fill the cell")
        self.assertEqual(page.locator(".cell.target").count(), 1)
        self.assertIn("R%dC%d" % (r + 1, c + 1), page.inner_text("#hint-text"))
        page.click("#hint")
        self.settle(page)
        s3 = self.state(page)
        self.assertEqual(s3["board"][r][c], game.solution[r][c])
        self.assertIn("revealed", self.cell(page, r, c).get_attribute("class"))
        self.assertEqual(s3["hints_used"], 3)
        # a revealed digit is locked
        self.cell(page, r, c).click()
        page.keyboard.press("Backspace")
        self.settle(page)
        self.assertEqual(self.state(page)["board"][r][c], game.solution[r][c])

    def test_selecting_a_cell_steers_the_hint_towards_it(self):
        page = self.open()
        game = self.game(page)
        masks = sl.candidate_masks(game.puzzle)
        singles = sorted(cell for cell, m in masks.items() if bin(m).count("1") == 1)
        r, c = singles[-1]
        self.cell(page, r, c).click()
        page.click("#hint")
        self.settle(page)
        page.click("#hint")
        self.settle(page)
        self.assertEqual(self.state(page)["active_hint"]["cell"], [r, c])

    def test_hint_usage_reaches_the_final_summary(self):
        page = self.open()
        game = self.game(page)
        page.click("#hint")
        self.settle(page)
        page.click("#hint")
        self.settle(page)
        self.fill_solution(page, game)
        self.assertEqual(self.state(page)["summary"]["hints_used"], 2)
        self.assertIn("2 of 4", page.inner_text("#dlg-body"))

    def fill_solution(self, page, game):
        moves = [[r, c, game.solution[r][c]] for r, c in self.editable(game) if (r, c) not in game.revealed]
        page.evaluate("""async (moves) => {
            for (const [r, c, d] of moves) { selectCell(r, c); place(d); await chain; }
        }""", moves)
        self.settle(page)


class TestPauseAndTimer(BrowserTestCase):
    def pause(self, page):
        page.click("#pause")
        page.wait_for_function("S.status === 'paused'")
        self.settle(page)

    def test_pause_freezes_time_and_blocks_every_gameplay_action(self):          # check 6
        self.allow_http_errors = True            # the raw API calls below are meant to be refused
        page = self.open()
        game = self.game(page)
        r, c = self.editable(game)[0]
        self.cell(page, r, c).click()
        page.wait_for_timeout(1100)
        self.pause(page)
        frozen_text, frozen_server = page.inner_text("#timer"), game.elapsed()
        page.wait_for_timeout(2300)
        self.assertEqual(page.inner_text("#timer"), frozen_text)
        self.assertAlmostEqual(game.elapsed(), frozen_server, delta=0.01)
        self.assertTrue(page.is_visible("#pause-overlay"))
        self.assertEqual(page.inner_text("#pause"), "Resume")
        self.assertTrue(all(self.gameplay_controls_disabled(page).values()),
                        self.gameplay_controls_disabled(page))
        # keyboard entry, erase, undo shortcut: nothing may change
        for key in ("5", "Backspace", "Control+z", "ArrowDown", "1"):
            page.keyboard.press(key)
        self.settle(page)
        self.assertEqual(game.entries, [[0] * 9 for _ in range(9)])
        self.assertEqual((game.mistakes, game.hints_used), (0, 0))
        # and the server refuses the same actions sent by hand
        codes = page.evaluate("""async (args) => {
            const [gid, r, c] = args, out = {};
            const post = async (path, extra) => {
              const res = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({game_id: gid, ...extra})});
              return [res.status, (await res.json()).code];
            };
            out.move = await post('/api/move', {row: r, col: c, value: 5});
            out.hint = await post('/api/hint', {});
            out.undo = await post('/api/undo', {});
            out.reset = await post('/api/reset', {});
            out.solve = await post('/api/solve', {});
            return out; }""", [game.id, r, c])
        for action, result in codes.items():
            self.assertEqual(result, [409, "paused"], action)
        self.assertEqual(game.status, gm.PAUSED)

    def test_clicking_anywhere_else_never_resumes_or_pauses(self):               # check 7
        page = self.open()
        game = self.game(page)
        self.pause(page)
        frozen = page.inner_text("#timer")
        # the overlay, the title, the page background, the legend, the difficulty menu,
        # the theme switch, a locked control, keys that normally act
        page.click(".pause-title")
        page.click(".pause-card", position={"x": 5, "y": 5})
        page.click(".wordmark")
        page.mouse.click(5, 400)
        page.click(".legend")
        page.click(".keys")
        page.select_option("#difficulty", "Hard")
        page.select_option("#difficulty", "Medium")
        page.click("#theme")
        page.click("#theme")
        page.locator("#hint").click(force=True)
        page.locator('.num[data-d="5"]').click(force=True)
        for key in ("Space", "Enter", "Tab", "Escape", "ArrowLeft", "3"):
            page.keyboard.press(key)
        page.evaluate("window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('blur'));")
        self.settle(page)
        page.wait_for_timeout(1500)
        self.assertEqual(self.state(page)["status"], "paused")
        self.assertEqual(game.status, gm.PAUSED)
        self.assertEqual(page.inner_text("#timer"), frozen)

    def test_only_the_explicit_resume_control_restarts_the_clock(self):           # check 8
        page = self.open()
        game = self.game(page)
        self.pause(page)
        frozen = self.timer_seconds(page)
        page.click("#resume")
        page.wait_for_function("S.status === 'playing'")
        self.settle(page)
        page.wait_for_timeout(1500)
        self.assertGreater(self.timer_seconds(page), frozen)
        self.assertEqual(game.status, gm.PLAYING)
        self.assertFalse(page.is_visible("#pause-overlay"))
        # the toolbar button also resumes (it is a Resume control while paused)
        self.pause(page)
        page.click("#pause")
        page.wait_for_function("S.status === 'playing'")
        self.assertEqual(page.inner_text("#pause"), "Pause")

    def test_pause_and_resume_change_nothing_but_the_clock(self):
        page = self.open()
        game = self.game(page)
        cells = self.editable(game)
        self.enter(page, *cells[0], self.wrong_digit(game, *cells[0]))
        self.enter(page, *cells[1], game.solution[cells[1][0]][cells[1][1]])
        page.click("#hint")
        self.settle(page)
        keys = ("board", "mistakes", "hints_used", "hints_left", "wrong", "active_hint")
        before = {k: self.state(page)[k] for k in keys}
        for _ in range(3):
            self.pause(page)
            page.click("#resume")
            page.wait_for_function("S.status === 'playing'")
        self.settle(page)
        after = {k: self.state(page)[k] for k in keys}
        self.assertEqual(after, before)

    def test_paused_time_is_excluded_from_the_total(self):
        page = self.open()
        game = self.game(page)
        page.wait_for_timeout(1200)
        self.pause(page)
        page.wait_for_timeout(3000)
        page.click("#resume")
        page.wait_for_function("S.status === 'playing'")
        page.wait_for_timeout(1200)
        self.assertLess(game.elapsed(), 3.6, "the 3 s pause must not count")
        self.assertAlmostEqual(self.timer_seconds(page), game.elapsed(), delta=1.1)

    def test_leaving_the_tab_pauses_and_returning_does_not_resume(self):           # tab visibility
        page = self.open()
        game = self.game(page)
        page.wait_for_timeout(600)
        page.evaluate("""Object.defineProperty(document, 'hidden', {value: true, configurable: true});
                         document.dispatchEvent(new Event('visibilitychange'));""")
        page.wait_for_function("S.status === 'paused'")
        self.settle(page)
        self.assertEqual(game.status, gm.PAUSED)
        self.assertEqual(game.pause_reason, "away")
        page.evaluate("""Object.defineProperty(document, 'hidden', {value: false, configurable: true});
                         document.dispatchEvent(new Event('visibilitychange'));""")
        page.wait_for_timeout(1300)
        self.assertEqual(self.state(page)["status"], "paused", "coming back must not resume")
        self.assertIn("switched away", page.inner_text("#pause-note"))
        frozen = page.inner_text("#timer")
        page.wait_for_timeout(1200)
        self.assertEqual(page.inner_text("#timer"), frozen)
        page.click("#resume")
        page.wait_for_function("S.status === 'playing'")

    def test_the_status_line_never_contradicts_the_pause_state(self):
        page = self.open()
        self.assertIn("running", page.inner_text("#status"))
        self.pause(page)
        text = page.inner_text("#status")
        self.assertIn("Paused", text)
        self.assertNotIn("running", text)
        page.click("#resume")
        page.wait_for_function("S.status === 'playing'")
        self.settle(page)
        self.assertIn("Resumed", page.inner_text("#status"))

    def test_losing_window_focus_alone_does_not_pause(self):
        page = self.open()
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        page.wait_for_timeout(500)
        self.assertEqual(self.state(page)["status"], "playing")

    def test_there_is_only_ever_one_timer_interval(self):
        page = self.open(init_scripts=[COUNT_INTERVALS])
        for _ in range(4):
            self.pause(page)
            page.click("#resume")
            page.wait_for_function("S.status === 'playing'")
        page.click("#theme")
        self.start(page, "Easy")
        self.start(page, "Hard")
        self.assertEqual(page.evaluate("window.__intervals"), 1)

    def test_the_clock_keeps_accurate_time(self):
        page = self.open()
        game = self.game(page)
        page.wait_for_timeout(3300)
        self.assertAlmostEqual(self.timer_seconds(page), game.elapsed(), delta=1.1)
        self.assertGreaterEqual(game.elapsed(), 3.2)


class TestThemes(BrowserTestCase):
    def theme(self, page):
        return page.evaluate("document.documentElement.getAttribute('data-theme')")

    def test_switching_themes_preserves_the_whole_game(self):                    # check 9
        page = self.open()
        game = self.game(page)
        cells = self.editable(game)
        self.enter(page, *cells[0], self.wrong_digit(game, *cells[0]))
        self.enter(page, *cells[1], game.solution[cells[1][0]][cells[1][1]])
        page.click("#hint")
        self.settle(page)
        keys = ("board", "mistakes", "hints_used", "hints_left", "wrong", "active_hint", "rev", "status")
        before = {k: self.state(page)[k] for k in keys}
        theme_before = self.theme(page)
        for _ in range(3):
            page.click("#theme")
        page.wait_for_timeout(1200)
        self.assertNotEqual(self.theme(page), theme_before)
        self.assertEqual({k: self.state(page)[k] for k in keys}, before, "theme must not touch game state")
        self.assertEqual(game.status, gm.PLAYING, "the clock keeps running")
        self.assertGreater(game.elapsed(), 1.0)
        self.assertEqual(page.inner_text("#mistakes-text"), "1/3")

    def test_the_toggle_is_labelled_and_works_while_paused_without_resuming(self):
        page = self.open()
        self.assertEqual(page.get_attribute("#theme", "aria-label"), "Switch to dark mode")
        page.click("#pause")
        page.wait_for_function("S.status === 'paused'")
        page.click("#theme")
        self.assertEqual(self.theme(page), "dark")
        self.assertEqual(page.get_attribute("#theme", "aria-label"), "Switch to light mode")
        self.assertEqual(page.get_attribute("#theme", "title"), "Switch to light mode")
        self.assertEqual(self.state(page)["status"], "paused")
        self.assertTrue(page.locator("#theme").is_enabled())

    def test_the_choice_persists_and_there_is_no_flash_of_the_wrong_theme(self):
        page = self.open(init_scripts=[RECORD_THEME_AT_PARSE])
        page.click("#theme")
        self.assertEqual(self.theme(page), "dark")
        page.reload()
        page.wait_for_function("S !== null")
        self.assertEqual(page.evaluate("window.__themeAtParse"), "dark",
                         "the theme must be set before the page first renders")
        self.assertEqual(page.evaluate("window.__bgAtParse"), "rgb(13, 19, 34)")
        self.assertEqual(self.theme(page), "dark")
        page.click("#theme")
        page.reload()
        page.wait_for_function("S !== null")
        self.assertEqual(page.evaluate("window.__themeAtParse"), "light")
        self.assertEqual(page.evaluate("window.__bgAtParse"), "rgb(238, 241, 247)")

    def test_first_visit_follows_the_system_theme(self):
        for scheme in ("dark", "light"):
            page = self.open(init_scripts=[RECORD_THEME_AT_PARSE], color_scheme=scheme)
            self.assertEqual(page.evaluate("window.__themeAtParse"), scheme)
            self.assertEqual(self.theme(page), scheme)

    def test_every_part_of_the_interface_follows_the_theme(self):
        page = self.open()
        self.start(page, "Medium")
        probe = """() => {
            const css = (sel, prop) => getComputedStyle(document.querySelector(sel))[prop];
            return {body: css('body', 'backgroundColor'), board: css('#frame', 'borderTopColor'),
                    clue: css('.cell.clue', 'backgroundColor'), btn: css('#undo', 'backgroundColor'),
                    primary: css('#new', 'backgroundColor'), select: css('#difficulty', 'backgroundColor'),
                    timer: css('#timer', 'color')}; }"""
        page.wait_for_timeout(300)                      # let any colour transition settle first
        light = page.evaluate(probe)
        page.click("#theme")
        page.wait_for_timeout(400)                      # the board frame fades its border over 0.2 s
        dark = page.evaluate(probe)
        for part in light:
            self.assertNotEqual(light[part], dark[part], f"{part} did not change with the theme")
        page.click("#hint")
        page.click("#solve")
        page.click("#dlg-actions button:has-text('Give up')")
        self.settle(page)
        page.click("#new")
        page.wait_for_selector("#dialog", state="hidden")
        page.evaluate("document.getElementById('dialog').showModal()")   # an open dialog also themes
        self.assertNotEqual(page.evaluate("getComputedStyle(document.getElementById('dialog')).backgroundColor"),
                            "rgb(255, 255, 255)")
        page.evaluate("document.getElementById('dialog').close()")


class TestGameEndings(BrowserTestCase):
    def fill_solution(self, page, game):
        moves = [[r, c, game.solution[r][c]] for r, c in self.editable(game) if (r, c) not in game.revealed]
        page.evaluate("""async (moves) => {
            for (const [r, c, d] of moves) { selectCell(r, c); place(d); await chain; }
        }""", moves)
        self.settle(page)

    def test_winning(self):                                                       # check 10 (win)
        page = self.open()
        game = self.game(page)
        self.fill_solution(page, game)
        page.wait_for_selector("#dialog[open]")
        self.assertEqual(self.state(page)["status"], "won")
        self.assertIn("Puzzle solved", page.inner_text("#dlg-title"))
        body = page.inner_text("#dlg-body")
        self.assertIn("0 of 3", body)
        self.assertIn("1,500", body, "a flawless Medium game scores 1,500")
        self.assertEqual(page.get_attribute("#dlg-body .stars", "aria-label"), "3 of 3 stars")
        frozen = page.inner_text("#timer")
        page.wait_for_timeout(1300)
        self.assertEqual(page.inner_text("#timer"), frozen, "the clock stops on a win")
        page.click("#dlg-actions button:has-text('Close')")
        self.assertTrue(all(self.gameplay_controls_disabled(page).values()))
        self.assertTrue(page.locator("#pause").is_disabled())
        self.assertFalse(self.state(page)["running"])
        self.assertEqual(page.evaluate("JSON.parse(localStorage.getItem('sudoku-best')).Medium"), 1500)

    def test_reaching_the_mistake_limit_ends_the_game(self):                      # check 10 (lose)
        page = self.open()
        self.start(page, "Hard")
        game = self.game(page)
        for r, c in self.editable(game)[:3]:
            self.enter(page, r, c, self.wrong_digit(game, r, c))
        page.wait_for_selector("#dialog[open]")
        self.assertEqual(self.state(page)["status"], "lost")
        self.assertIn("Out of mistakes", page.inner_text("#dlg-title"))
        self.assertEqual(page.inner_text("#mistakes-text"), "3/3")
        frozen = page.inner_text("#timer")
        page.click("#dlg-actions button:has-text('Review board')")
        self.assertTrue(all(self.gameplay_controls_disabled(page).values()))
        page.wait_for_timeout(1300)
        self.assertEqual(page.inner_text("#timer"), frozen)
        self.assertEqual(page.locator(".cell.wrong").count(), 3, "the wrong entries stay marked")
        # typing after game over changes nothing
        r, c = self.editable(game)[5]
        self.cell(page, r, c).click()
        page.keyboard.press("5")
        self.settle(page)
        self.assertEqual(self.state(page)["board"][r][c], 0)

    def test_retrying_gives_the_same_puzzle_with_fresh_counters(self):            # check 10 (restart)
        page = self.open()
        self.start(page, "Hard")
        game = self.game(page)
        puzzle = self.state(page)["puzzle"]
        page.click("#hint")
        self.settle(page)
        for r, c in self.editable(game)[:3]:
            self.enter(page, r, c, self.wrong_digit(game, r, c))
        page.wait_for_selector("#dialog[open]")
        page.click("#dlg-actions button:has-text('Try this puzzle again')")
        page.wait_for_function("S.attempt === 2")
        s = self.state(page)
        self.assertEqual(s["puzzle"], puzzle)
        self.assertEqual((s["mistakes"], s["hints_used"], s["status"]), (0, 0, "playing"))
        self.assertIn("attempt 2", page.inner_text("#level"))
        self.assertEqual(page.locator("#mistake-boxes .box.struck").count(), 0)

    def test_new_game_gives_a_different_puzzle_and_a_clean_slate(self):
        page = self.open()
        first = self.state(page)
        self.start(page, "Easy")
        second = self.state(page)
        self.assertNotEqual(second["game_id"], first["game_id"])
        self.assertEqual(sum(1 for row in second["puzzle"] for v in row if v), 42)
        self.assertEqual((second["mistakes"], second["hints_used"], second["attempt"]), (0, 0, 1))
        self.assertEqual(page.inner_text("#mistakes-text"), "0/5")
        self.assertEqual(page.inner_text("#hints-text"), "5 left")
        self.assertLess(self.timer_seconds(page), 3)

    def test_starting_over_mid_game_asks_first(self):
        page = self.open()
        game = self.game(page)
        r, c = self.editable(game)[0]
        self.enter(page, r, c, game.solution[r][c])
        page.click("#new")
        page.wait_for_selector("#dialog[open]")
        self.assertIn("Start a new game?", page.inner_text("#dlg-title"))
        page.click("#dlg-actions button:has-text('Keep playing')")
        self.assertEqual(page.evaluate("S.game_id"), game.id)
        page.click("#new")
        page.click("#dlg-actions button:has-text('New game')")
        page.wait_for_function(f"S.game_id !== '{game.id}'")

    def test_giving_up_confirms_then_reveals_the_solution_without_a_score(self):
        page = self.open()
        game = self.game(page)
        page.click("#solve")
        page.wait_for_selector("#dialog[open]")
        page.click("#dlg-actions button:has-text('Keep playing')")
        self.assertEqual(self.state(page)["status"], "playing")
        page.click("#solve")
        page.click("#dlg-actions button:has-text('Give up')")
        page.wait_for_function("S.status === 'gave_up'")
        self.settle(page)
        self.assertEqual(self.state(page)["board"], game.solution)
        self.assertIsNone(self.state(page)["summary"])
        self.assertIn("no score", page.inner_text("#status"))
        self.assertTrue(all(self.gameplay_controls_disabled(page).values()))

    def test_reset_clears_entries_but_keeps_the_costs(self):
        page = self.open()
        game = self.game(page)
        cells = self.editable(game)
        self.enter(page, *cells[0], game.solution[cells[0][0]][cells[0][1]])
        self.enter(page, *cells[1], self.wrong_digit(game, *cells[1]))
        page.click("#hint")
        self.settle(page)
        page.wait_for_timeout(1000)
        before = game.elapsed()
        page.click("#reset")
        page.click("#dlg-actions button:has-text('Reset board')")
        self.settle(page)
        s = self.state(page)
        self.assertEqual(s["board"], s["puzzle"])
        self.assertEqual((s["mistakes"], s["hints_used"]), (1, 1))
        self.assertEqual(page.locator(".cell.wrong").count(), 0)
        self.assertGreaterEqual(game.elapsed(), before, "reset must not rewind the clock")
        self.assertGreater(game.elapsed(), 1.0)
        self.assertEqual(s["status"], "playing")


class TestReload(BrowserTestCase):
    def test_a_reload_resumes_the_same_game_paused_and_refunds_nothing(self):
        page = self.open()
        game = self.game(page)
        r, c = self.editable(game)[0]
        self.enter(page, r, c, self.wrong_digit(game, r, c))
        page.click("#hint")
        self.settle(page)
        page.reload()
        page.wait_for_function("S !== null")
        page.wait_for_function("S.status === 'paused'")
        s = self.state(page)
        self.assertEqual(s["game_id"], game.id)
        self.assertEqual((s["mistakes"], s["hints_used"]), (1, 1))
        self.assertEqual(s["board"][r][c], self.state(page)["board"][r][c])
        self.assertTrue(page.is_visible("#pause-overlay"))
        self.assertEqual(game.status, gm.PAUSED)
        page.click("#resume")
        page.wait_for_function("S.status === 'playing'")

    def test_a_finished_game_is_restored_without_popping_the_dialog_again(self):
        page = self.open()
        game = self.game(page)
        page.click("#solve")
        page.click("#dlg-actions button:has-text('Give up')")
        page.wait_for_function("S.status === 'gave_up'")
        page.reload()
        page.wait_for_function("S !== null && S.status === 'gave_up'")
        self.assertFalse(page.locator("#dialog[open]").count())

    def test_an_unknown_saved_game_falls_back_to_a_new_one(self):
        self.allow_http_errors = True            # the expired id is answered with a 404, then replaced
        page = self.open()
        page.evaluate("localStorage.setItem('sudoku-game-id', 'expired')")
        page.reload()
        page.wait_for_function("S !== null && S.puzzle")
        self.assertEqual(page.evaluate("S.status"), "playing")
        self.assertNotEqual(page.evaluate("S.game_id"), "expired")


class TestFeaturesFromTheFirstVersion(BrowserTestCase):
    def test_clue_cells_cannot_be_edited_and_only_digits_are_accepted(self):
        page = self.open()
        game = self.game(page)
        clue = next((r, c) for r in range(9) for c in range(9) if game.puzzle[r][c])
        self.cell(page, *clue).click()
        page.keyboard.press("9")
        page.keyboard.press("Backspace")
        self.settle(page)
        self.assertEqual(self.state(page)["board"][clue[0]][clue[1]], game.puzzle[clue[0]][clue[1]])
        self.assertIn("locked", page.inner_text("#status"))
        r, c = self.editable(game)[0]
        self.cell(page, r, c).click()
        # Letters, symbols and 0 enter nothing. Ctrl/Alt/Meta + digit are browser shortcuts
        # (Ctrl+5 switches tabs) and must be left alone. Shift+digit is NOT excluded on purpose:
        # on layouts such as French AZERTY a digit is typed with Shift held.
        for key in ("a", "0", "-", "+", "Control+5", "Alt+5", "Meta+5"):
            page.keyboard.press(key)
        self.settle(page)
        self.assertEqual(game.entries[r][c], 0)
        self.assertEqual(game.mistakes, 0)

    def test_number_pad_erase_undo_and_arrow_keys(self):
        page = self.open()
        game = self.game(page)
        r, c = self.editable(game)[0]
        d = game.solution[r][c]
        self.cell(page, r, c).click()
        page.click(f'.num[data-d="{d}"]')
        self.settle(page)
        self.assertEqual(self.state(page)["board"][r][c], d)
        page.click("#erase")
        self.settle(page)
        self.assertEqual(self.state(page)["board"][r][c], 0)
        page.click("#undo")
        self.settle(page)
        self.assertEqual(self.state(page)["board"][r][c], d)
        page.keyboard.press("Control+z")
        self.settle(page)
        self.assertEqual(self.state(page)["board"][r][c], 0)
        self.assertTrue(page.locator("#undo").is_disabled())
        self.cell(page, 4, 4).click()
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowDown")
        self.assertEqual(page.evaluate("sel"), [5, 5])
        for _ in range(12):
            page.keyboard.press("ArrowUp")
        self.assertEqual(page.evaluate("sel"), [0, 5], "movement stops at the edge")

    def test_the_number_pad_shows_only_the_digits_one_to_nine_in_order(self):
        page = self.open()
        game = self.game(page)
        keys = page.locator(".num")
        self.assertEqual([keys.nth(i).inner_text() for i in range(keys.count())],
                         [str(d) for d in range(1, 10)], "no counts or other text on the keys")
        self.assertEqual(page.locator(".num *").count(), 0, "keys hold nothing but the digit")
        # a digit that is fully placed (all nine) can no longer be chosen; the label still says why
        d = 5
        moves = [[r, c, game.solution[r][c]] for r, c in self.editable(game)
                 if game.solution[r][c] == d]
        page.evaluate("""async (moves) => { for (const [r, c, v] of moves) { selectCell(r, c); place(v); await chain; } }""",
                      moves)
        self.settle(page)
        self.assertTrue(page.locator(f'.num[data-d="{d}"]').is_disabled())
        self.assertIn("0 left", page.get_attribute(f'.num[data-d="{d}"]', "aria-label"))
        self.assertFalse(page.locator('.num[data-d="4"]').is_disabled())

    def test_check_solution_reports_progress_without_costing_anything(self):
        page = self.open()
        game = self.game(page)
        page.click("#check")
        self.settle(page)
        self.assertIn("No mistakes so far", page.inner_text("#status"))
        r, c = self.editable(game)[0]
        self.enter(page, r, c, self.wrong_digit(game, r, c))
        page.click("#check")
        self.settle(page)
        self.assertIn("1 entry is wrong", page.inner_text("#status"))
        self.assertEqual((game.mistakes, game.hints_used), (1, 0))

    def test_conflicting_entries_are_highlighted(self):
        page = self.open()
        game = self.game(page)
        r, c = self.editable(game)[0]
        clash = next(game.puzzle[r][k] for k in range(9) if game.puzzle[r][k])
        self.enter(page, r, c, clash)
        self.assertGreaterEqual(page.locator(".cell.conflict").count(), 1)

    def test_difficulty_levels_start_the_right_puzzle_and_rules(self):
        page = self.open()
        for level, clues, mistakes, hints in (("Easy", 42, 5, 5), ("Hard", 27, 3, 3), ("Medium", 32, 3, 4)):
            self.start(page, level)
            s = self.state(page)
            self.assertEqual(sum(1 for row in s["puzzle"] for v in row if v), clues)
            self.assertEqual(page.inner_text("#mistakes-text"), f"0/{mistakes}")
            self.assertEqual(page.inner_text("#hints-text"), f"{hints} left")
            self.assertEqual(page.inner_text("#level"), level)
            self.assertEqual(page.locator("#mistake-boxes .box").count(), mistakes)
            self.assertEqual(page.locator("#hint-dots .dot").count(), hints)

    def test_printed_and_typed_digits_use_different_typefaces(self):
        page = self.open()
        game = self.game(page)
        r, c = self.editable(game)[0]
        self.enter(page, r, c, game.solution[r][c])
        fonts = page.evaluate("""([r, c]) => {
            const clue = document.querySelector('.cell.clue');
            const mine = document.querySelector(`.cell[data-r="${r}"][data-c="${c}"]`);
            return [getComputedStyle(clue).fontFamily, getComputedStyle(mine).fontFamily]; }""", [r, c])
        self.assertNotEqual(fonts[0], fonts[1])


class TestResponsiveAndAccessible(BrowserTestCase):
    SIZES = [(1440, 900), (1280, 800), (1024, 768), (900, 700), (768, 1024),
             (414, 896), (390, 844), (375, 667), (320, 568)]

    def test_no_horizontal_scroll_and_everything_reachable_at_every_size(self):      # check 11
        for width, height in self.SIZES:
            page = self.open(width, height, has_touch=width < 700)
            m = page.evaluate("""() => {
                const box = (s) => document.querySelector(s).getBoundingClientRect();
                return {sw: document.documentElement.scrollWidth, iw: innerWidth,
                        sh: document.documentElement.scrollHeight, ih: innerHeight,
                        board: box('#board'), numpad: box('#numpad'), timer: box('#timer'),
                        tools: box('.tools'), theme: box('#theme')}; }""")
            label = f"{width}x{height}"
            self.assertLessEqual(m["sw"], m["iw"], f"{label}: horizontal scroll")
            self.assertGreaterEqual(m["board"]["x"], 0, label)
            self.assertLessEqual(m["board"]["x"] + m["board"]["width"], m["iw"], f"{label}: board clipped")
            self.assertGreater(m["board"]["width"], 270, f"{label}: board too small")
            self.assertAlmostEqual(m["board"]["width"], m["board"]["height"], delta=2, msg=label)
            for part in ("numpad", "timer", "tools", "theme"):
                self.assertGreater(m[part]["width"], 20, f"{label}: {part} has no size")
            self.assertLessEqual(m["theme"]["x"] + m["theme"]["width"], m["iw"], f"{label}: theme toggle clipped")
            if width >= 900 and height >= 800:
                self.assertLessEqual(m["sh"], m["ih"] + 1, f"{label}: desktop should fit without scrolling")
            if width < 700 and height >= 667:
                self.assertLessEqual(m["numpad"]["y"] + m["numpad"]["height"], m["ih"],
                                     f"{label}: number pad should be reachable without scrolling")
            touch = page.evaluate("""() => [...document.querySelectorAll('.num, .tools .btn')].map(b => {
                const r = b.getBoundingClientRect(); return [r.width, r.height]; })""")
            # Height is always >= 44. Width is >= 44 from tablet up; on a phone nine digit keys
            # share one row, so they only have to meet the WCAG 2.2 AA minimum of 24 CSS px.
            floor = 24 if width < 700 else 44
            self.assertTrue(all(w >= floor and h >= 44 for w, h in touch),
                            f"{label}: touch targets too small {min(touch)}")

    def test_a_phone_can_play_a_move_with_taps_only(self):
        page = self.open(375, 667, has_touch=True, is_mobile=True)
        game = self.game(page)
        r, c = self.editable(game)[0]
        self.cell(page, r, c).tap()
        page.locator(f'.num[data-d="{game.solution[r][c]}"]').tap()
        self.settle(page)
        self.assertEqual(self.state(page)["board"][r][c], game.solution[r][c])
        # the cells are not text fields, so no on-screen keyboard is summoned
        self.assertEqual(page.evaluate("document.querySelector('.cell').tagName"), "DIV")

    def test_the_hint_text_is_on_screen_next_to_the_board_on_a_phone(self):
        for width, height in ((375, 667), (390, 844)):
            page = self.open(width, height, has_touch=True)
            for _ in range(2):                                   # nudge, then explanation
                page.click("#hint")
                self.settle(page)
                m = page.evaluate("""() => {
                    const r = (id) => document.getElementById(id).getBoundingClientRect();
                    return {board: r('frame'), note: r('hint-note'), numpad: r('numpad'), tools: r('hint'),
                            ih: innerHeight, scrolled: scrollY}; }""")
                label = f"{width}x{height}"
                self.assertTrue(page.is_visible("#hint-note"), label)
                self.assertGreaterEqual(m["note"]["top"], m["board"]["bottom"], f"{label}: note must sit under the board")
                self.assertLessEqual(m["note"]["bottom"], m["ih"], f"{label}: hint text is below the fold")
                self.assertLessEqual(m["numpad"]["bottom"], m["ih"], f"{label}: number pad pushed off screen")
                self.assertLessEqual(m["tools"]["bottom"], m["ih"], f"{label}: Hint button pushed off screen")
                self.assertEqual(m["scrolled"], 0)

    def test_the_hint_note_fills_the_space_under_the_board_on_desktop(self):
        page = self.open(1280, 800)
        page.click("#hint")
        self.settle(page)
        m = page.evaluate("""() => { const r = (id) => document.getElementById(id).getBoundingClientRect();
            return {board: r('frame'), note: r('hint-note'), iw: innerWidth, sh: document.documentElement.scrollHeight}; }""")
        self.assertGreaterEqual(m["note"]["top"], m["board"]["bottom"])
        self.assertAlmostEqual(m["note"]["width"], m["board"]["width"], delta=2)
        self.assertLessEqual(m["sh"], 800 + 1, "the note must not make the desktop page scroll")

    def test_the_overlay_and_dialogs_fit_a_small_screen(self):
        page = self.open(320, 568, has_touch=True)
        page.click("#pause")
        page.wait_for_function("S.status === 'paused'")
        box = page.evaluate("""() => { const o = document.getElementById('pause-overlay').getBoundingClientRect();
                                       const b = document.getElementById('resume').getBoundingClientRect();
                                       return [o.width, b.x, b.x + b.width, innerWidth]; }""")
        self.assertGreaterEqual(box[1], 0)
        self.assertLessEqual(box[2], box[3])
        page.click("#resume")
        page.wait_for_function("S.status === 'playing'")
        page.click("#solve")
        page.wait_for_selector("#dialog[open]")
        d = page.evaluate("(() => { const r = document.getElementById('dialog').getBoundingClientRect(); return [r.x, r.width, innerWidth]; })()")
        self.assertGreaterEqual(d[0], 0)
        self.assertLessEqual(d[0] + d[1], d[2])

    def test_semantics_for_assistive_technology(self):
        page = self.open()
        game = self.game(page)
        self.assertEqual(page.get_attribute("#board", "role"), "grid")
        self.assertEqual(page.locator('[role="gridcell"]').count(), 81)
        self.assertEqual(page.get_attribute("#status", "aria-live"), "polite")
        self.assertEqual(page.get_attribute("#dialog", "aria-labelledby"), "dlg-title")
        self.assertEqual(page.get_attribute("#timer", "role"), "timer")
        unnamed = page.evaluate("""() => [...document.querySelectorAll('button, select')]
            .filter(b => !(b.getAttribute('aria-label') || b.textContent.trim() || b.labels?.length)).length""")
        self.assertEqual(unnamed, 0, "every control needs an accessible name")
        r, c = self.editable(game)[0]
        self.assertIn("empty", self.cell(page, r, c).get_attribute("aria-label"))
        clue = next((r, c) for r in range(9) for c in range(9) if game.puzzle[r][c])
        self.assertIn("printed clue", self.cell(page, *clue).get_attribute("aria-label"))
        self.enter(page, r, c, self.wrong_digit(game, r, c))
        self.assertIn("incorrect", self.cell(page, r, c).get_attribute("aria-label"))
        self.assertIn("Uses 1 of your 4 remaining hints", page.get_attribute("#hint", "aria-label"))
        # the grid is reachable from the keyboard (one cell is in the tab order)
        self.assertEqual(page.locator('.cell[tabindex="0"]').count(), 1)
        page.keyboard.press("Tab")
        self.assertTrue(page.evaluate("document.activeElement !== document.body"))

    def test_reduced_motion_is_respected(self):
        page = self.open(reduced_motion="reduce")
        durations = page.evaluate("""() => { const el = document.createElement('div'); el.className = 'cell shake';
            document.body.appendChild(el); const d = getComputedStyle(el).animationDuration; el.remove(); return d; }""")
        self.assertEqual(durations, "1e-05s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
