"""
test_api.py
-----------
HTTP-level tests of the Flask API. These exercise the same endpoints the page
uses, including the abuse cases: spam clicks, replayed requests, concurrent
double-clicks and acting while paused.

Run with:  python -m unittest test_api
"""

import json
import threading
import unittest

import app as web
import game as gm
import sudoku_logic as sl


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()

    # -- helpers -------------------------------------------------------- #
    def post(self, path, **body):
        resp = self.client.post(path, json=body)
        return resp.status_code, resp.get_json()

    def new(self, level="Easy", **extra):
        status, body = self.post("/api/new", difficulty=level, **extra)
        self.assertEqual(status, 200, body)
        return body["state"]

    def server_game(self, state):
        return web.STORE.get(state["game_id"])

    def editable(self, state):
        return [(r, c) for r in range(9) for c in range(9) if not state["puzzle"][r][c]]

    def move(self, state, r, c, v):
        return self.post("/api/move", game_id=state["game_id"], row=r, col=c, value=v)

    def wrong(self, state, r, c, avoid=()):
        sol = self.server_game(state).solution
        return next(d for d in range(1, 10) if d != sol[r][c] and d not in avoid)


class TestNewGameAndState(ApiTestCase):
    def test_index_and_static_files_are_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)
        self.assertEqual(self.client.get("/static/script.js").status_code, 200)

    def test_each_level_starts_a_unique_puzzle_with_its_rules(self):
        for level, rules in gm.RULES.items():
            state = self.new(level)
            self.assertEqual(state["status"], "playing")
            self.assertEqual(state["difficulty"], level)
            self.assertEqual(state["max_mistakes"], rules.max_mistakes)
            self.assertEqual(state["max_hints"], rules.max_hints)
            self.assertEqual(state["hints_left"], rules.max_hints)
            self.assertEqual(state["mistakes"], 0)
            clues = sum(1 for row in state["puzzle"] for v in row if v)
            self.assertEqual(clues, sl.DIFFICULTY_PROFILE[level]["clues"])
            self.assertEqual(sl.count_solutions(state["puzzle"]), 1)

    def test_the_solution_is_never_sent_while_playing(self):
        state = self.new("Medium")
        self.assertNotIn("solution", state)
        solution = self.server_game(state).solution
        self.assertNotEqual(state["board"], solution)
        self.assertNotIn(json.dumps(solution), json.dumps(state))

    def test_unknown_difficulty_and_unknown_game(self):
        status, body = self.post("/api/new", difficulty="Nightmare")
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        resp = self.client.get("/api/state?game_id=nope")
        self.assertEqual((resp.status_code, resp.get_json()["code"]), (404, "unknown_game"))
        self.assertEqual(self.client.get("/api/state").status_code, 404)
        self.assertEqual(self.post("/api/move", game_id="x", row=0, col=0, value=1)[0], 404)

    def test_state_can_be_restored_after_a_reload(self):
        state = self.new()
        r, c = self.editable(state)[0]
        self.move(state, r, c, self.wrong(state, r, c))
        restored = self.client.get(f"/api/state?game_id={state['game_id']}").get_json()["state"]
        self.assertEqual(restored["mistakes"], 1)
        self.assertEqual(restored["board"][r][c], self.wrong(state, r, c))

    def test_garbage_bodies_never_cause_a_server_error(self):
        state = self.new()
        for raw in (b"", b"not json", b"[1,2,3]", b'"str"', b"null"):
            resp = self.client.post("/api/move", data=raw, content_type="application/json")
            self.assertIn(resp.status_code, (404, 409, 400), raw)
        status, body = self.post("/api/hint", game_id=state["game_id"], seq="abc")
        self.assertEqual((status, body["code"]), (400, "bad_request"))

    def test_api_responses_are_not_cacheable(self):
        state = self.new()
        resp = self.client.get(f"/api/state?game_id={state['game_id']}")
        self.assertEqual(resp.headers["Cache-Control"], "no-store")


