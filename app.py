"""
app.py
------
Flask backend for the Sudoku web application.

Every rule (mistakes, hint allowance, pause, scoring) is enforced by the Game
class in game.py; these routes only translate HTTP to Game calls. The solution
is kept server-side and sent to the browser only after the player gives up.

Run with:
    pip install flask
    python app.py
Then open http://127.0.0.1:5000 in a browser.
"""

from __future__ import annotations

import os
import sys
from functools import wraps

from flask import Flask, jsonify, render_template, request

import sudoku_logic as sl
import tutor_llm
from game import MODES, Game, GameError, GameStore, as_int

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024      # requests are tiny; refuse big ones

# Active games, newest last. Fine for a single-process app; swap for a cache or
# database to scale out.
STORE = GameStore(max_games=500)

# Optional AI wording for the tutor. None unless a key (GEMINI_API_KEY,
# OPENAI_API_KEY or ANTHROPIC_API_KEY) is set; the key is read once here and
# never leaves the server. Tests replace this.
DOTENV_FOUND = tutor_llm.load_dotenv(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
LLM = tutor_llm.from_env()
AI_KINDS = {"explain", "info", "lesson", "review"}     # replies the model may reword


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _payload() -> dict:
    data = request.get_json(silent=True)          # also parses navigator.sendBeacon
    return data if isinstance(data, dict) else {}


def _int_or_none(value):
    return None if value is None else as_int(value, "sequence number")


def _tutor_after():
    """Id of the last tutor message the page already has (None = send none)."""
    value = _payload().get("tutor_after", request.args.get("tutor_after"))
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _respond(game: Game, event: dict | None = None, fresh: bool = False):
    # Any tutor messages the page has not seen ride along on every response, so
    # coaching that follows a move or a hint needs no extra round trip.
    # `fresh` is for a brand-new page: it has none yet, so send them all.
    after = 0 if fresh else _tutor_after()
    return jsonify({"state": game.public_state(), "event": event,
                    "tutor": game.tutor_messages(after), "ai": LLM is not None})


@app.errorhandler(GameError)
def _game_error(exc: GameError):
    return jsonify({"error": exc.message, "code": exc.code}), exc.http_status


def with_game(fn):
    """Look up the game named in the request and turn rule violations into
    JSON errors that carry the current state, so the page can resynchronise."""
    @wraps(fn)
    def wrapper():
        data = _payload()
        game = STORE.get(data.get("game_id") or request.args.get("game_id"))
        if game is None:
            raise GameError("unknown_game",
                            "That game no longer exists. Start a new one.", 404)
        try:
            return fn(game, data)
        except GameError as exc:
            return jsonify({"error": exc.message, "code": exc.code,
                            "state": game.public_state(),
                            "tutor": game.tutor_messages(_tutor_after()),
                            "ai": LLM is not None}), exc.http_status
    return wrapper


@app.after_request
def _no_store(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- #
# Pages and API
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/new")
def api_new():
    """Start a game, or a fresh attempt at an earlier puzzle (`retry_of`)."""
    data = _payload()
    retry_of = data.get("retry_of")
    if retry_of:
        old = STORE.get(retry_of)
        if old is None:
            raise GameError("unknown_game", "That game no longer exists.", 404)
        game = Game(old.puzzle, old.solution, old.difficulty,
                    attempt=old.attempt + 1, mode=old.mode)
    else:
        level = data.get("difficulty", "Medium")
        if level not in sl.DIFFICULTY_PROFILE:
            raise GameError("bad_request", "Unknown difficulty.", 400)
        mode = data.get("mode", "play")
        if mode not in MODES:
            raise GameError("bad_request", "Mode must be play or learn.", 400)
        puzzle, solution = sl.generate_puzzle(level)
        game = Game(puzzle, solution, level, mode=mode)
    if game.mode == "learn":
        game.set_mode("learn")                 # greets the player in a fresh chat
    STORE.add(game)
    return _respond(game, fresh=True)


@app.get("/api/state")
@with_game
def api_state(game: Game, data: dict):
    return _respond(game)


@app.post("/api/move")
@with_game
def api_move(game: Game, data: dict):
    return _respond(game, game.move(data.get("row"), data.get("col"),
                                    data.get("value")))


@app.post("/api/undo")
@with_game
def api_undo(game: Game, data: dict):
    return _respond(game, game.undo())


@app.post("/api/hint")
@with_game
def api_hint(game: Game, data: dict):
    return _respond(game, game.hint(seq=_int_or_none(data.get("seq")),
                                    prefer=data.get("selected")))


@app.post("/api/mode")
@with_game
def api_mode(game: Game, data: dict):
    """Switch Play / Learn. Changes nothing but the mode."""
    return _respond(game, game.set_mode(data.get("mode")))


@app.post("/api/tutor")
@with_game
def api_tutor(game: Game, data: dict):
    """Ask the tutor. Free answers cost nothing; `next_move` spends a real hint."""
    selected = data.get("selected")
    event = game.tutor_ask(text=data.get("text"), intent=data.get("intent"),
                           params=data.get("params"), selected=selected,
                           seq=_int_or_none(data.get("seq")), label=data.get("label"))
    event["reply"] = _ai_wording(game, event, selected, data.get("text"))
    return _respond(game, event)


@app.get("/api/tutor/history")
@with_game
def api_tutor_history(game: Game, data: dict):
    return jsonify({"state": game.public_state(), "messages": game.tutor_messages(0),
                    "ai": tutor_llm.status(LLM)})


@app.post("/api/tutor/clear")
@with_game
def api_tutor_clear(game: Game, data: dict):
    return _respond(game, game.tutor_clear())


def _log_ai_problem(client, problem: str) -> None:
    """Say in the server terminal why the model was not used (never the key)."""
    print(f" * Tutor AI ({client.provider} / {client.model}) not used: {problem}",
          file=sys.stderr, flush=True)


def _ai_wording(game: Game, event: dict, selected, question):
    """Let the optional model reword a verified, cost-free answer.

    Runs outside the game lock (it may take seconds). Any failure leaves the
    built-in wording in place and marks the message so the page can say so.
    """
    reply = event["reply"]
    client = LLM
    if reply is None or client is None or reply.get("cost") or event["intent"] == "next_move":
        return reply
    unknown = event["intent"] == "unknown"
    if not unknown and reply.get("kind") not in AI_KINDS:
        return reply
    if not game.llm_acquire(tutor_llm.MAX_CALLS_PER_GAME):
        return reply
    new_text = None
    problem = None
    try:
        q = question if isinstance(question, str) else ""
        if unknown:
            new_text = tutor_llm.answer_open(client, q, game.tutor_open_facts(selected))
        else:
            new_text = tutor_llm.reword(client, reply["text"], q)
        if not new_text:
            problem = ("the model's reply was empty, over "
                       f"{tutor_llm.MAX_REPLY_CHARS} characters, or looked like it named a cell's digit")
    except tutor_llm.LLMError as exc:      # message is already free of the key
        problem = str(exc)
    except Exception as exc:               # AI must never be able to break the game
        problem = type(exc).__name__
    finally:
        game.llm_release()
    if problem:
        _log_ai_problem(client, problem)
    fields = {"text": new_text, "source": "ai"} if new_text else {"ai_error": True}
    return game.tutor_annotate(reply["id"], **fields) or reply


@app.post("/api/pause")
@with_game
def api_pause(game: Game, data: dict):
    changed = game.pause(str(data.get("reason", "manual")))
    return _respond(game, {"type": "pause", "changed": changed})


@app.post("/api/resume")
@with_game
def api_resume(game: Game, data: dict):
    changed = game.resume()
    return _respond(game, {"type": "resume", "changed": changed})


@app.post("/api/reset")
@with_game
def api_reset(game: Game, data: dict):
    return _respond(game, game.reset())


@app.post("/api/check")
@with_game
def api_check(game: Game, data: dict):
    return _respond(game, game.check())


@app.post("/api/solve")
@with_game
def api_solve(game: Game, data: dict):
    """Give up: ends the game (no score) and reveals the solution."""
    return _respond(game, game.give_up())


def startup_line() -> str:
    """One line for the terminal: which AI is on, or exactly why it is off."""
    if LLM is not None:
        return f" * Tutor AI: {LLM.provider} / {LLM.model}"      # never the key
    why = tutor_llm.explain_off(dotenv_found=DOTENV_FOUND)
    return f" * Tutor AI: off, built-in answers only ({why})"


if __name__ == "__main__":
    print(startup_line())
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
