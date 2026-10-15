"""
tutor_llm.py
------------
OPTIONAL language-model wording for the tutor. The app is complete without it.

What the model is (and is not) allowed to do
    * It never decides what is true. The rules engine in tutor.py has already
      worked out the answer; the model may only reword it, or answer an
      open-ended question from a short list of facts.
    * It only ever receives text the player may already see. It is never sent
      the solution, the candidates of an unsolved cell, or the full board.
    * Hints are never routed through it (they carry the cost and the answer).
    * Its output is checked before use. A reply that is too long, looks like an
      instruction to fill a cell, or states what goes in a cell is thrown away
      and the built-in wording is used instead.
    * Any failure (no key, timeout, HTTP error, odd reply) silently falls back
      to the built-in wording. The game never waits on, or breaks because of, AI.

Providers: Anthropic, OpenAI and Google Gemini. Setup is described in README.md.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

API_URL = "https://api.anthropic.com/v1/messages"          # Anthropic
API_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"  # OpenAI
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-4o-mini",
                  "gemini": "gemini-3.5-flash-lite"}
DEFAULT_MODEL = DEFAULT_MODELS["anthropic"]                 # kept for older callers
PROVIDERS = tuple(DEFAULT_MODELS)
TIMEOUT_SECONDS = 8
GEMINI_TIMEOUT_SECONDS = 10
MAX_TOKENS = 300
OPENAI_MAX_TOKENS = 700          # reasoning models spend part of this budget before they answer
GEMINI_MAX_TOKENS = 1024         # same reason: Gemini "thinking" tokens count against the limit
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_REPLY_CHARS = 600
MAX_CALLS_PER_GAME = 40          # a hard ceiling on cost per game

SYSTEM_PROMPT = (
    "You are the Sudoku Tutor inside a puzzle app. A rules engine has already worked out "
    "the facts you are given; they are the only facts you may rely on. Never say or hint "
    "which digit belongs in an empty cell, never tell the player which cell to fill, and "
    "never solve the puzzle. If asked to, decline kindly and offer to explain a technique "
    "instead. Reply in at most four short sentences of plain text with no markdown, in a "
    "friendly, encouraging tone, and refer to cells like R3C5. If the facts don't answer "
    "the question, say what you can help with: explaining a cell, why a digit doesn't "
    "fit, what a row is missing, techniques, or reviewing moves."
)

Transport = Callable[[str, dict, bytes, float], Tuple[int, bytes]]


class LLMError(Exception):
    """Anything that stops an AI reply from being used."""


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"network error: {type(exc).__name__}") from None


def _error_detail(raw: bytes, secret: str) -> str:
    """The provider's own explanation of a failure (e.g. "quota exceeded"), shortened
    and with the key removed, so a failing setup can be diagnosed from the terminal."""
    try:
        err = json.loads(raw)["error"]
        text = err["message"] if isinstance(err, dict) else err
        text = " ".join(str(text).split())
    except (ValueError, KeyError, TypeError):
        return ""
    if secret:
        text = text.replace(secret, "***")
    return text[:200]


class Client:
    def __init__(self, api_key: str, model: Optional[str] = None,
                 transport: Optional[Transport] = None, provider: str = "anthropic"):
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider!r}")
        self._api_key = api_key                    # never logged, never returned
        self.provider = provider
        model = model or DEFAULT_MODELS[provider]
        if provider == "gemini":                   # the name becomes part of the URL
            model = model.strip().removeprefix("models/")
            if not _MODEL_NAME.fullmatch(model):
                raise ValueError(f"Not a usable model name: {model!r}")
        self.model = model
        self._transport = transport or _urllib_transport

    def complete(self, system: str, user: str) -> str:
        timeout = TIMEOUT_SECONDS
        if self.provider == "gemini":
            timeout = GEMINI_TIMEOUT_SECONDS
            url = GEMINI_URL.format(model=self.model)
            headers = {"content-type": "application/json", "x-goog-api-key": self._api_key}
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"maxOutputTokens": GEMINI_MAX_TOKENS,
                                         "temperature": 0.4}}
        elif self.provider == "openai":
            url = OPENAI_URL
            headers = {"content-type": "application/json",
                       "authorization": f"Bearer {self._api_key}"}
            body = {"model": self.model, "max_completion_tokens": OPENAI_MAX_TOKENS,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
        else:
            url = API_URL
            headers = {"content-type": "application/json", "x-api-key": self._api_key,
                       "anthropic-version": API_VERSION}
            body = {"model": self.model, "max_tokens": MAX_TOKENS, "system": system,
                    "messages": [{"role": "user", "content": user}]}
        status, raw = self._transport(url, headers, json.dumps(body).encode(), timeout)
        if status != 200:
            detail = _error_detail(raw, self._api_key)
            raise LLMError(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        try:
            data = json.loads(raw)
            if self.provider == "gemini":
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts
                               if not p.get("thought")).strip()
            if self.provider == "openai":
                return (data["choices"][0]["message"]["content"] or "").strip()
            return "".join(b.get("text", "") for b in data["content"]
                           if b.get("type") == "text").strip()
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("unreadable reply") from None

    def __repr__(self) -> str:                     # keep the key out of tracebacks and logs
        return f"Client(provider={self.provider!r}, model={self.model!r})"


def load_dotenv(path, environ=None) -> bool:
    """Read KEY=VALUE lines from a local .env file into the environment.

    For keeping your own key on your own machine: the file is never part of the
    project ZIP and is never served to the browser. Real environment variables
    win over the file. Returns True if a file was read."""
    env = os.environ if environ is None else environ
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in env:
            env[key] = value
    return True


def from_env(environ=None, transport: Optional[Transport] = None) -> Optional[Client]:
    env = os.environ if environ is None else environ
    if env.get("TUTOR_AI", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
    keys = {"anthropic": (env.get("ANTHROPIC_API_KEY") or "").strip(),
            "openai": (env.get("OPENAI_API_KEY") or "").strip(),
            "gemini": (env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip()}
    wanted = (env.get("TUTOR_PROVIDER") or "").strip().lower()
    if wanted:
        if wanted not in PROVIDERS or not keys[wanted]:
            return None                            # asked for a provider we have no key for
        provider = wanted
    else:
        provider = next((p for p in PROVIDERS if keys[p]), None)
        if provider is None:
            return None
    try:
        return Client(keys[provider], (env.get("TUTOR_MODEL") or "").strip() or None,
                      transport, provider)
    except ValueError:                             # an unusable TUTOR_MODEL: AI off, game on
        return None


_KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
             "gemini": "GEMINI_API_KEY"}


def explain_off(environ=None, dotenv_found: Optional[bool] = None) -> str:
    """In plain words, why `from_env` gave no client ("" if it would give one).

    Printed in the server terminal so a mistake in `.env` is visible at once. It
    never contains a key: any key value found in the settings is masked."""
    env = os.environ if environ is None else environ
    if from_env(env) is not None:
        return ""
    secrets = [v.strip() for k, v in env.items()
               if k.endswith("API_KEY") and isinstance(v, str) and len(v.strip()) >= 6]

    def safe(text: str) -> str:
        for s in secrets:
            text = text.replace(s, "***")
        return text[:60]

    if env.get("TUTOR_AI", "on").strip().lower() in ("off", "0", "false", "no"):
        return "TUTOR_AI is set to off"
    have = {p: bool((env.get(v) or "").strip()) for p, v in _KEY_VARS.items()}
    have["gemini"] = have["gemini"] or bool((env.get("GOOGLE_API_KEY") or "").strip())
    wanted = (env.get("TUTOR_PROVIDER") or "").strip().lower()
    if wanted and wanted not in PROVIDERS:
        return (f"TUTOR_PROVIDER is {safe(wanted)!r} but must be one of "
                f"{', '.join(PROVIDERS)}")
    if wanted and not have[wanted]:
        return f"TUTOR_PROVIDER is {wanted} but {_KEY_VARS[wanted]} is empty or missing"
    if not any(have.values()):
        where = {True: "the .env file was read but holds no key",
                 False: "no .env file was found next to app.py",
                 None: "set a key in .env or the environment"}[dotenv_found]
        return f"no API key found ({where})"
    return f"TUTOR_MODEL {safe((env.get('TUTOR_MODEL') or '').strip())!r} is not a usable model name"


def status(client: Optional[Client]) -> dict:
    """What the browser may know: whether AI wording is on. Never the key."""
    return {"enabled": client is not None, "model": client.model if client else None,
            "provider": client.provider if client else None}


# --------------------------------------------------------------------------- #
# Checking what comes back
# --------------------------------------------------------------------------- #
_ASSIGNMENT = re.compile(
    r"\bR[1-9]C[1-9]\b\s*(?:=|:|is|must be|should be|has to be|goes|equals|takes)\s*(?:a |an |the )?[1-9]\b"
    r"|\b(?:put|place|write|enter|type|fill)\b[^.]{0,20}\b[1-9]\b[^.]{0,12}\b(?:in|into|at)\b[^.]{0,6}\bR[1-9]C[1-9]\b"
    r"|\b(?:the|your)\s+answer\s+is\b"
    r"|\bthe\s+(?:digit|number)\s+(?:for|in)\b[^.]{0,20}\bis\s+[1-9]\b",
    re.IGNORECASE)


def accept(text: Optional[str]) -> Optional[str]:
    """Return the text if it is safe to show, otherwise None."""
    if not text:
        return None
    text = re.sub(r"[*_`#>]+", "", text).strip()          # no markdown
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text or len(text) > MAX_REPLY_CHARS:
        return None
    if _ASSIGNMENT.search(text):
        return None
    return text


# --------------------------------------------------------------------------- #
# The two things the model is asked to do
# --------------------------------------------------------------------------- #
def reword(client: Client, verified_text: str, question: str = "") -> Optional[str]:
    """Rephrase an already-verified explanation. Keeps every fact and cell name."""
    user = ("Rewrite this tutor reply in your own friendly words. Keep every fact and "
            "every cell name exactly; add nothing new.\n\n"
            + (f"The player asked: {question}\n\n" if question else "")
            + f"Reply to reword:\n{verified_text}")
    return accept(client.complete(SYSTEM_PROMPT, user))


def answer_open(client: Client, question: str, facts: dict) -> Optional[str]:
    """Answer a question the rules engine did not recognise, from facts only."""
    user = ("Facts from the game (the only facts you may use):\n"
            + json.dumps(facts, sort_keys=True)
            + f"\n\nPlayer question: {question}\n\nAnswer helpfully without revealing any "
              "cell's digit.")
    return accept(client.complete(SYSTEM_PROMPT, user))
