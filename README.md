# Sudoku

A Sudoku game for the browser, built with Python and Flask. It has a Play mode
and a Learn mode with a small tutor chat.

## Requirements

Python 3.10 or newer.

## Running it

Open a terminal in the project folder (the one with `app.py`), then:

```
pip install flask
python app.py
```

Now go to http://127.0.0.1:5000 in your browser. Press Ctrl+C in the terminal
to stop it.

If `python` doesn't work, try `python3`. If port 5000 is taken, start it on
another one, for example `PORT=8000 python app.py` (on Windows PowerShell:
`$env:PORT=8000; python app.py`) and open that port instead.

## Playing

- Pick Easy, Medium or Hard.
- Click a cell and type 1-9, or use the number pad. Backspace clears it.
- Hint gives a nudge first, then an explanation, then the digit. Each step uses
  one hint, and each puzzle has a fixed number of hints.
- Pause stops the clock and hides the board until you resume.
- A mistake is a digit that doesn't match the solution. Each difficulty has a
  limit, and reaching it ends the game.
- The score drops with mistakes, hints and time over the par time.

## Learn mode

Use the Play / Learn switch in the header. It is the same game either way, so
switching never changes your board, clock, mistakes or hints. Learn mode adds a
chat called Sudoku Tutor next to the board.

What the tutor does:

- Reacts to your moves. When you make a mistake it names the cell and the digit
  and points out the cells it clashes with, highlighting them on the board.
- Stays quiet on good moves, except when there is something worth learning.
- Answers questions, either typed or from the buttons: "Explain this cell",
  "Why can't I put 5 here?", "What am I missing in this row?" and similar.
- Teaches four techniques: scanning, naked singles, hidden singles and naked
  pairs. Each lesson ends with a short question.
- Offers a nudge if you've been stuck for two minutes.

The tutor never fills in a cell for you. Explaining rules and general theory is
free. Anything that tells you more about an empty cell, such as "help me find my
next move", uses one of your hints, just like the Hint button.

## Tutor AI (optional)

Everything works without this. If you want an AI model to reword the tutor's
answers, add a Gemini key:

1. Copy `.env.example` to `.env` in the same folder as `app.py`.
2. Put your key after `GEMINI_API_KEY=` in `.env`.
3. Restart the app. The first line it prints should read `Tutor AI: gemini`.

## Tests

```
python -m unittest test_sudoku test_hints test_game test_api test_theme_contrast test_tutor -v
```