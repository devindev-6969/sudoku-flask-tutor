"""
lessons.py
----------
Short, fixed lessons for the Sudoku tutor. They are written by hand, so they
are always correct and need no AI. Each has a worked example and a one-question
check. Lessons talk about *general technique*; they never look at the board,
so they cannot give anything away.
"""

from __future__ import annotations

from typing import Dict, List, Optional

LESSONS: Dict[str, dict] = {
    "scanning": {
        "title": "Scanning",
        "intro": "Scanning means taking one digit at a time and finding where it "
                 "still fits in each box.",
        "steps": [
            "Pick a digit that already appears many times on the board.",
            "Mentally cross out every row and column that already holds it.",
            "In a box that lacks the digit, look for the one cell left uncrossed. "
            "That is where it goes.",
            "Repeat with the next digit. Every placement makes the next scan easier.",
        ],
        "example": "Example: 7s sit in rows 1 and 5 and in columns 3 and 7. Take a "
                   "box with no 7, cross out its cells in those rows and columns, "
                   "and if one empty cell remains, it must be the 7.",
        "quiz": {
            "question": "You scan for a 4 in a box covering rows 1 to 3 and columns "
                        "7 to 9. Rows 2 and 3 already have a 4, and so do columns "
                        "7 and 8. Where can the box's 4 go?",
            "options": ["R1C7", "R1C9", "R2C8"],
            "answer": 1,
            "explain": "Rows 2 and 3 rule out the lower two rows of the box, and "
                       "columns 7 and 8 rule out two columns. Only R1C9 is left.",
        },
        "note": None,
    },
    "naked_single": {
        "title": "Naked singles",
        "intro": "A naked single is a cell where only one digit can still fit.",
        "steps": [
            "Pick an empty cell.",
            "List the digits already in its row, column and box.",
            "If eight different digits appear across those three lists, the ninth "
            "is forced. Enter it.",
        ],
        "example": "Example: a cell's row holds 1, 2, 3, its column holds 4, 5, 6 "
                   "and its box holds 7, 8. Together that is 1 to 8, so the cell "
                   "must be 9.",
        "quiz": {
            "question": "A cell's row holds 2, 5, 9, its column holds 1, 3, 8 and "
                        "its box holds 4, 6. Which digit goes there?",
            "options": ["7", "4", "9"],
            "answer": 0,
            "explain": "1, 2, 3, 4, 5, 6, 8 and 9 are all blocked. Only 7 is left.",
        },
        "note": None,
    },
    "hidden_single": {
        "title": "Hidden singles",
        "intro": "A hidden single is a digit that has only one possible cell left in "
                 "a row, column or box, even if that cell could hold other digits too.",
        "steps": [
            "Choose a row, column or box with a missing digit, say 5.",
            "For each empty cell in it, ask: could a 5 go here? Cross out cells "
            "whose row, column or box already holds a 5.",
            "If only one cell survives, the 5 goes there.",
        ],
        "example": "Example: Box 5 is missing a 4. Rows 4 and 6 already have a 4 "
                   "outside the box, and so do columns 5 and 6. Only R5C4 is left "
                   "in the box, so R5C4 is 4.",
        "quiz": {
            "question": "Row 7 is missing a 3. Its empty cells are R7C2, R7C5 and "
                        "R7C8. Columns 2 and 5 already contain a 3. Where does the "
                        "3 go?",
            "options": ["R7C2", "R7C5", "R7C8"],
            "answer": 2,
            "explain": "Columns 2 and 5 rule out R7C2 and R7C5. R7C8 is the only "
                       "cell in the row that can take the 3.",
        },
        "note": None,
    },
    "naked_pair": {
        "title": "Naked pairs",
        "intro": "A naked pair is two cells in the same row, column or box that can "
                 "each only be the same two digits. Those two digits cannot go "
                 "anywhere else in that unit.",
        "steps": [
            "Write down the candidates of the empty cells in a row, column or box.",
            "Find two cells that both have exactly the same two candidates, say 3 "
            "and 8.",
            "Remove 3 and 8 from every other cell in that unit.",
            "Check whether that leaves any new single.",
        ],
        "example": "Example: in Row 5, R5C2 and R5C7 can only be 3 or 8. One takes "
                   "the 3 and the other the 8, so no other cell in Row 5 can be 3 "
                   "or 8.",
        "quiz": {
            "question": "R2C1 and R2C4 can only be 5 or 9. What does that tell you "
                        "about R2C6?",
            "options": ["It can't be 5 or 9", "It must be 5", "It must be 9"],
            "answer": 0,
            "explain": "The pair will use up both the 5 and the 9 in Row 2, so no "
                       "other cell in that row can be either.",
        },
        "note": "I can teach this technique, but I can't spot pairs on your board yet. "
                "My board analysis finds singles only.",
    },
}

ORDER: List[str] = ["scanning", "naked_single", "hidden_single", "naked_pair"]

# Which lesson goes with which hint technique.
FOR_TECHNIQUE: Dict[str, str] = {
    "naked_single": "naked_single",
    "hidden_single": "hidden_single",
}

# Words a player might type, mapped to lessons.
ALIASES: Dict[str, str] = {
    "scan": "scanning", "scanning": "scanning", "cross-hatch": "scanning",
    "crosshatch": "scanning", "cross hatch": "scanning", "cross-hatching": "scanning",
    "naked single": "naked_single", "naked singles": "naked_single",
    "hidden single": "hidden_single", "hidden singles": "hidden_single",
    "naked pair": "naked_pair", "naked pairs": "naked_pair", "pair": "naked_pair",
    "pairs": "naked_pair",
}


def find_lesson(text: str) -> Optional[str]:
    """Return the lesson a sentence asks about, preferring the longest phrase."""
    low = text.lower()
    for phrase in sorted(ALIASES, key=len, reverse=True):
        if phrase in low:
            return ALIASES[phrase]
    return None
