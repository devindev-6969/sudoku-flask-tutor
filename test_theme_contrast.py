"""
test_theme_contrast.py
----------------------
Checks WCAG contrast for both themes by reading the colour tokens straight out
of static/style.css, so a palette tweak that hurts readability fails the build.

  text and digits      >= 4.5 : 1
  rings, borders, dots >= 3   : 1   (WCAG non-text contrast)
"""

import pathlib
import re
import unittest

CSS = pathlib.Path(__file__).with_name("static").joinpath("style.css").read_text()


def tokens(theme):
    block = re.search(r':root\[data-theme="%s"\]\s*\{(.*?)\n\}' % theme, CSS, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


CELL_BACKGROUNDS = ["cell", "peer", "same", "select", "region"]
PRINTED_BACKGROUNDS = ["clue-bg", "clue-peer", "same", "select", "region"]

TEXT_PAIRS = (
    [("pen", bg) for bg in CELL_BACKGROUNDS]                 # your digits
    + [("margin", bg) for bg in CELL_BACKGROUNDS]            # hint-revealed digits
    + [("ink", bg) for bg in PRINTED_BACKGROUNDS]            # printed clues
    + [("proof", "wrong-bg"),                                # wrong entry
       ("ink", "paper"), ("ink-soft", "paper"),              # page text and labels
       ("ink", "sheet"), ("ink-soft", "sheet"),              # dialogs, select
       ("ink-soft", "btn"), ("ink-soft", "cell"),            # "left" counts, legend
       ("btn-text", "btn"), ("btn-text", "btn-hover"),
       ("primary-text", "primary"), ("primary-text", "primary-hover"),
       ("accent-text", "accent-bg"), ("accent-text", "accent-hover"),
       ("ink", "region"), ("accent-text", "region"),         # hint note
       ("proof", "paper"), ("margin", "paper"),              # error / ok status
       ("ink-soft", "clue-bg"),
       ("ink", "tutor-bg"), ("ink-soft", "tutor-bg"),        # tutor bubbles, their meta lines
       ("tutor", "tutor-bg"), ("tutor", "sheet"),            # titles, chips and cell refs on the panel
       ("primary-text", "tutor"),                            # unread badge, chosen quiz answer
       ("ink", "wrong-bg"), ("ink-soft", "sheet"),           # error bubble, footnote
       ("primary-text", "primary")]                          # your own chat bubbles, active Play/Learn
)

NON_TEXT_PAIRS = [
    ("frame", "paper"), ("line-strong", "cell"),             # board frame and box lines
    ("focus", "paper"), ("focus", "sheet"),                  # keyboard focus ring
    ("ink-soft", "paper"),                                   # empty mistake boxes
    ("proof", "paper"), ("margin", "paper"),                 # struck box, hint dots
    ("pen", "select"), ("pen", "wrong-bg"),                  # selected-cell ring
    ("proof", "cell"), ("proof", "same"),                    # conflict ring
    ("margin", "region"),                                    # hint target ring
    ("tutor", "sheet"), ("tutor", "tutor-bg"),               # chip outlines, the tutor's bubble stripe
]


class TestThemeContrast(unittest.TestCase):
    def check(self, theme, pairs, minimum):
        t = tokens(theme)
        failures = []
        for fg, bg in pairs:
            ratio = contrast(t[fg], t[bg])
            if ratio < minimum:
                failures.append(f"{theme}: {fg} {t[fg]} on {bg} {t[bg]} = {ratio:.2f}")
        self.assertEqual(failures, [], f"below {minimum}:1")

    def test_text_contrast_light(self):
        self.check("light", TEXT_PAIRS, 4.5)

    def test_text_contrast_dark(self):
        self.check("dark", TEXT_PAIRS, 4.5)

    def test_non_text_contrast_light(self):
        self.check("light", NON_TEXT_PAIRS, 3.0)

    def test_non_text_contrast_dark(self):
        self.check("dark", NON_TEXT_PAIRS, 3.0)

    def test_both_themes_define_the_same_tokens(self):
        self.assertEqual(set(tokens("light")), set(tokens("dark")))

    def test_the_selection_ring_is_visible_against_every_background_it_can_sit_on(self):
        """The selected cell is marked by a ring as well as a fill (hue alone is
        not enough), so the ring must stand out from every neighbouring cell."""
        for theme in ("light", "dark"):
            t = tokens(theme)
            for bg in ("cell", "peer", "same", "region", "clue-bg", "clue-peer", "select"):
                self.assertGreaterEqual(contrast(t["pen"], t[bg]), 3.0, f"{theme}: ring vs {bg}")
            for bg in ("cell", "same", "peer"):                    # target ring in the hint colour
                self.assertGreaterEqual(contrast(t["margin"], t[bg]), 3.0, f"{theme}: hint ring vs {bg}")
            for bg in ("cell", "peer", "same", "region", "clue-bg", "clue-peer", "select"):   # tutor's ring
                self.assertGreaterEqual(contrast(t["tutor"], t[bg]), 3.0, f"{theme}: tutor ring vs {bg}")

    def test_wrong_entries_stand_out_from_normal_cells(self):
        for theme in ("light", "dark"):
            t = tokens(theme)
            self.assertGreater(contrast(t["wrong-bg"], t["cell"]), 1.1, theme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