class TestMistakesOverHttp(ApiTestCase):
    def test_wrong_entry_updates_the_counter_and_flags_the_cell(self):
        state = self.new()
        r, c = self.editable(state)[0]
        status, body = self.move(state, r, c, self.wrong(state, r, c))
        self.assertEqual(status, 200)
        self.assertEqual(body["event"]["type"], "mistake")
        self.assertEqual(body["state"]["mistakes"], 1)
        self.assertEqual(body["state"]["wrong"], [[r, c]])

    def test_repeating_the_same_request_never_inflates_the_counter(self):
        state = self.new()
        r, c = self.editable(state)[0]
        w = self.wrong(state, r, c)
        for _ in range(25):                               # spam clicks / replays
            _, body = self.move(state, r, c, w)
        self.assertEqual(body["state"]["mistakes"], 1)
        for _ in range(5):                                # erase and retype
            self.move(state, r, c, 0)
            _, body = self.move(state, r, c, w)
        self.assertEqual(body["state"]["mistakes"], 1)

    def test_correct_entries_are_free_and_correcting_never_refunds(self):
        state = self.new()
        sol = self.server_game(state).solution
        cells = self.editable(state)
        for r, c in cells[:5]:
            _, body = self.move(state, r, c, sol[r][c])
            self.assertEqual(body["event"]["type"], "correct")
        self.assertEqual(body["state"]["mistakes"], 0)
        r, c = cells[5]
        self.move(state, r, c, self.wrong(state, r, c))
        _, body = self.move(state, r, c, sol[r][c])
        self.assertEqual(body["state"]["mistakes"], 1)
        self.assertEqual(body["state"]["wrong"], [])

    def test_clue_cells_are_locked_and_bad_input_rejected(self):
        state = self.new()
        r, c = next((r, c) for r in range(9) for c in range(9) if state["puzzle"][r][c])
        status, body = self.move(state, r, c, 3)
        self.assertEqual((status, body["code"]), (409, "locked"))
        r, c = self.editable(state)[0]
        for bad in (0.5, "x", 10, -1, None):
            status, body = self.move(state, r, c, bad)
            self.assertEqual((status, body["code"]), (400, "bad_request"), bad)

    def test_reaching_the_limit_ends_the_game(self):
        state = self.new("Hard")
        limit = state["max_mistakes"]
        for r, c in self.editable(state)[:limit]:
            _, body = self.move(state, r, c, self.wrong(state, r, c))
        self.assertEqual(body["state"]["status"], "lost")
        self.assertEqual(body["state"]["mistakes"], limit)
        r, c = self.editable(state)[limit]
        status, body = self.move(state, r, c, 1)
        self.assertEqual((status, body["code"]), (409, "finished"))


