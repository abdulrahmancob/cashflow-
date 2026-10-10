"""Control detection and sibling-relative marking (R5, R6, R6b) on synthetic ink."""

from __future__ import annotations

import unittest

from intake_reader.anchors import NEW_CIRCLE, OLD_BULLET, OLD_CHECKBOX
from intake_reader.controls import find_controls, score_controls
from intake_reader.labels import analyse
from intake_reader.page import binarize, mask_lines

from .synth import Spec, draw_block

OLD_LABELS = [
    "Doctor's referral/recommendations",
    "Google",
    "Zocdoc",
    "Social Media",
    "Insurance Recommendations",
    "Direct Mail",
    "Word of Mouth",
    "Marketing Table",
    "Event or community outreach",
    "Clinic staff",
    "From doctor office",
    "From street distribution",
    "Other (please specify)",
]
NEW_LEFT = ["Doctor referral", "Zocdoc", "Insurance", "Event / Outreach", "Other:"]
NEW_RIGHT = ["Google", "Social Media", "Word of Mouth"]


def run_block(block, family):
    layout = analyse(block.words, family, block.text_h)
    raw = binarize(block.gray, 165)
    ink = mask_lines(raw, layout.text_h)
    locate = mask_lines(binarize(block.gray, 200), layout.text_h)
    controls = find_controls(ink, layout.hear, layout.text_h, family, locate, block.words, raw=raw)
    score_controls(controls, family)
    return layout, controls


def marked_codes(controls):
    return sorted(c.code for c in controls if c.marked)


class OldCheckboxTests(unittest.TestCase):
    def _specs(self, marks: dict[str, str]):
        return [Spec(label, marks.get(label, "")) for label in OLD_LABELS]

    def test_all_labels_and_controls_found(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({}), "box")
        layout, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(len(layout.hear), 13, [h.code for h in layout.hear])
        self.assertEqual(len(controls), 13)
        self.assertTrue(all(c.found for c in controls), [(c.code, c.found) for c in controls])
        self.assertEqual(marked_codes(controls), [])

    def test_thick_check(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({"Google": "check"}), "box")
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["google"])

    def test_thin_check_one_pixel(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({"Zocdoc": "thin"}), "box")
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["zocdoc"])

    def test_x_mark_and_spill(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({"Insurance Recommendations": "x", "Direct Mail": "spill"}), "box")
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["direct_mail", "insurance"])

    def test_faded_print_dark_pen(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({"Word of Mouth": "check"}), "box", print_gray=175)
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["friend_family"])

    def test_binder_line_and_highlighter(self):
        block = draw_block(
            "How did you hear about us? (*) Please check what applies",
            self._specs({"Marketing Table": "check"}),
            "box",
            binder_line=True,
            highlighter=True,
        )
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["marketing_table"])

    def test_skew(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({"Clinic staff": "check"}), "box", skew_deg=2.5)
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["clinic_staff"])

    def test_two_marks(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({"Google": "check", "Zocdoc": "x"}), "box")
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["google", "zocdoc"])

    def test_scribble(self):
        block = draw_block("How did you hear about us? (*) Please check what applies", self._specs({"Event or community outreach": "scribble"}), "box")
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["event"])


class NewCircleTests(unittest.TestCase):
    def _specs(self, marks: dict[str, str]):
        return [Spec(label, marks.get(label, ""), column=0) for label in NEW_LEFT] + [Spec(label, marks.get(label, ""), column=1) for label in NEW_RIGHT]

    def test_two_columns_unmarked(self):
        block = draw_block("How did you hear about us?", self._specs({}), "circle")
        layout, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(sorted(h.code for h in layout.hear), sorted(["doctor", "zocdoc", "insurance", "event", "other", "google", "social_media", "friend_family"]))
        self.assertEqual(marked_codes(controls), [])

    def test_check_through_circle_right_column(self):
        block = draw_block("How did you hear about us?", self._specs({"Google": "check"}), "circle")
        _, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["google"])

    def test_slash_left_column(self):
        block = draw_block("How did you hear about us?", self._specs({"Insurance": "slash"}), "circle")
        _, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["insurance"])

    def test_filled_dot(self):
        block = draw_block("How did you hear about us?", self._specs({"Word of Mouth": "dot"}), "circle")
        _, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["friend_family"])

    def test_circled_label(self):
        block = draw_block("How did you hear about us?", self._specs({"Zocdoc": "circle_label"}), "circle")
        _, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["zocdoc"])
        self.assertIn([c.reason for c in controls if c.marked][0], ("circled", "fill"))

    def test_other_marked_not_event(self):
        """A marked Other circle must not be read as the Event line above it (root cause 3)."""
        block = draw_block("How did you hear about us?", self._specs({"Other:": "check"}), "circle")
        _, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["other"])


class BulletTests(unittest.TestCase):
    LABELS = [
        "Doctor's referral/recommendations",
        "Google",
        "Zocdoc",
        "Social Media",
        "Insurance recommendations",
        "Direct mail",
        "Word of Mouth",
        "Event or community outreach (flyers)",
        "Other (please specify)",
    ]

    def test_filled_bullet(self):
        specs = [Spec(label, "dot" if label.startswith("Event") else "") for label in self.LABELS]
        block = draw_block("How did you hear about us? Please check what applies", specs, "bullet")
        _, controls = run_block(block, OLD_BULLET)
        self.assertEqual(marked_codes(controls), ["event"])

    def test_hollow_bullets_unmarked(self):
        specs = [Spec(label) for label in self.LABELS]
        block = draw_block("How did you hear about us? Please check what applies", specs, "bullet")
        _, controls = run_block(block, OLD_BULLET)
        self.assertEqual(marked_codes(controls), [])


if __name__ == "__main__":
    unittest.main()