class TestHintsOverHttp(ApiTestCase):
    def hint(self, state, **extra):
        return self.post("/api/hint", game_id=state["game_id"], **extra)

    def test_hint_levels_and_what_each_reveals(self):
        state = self.new("Medium")
        _, b1 = self.hint(state, seq=0)
        view = b1["state"]["active_hint"]
        self.assertEqual(view["level"], 1)
        self.assertNotIn("cell", view)
        self.assertEqual(b1["state"]["board"], state["board"], "nudge changes no cell")
        _, b2 = self.hint(state, seq=b1["state"]["hint_seq"])
        r, c = b2["state"]["active_hint"]["cell"]
        self.assertEqual(b2["state"]["board"][r][c], 0, "explain does not fill the cell")
        _, b3 = self.hint(state, seq=b2["state"]["hint_seq"])
        self.assertEqual(b3["state"]["board"][r][c], self.server_game(state).solution[r][c])
        self.assertEqual(b3["state"]["hints_left"], gm.RULES["Medium"].max_hints - 3)

    def test_spamming_the_hint_endpoint_stops_at_the_allowance(self):
        for level in ("Easy", "Medium", "Hard"):
            state = self.new(level)
            cap = state["max_hints"]
            codes = []
            for _ in range(cap * 5):
                status, body = self.hint(state)          # no seq: worst case spam
                codes.append(status)
            self.assertEqual(codes.count(200), cap)
            self.assertEqual(set(codes) - {200}, {409})
            latest = self.client.get(f"/api/state?game_id={state['game_id']}").get_json()["state"]
            self.assertEqual((latest["hints_used"], latest["hints_left"]), (cap, 0))
            empties = sum(1 for row in latest["board"] for v in row if v == 0)
            self.assertGreater(empties, 20)
            self.assertEqual(latest["status"], "playing")

    def test_exhausted_hints_return_the_state_without_changing_it(self):
        state = self.new("Hard")
        for _ in range(state["max_hints"]):
            self.hint(state)
        status, body = self.hint(state)
        self.assertEqual((status, body["code"]), (409, "no_hints"))
        self.assertEqual(body["state"]["hints_left"], 0)

    def test_replayed_sequence_numbers_are_charged_once(self):
        state = self.new()
        for _ in range(10):
            status, body = self.hint(state, seq=0)
            self.assertEqual(status, 200)
        self.assertEqual(body["state"]["hints_used"], 1)

    def test_concurrent_double_clicks_are_charged_once(self):
        state = self.new()
        outcomes, barrier = [], threading.Barrier(25)

        def click():
            client = web.app.test_client()
            barrier.wait()
            resp = client.post("/api/hint", json={"game_id": state["game_id"], "seq": 0})
            outcomes.append(resp.get_json()["event"]["type"])

        threads = [threading.Thread(target=click) for _ in range(25)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(outcomes.count("hint"), 1)
        self.assertEqual(self.server_game(state).hints_used, 1)

    def test_hint_totals_appear_in_the_final_summary(self):
        state = self.new()
        self.hint(state)
        self.hint(state)
        game = self.server_game(state)
        for r, c in self.editable(state):
            if (r, c) not in game.revealed:
                _, body = self.move(state, r, c, game.solution[r][c])
        self.assertEqual(body["state"]["status"], "won")
        self.assertEqual(body["state"]["summary"]["hints_used"], 2)


class TestPauseOverHttp(ApiTestCase):
    def test_paused_games_reject_every_puzzle_action(self):
        state = self.new()
        gid = state["game_id"]
        r, c = self.editable(state)[0]
        self.post("/api/pause", game_id=gid)
        calls = [
            ("/api/move",  dict(row=r, col=c, value=1)),
            ("/api/undo",  {}),
            ("/api/hint",  {}),
            ("/api/reset", {}),
            ("/api/check", {}),
            ("/api/solve", {}),
        ]
        for path, extra in calls:
            status, body = self.post(path, game_id=gid, **extra)
            self.assertEqual((status, body["code"]), (409, "paused"), path)
            self.assertEqual(body["state"]["status"], "paused")
        game = self.server_game(state)
        self.assertEqual(game.mistakes, 0)
        self.assertEqual(game.hints_used, 0)

    def test_state_reads_never_resume_a_paused_game_and_time_is_frozen(self):
        state = self.new()
        gid = state["game_id"]
        _, body = self.post("/api/pause", game_id=gid, reason="manual")
        frozen = body["state"]["elapsed"]
        for _ in range(5):
            got = self.client.get(f"/api/state?game_id={gid}").get_json()["state"]
            self.assertEqual((got["status"], got["running"], got["elapsed"]),
                             ("paused", False, frozen))
        self.post("/api/pause", game_id=gid, reason="away")            # repeated pause
        got = self.client.get(f"/api/state?game_id={gid}").get_json()["state"]
        self.assertEqual(got["pause_reason"], "manual")

    def test_only_resume_restarts_the_game(self):
        state = self.new()
        gid = state["game_id"]
        self.post("/api/pause", game_id=gid)
        status, body = self.post("/api/resume", game_id=gid)
        self.assertEqual((status, body["state"]["status"]), (200, "playing"))
        self.assertTrue(body["state"]["running"])
        _, again = self.post("/api/resume", game_id=gid)
        self.assertFalse(again["event"]["changed"])

    def test_pausing_works_when_sent_as_a_beacon(self):
        """navigator.sendBeacon posts a JSON Blob; it must still pause."""
        state = self.new()
        resp = self.client.post("/api/pause",
                                data=json.dumps({"game_id": state["game_id"], "reason": "away"}),
                                content_type="application/json")
        self.assertEqual(resp.get_json()["state"]["pause_reason"], "away")


class TestLifecycleOverHttp(ApiTestCase):
    def test_reset_clears_entries_but_keeps_mistakes_and_hints(self):
        state = self.new("Medium")
        gid = state["game_id"]
        sol = self.server_game(state).solution
        cells = self.editable(state)
        self.move(state, *cells[0], sol[cells[0][0]][cells[0][1]])
        self.move(state, *cells[1], self.wrong(state, *cells[1]))
        self.post("/api/hint", game_id=gid)
        status, body = self.post("/api/reset", game_id=gid)
        st = body["state"]
        self.assertEqual(status, 200)
        self.assertEqual(st["board"], state["puzzle"])
        self.assertEqual((st["mistakes"], st["hints_used"]), (1, 1))
        self.assertEqual(st["wrong"], [])

    def test_undo_restores_the_previous_value_without_refunds(self):
        state = self.new()
        gid = state["game_id"]
        r, c = self.editable(state)[0]
        self.move(state, r, c, self.wrong(state, r, c))
        status, body = self.post("/api/undo", game_id=gid)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"]["board"][r][c], 0)
        self.assertEqual(body["state"]["mistakes"], 1)
        self.assertEqual(self.post("/api/undo", game_id=gid)[1]["code"], "nothing_to_undo")

    def test_give_up_reveals_the_solution_and_ends_the_game(self):
        state = self.new()
        status, body = self.post("/api/solve", game_id=state["game_id"])
        st = body["state"]
        self.assertEqual((status, st["status"]), (200, "gave_up"))
        self.assertTrue(sl.is_complete_and_valid(st["board"]))
        self.assertIsNone(st["summary"])
        status, body = self.post("/api/check", game_id=state["game_id"])
        self.assertEqual((status, body["code"]), (409, "finished"))

    def test_winning_produces_a_summary(self):
        state = self.new("Easy")
        sol = self.server_game(state).solution
        for r, c in self.editable(state):
            _, body = self.move(state, r, c, sol[r][c])
        st = body["state"]
        self.assertEqual(st["status"], "won")
        self.assertTrue(st["summary"]["flawless"])
        self.assertEqual(st["summary"]["score"], 1000)
        self.assertEqual(st["summary"]["stars"], 3)
        self.assertFalse(st["running"])

    def test_retry_is_a_fresh_attempt_at_the_same_puzzle(self):
        state = self.new("Hard")
        for r, c in self.editable(state)[:state["max_mistakes"]]:
            self.move(state, r, c, self.wrong(state, r, c))
        status, body = self.post("/api/new", retry_of=state["game_id"])
        fresh = body["state"]
        self.assertEqual(status, 200)
        self.assertNotEqual(fresh["game_id"], state["game_id"])
        self.assertEqual(fresh["puzzle"], state["puzzle"])
        self.assertEqual(fresh["difficulty"], "Hard")
        self.assertEqual((fresh["attempt"], fresh["mistakes"], fresh["hints_used"]), (2, 0, 0))
        self.assertEqual(fresh["status"], "playing")
        old = self.client.get(f"/api/state?game_id={state['game_id']}").get_json()["state"]
        self.assertEqual(old["status"], "lost", "the old game stays finished")
        self.assertEqual(self.post("/api/new", retry_of="nope")[0], 404)

    def test_check_reports_progress(self):
        state = self.new()
        _, body = self.post("/api/check", game_id=state["game_id"])
        self.assertEqual(body["event"]["status"], "ok")
        r, c = self.editable(state)[0]
        self.move(state, r, c, self.wrong(state, r, c))
        _, body = self.post("/api/check", game_id=state["game_id"])
        self.assertEqual((body["event"]["status"], body["event"]["wrong"]), ("wrong", 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
