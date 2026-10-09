"""Rules added after the second golden eval: boxes glued to labels and letters mistaken for boxes
(column vote), indented sub-options, labels whose first word OCR lost, the geometric "Other:" line,
write-in false positives from printed rules and helper text, fuzzy keyword mapping."""

from __future__ import annotations

import unittest

import numpy as np

from intake_reader.anchors import NEW_CIRCLE, OLD_CHECKBOX, TINY
from intake_reader.controls import find_controls, score_controls
from intake_reader.labels import LabelHit, analyse, similarity
from intake_reader.page import binarize, mask_lines
from intake_reader.writein import _printed_boxes, map_text, writein_area

from .synth import Spec, draw_block
from .test_controls import OLD_LABELS, marked_codes, run_block
from .test_labels import words_from_text
from .test_page_and_writein import ink_detect_without_ocr


def _old_specs(marks: dict[str, str], **extra) -> list[Spec]:
    specs = []
    for label in OLD_LABELS:
        kw = dict(extra.get(label, {}))
        specs.append(Spec(label, marks.get(label, ""), **kw))
    return specs


class GluedBoxTests(unittest.TestCase):
    """Spanish and some English checkbox forms print the box touching the first letter."""

    def test_glued_boxes_found_at_their_column(self):
        block = draw_block("¿Cómo nos conoció? Marque lo que corresponda", _old_specs({"Google": "thin"}), "box", gap=0)
        layout, controls = run_block(block, OLD_CHECKBOX)
        xs = [c.x0 for c in controls]
        self.assertLessEqual(max(xs) - min(xs), 6, xs)
        self.assertEqual(marked_codes(controls), ["google"])

    def test_glued_boxes_check_spilling_over(self):
        block = draw_block("How did you hear about us? Please check what applies", _old_specs({"Social Media": "check", "Direct Mail": "x"}), "box", gap=1)
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["direct_mail", "social_media"])

    def test_letters_never_become_the_column(self):
        """Capital letters are box-like too; the column of boxes is the one further left."""
        block = draw_block("How did you hear about us?", _old_specs({}), "box", gap=0)
        _, controls = run_block(block, OLD_CHECKBOX)
        for control, (spec, box) in zip(controls, block.rows):
            self.assertLessEqual(abs(control.x0 - box[0]), 4, (control.code, control.x0, box))


class IndentedSubOptionTests(unittest.TestCase):
    def test_flyer_rows_keep_their_indented_box(self):
        extra = {"From doctor office": {"indent": 36}, "From street distribution": {"indent": 36}}
        block = draw_block("How did you hear about us?", _old_specs({"From doctor office": "check"}, **extra), "box")
        _, controls = run_block(block, OLD_CHECKBOX)
        flyer = next(c for c in controls if c.code == "flyer_doctor_office")
        main = next(c for c in controls if c.code == "google")
        self.assertGreater(flyer.x0, main.x0 + 20)
        self.assertEqual(marked_codes(controls), ["flyer_doctor_office"])

    def test_indented_row_unmarked_stays_quiet(self):
        extra = {"From doctor office": {"indent": 36}, "From street distribution": {"indent": 36}}
        block = draw_block("How did you hear about us?", _old_specs({"Zocdoc": "check"}, **extra), "box")
        _, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["zocdoc"])


class LostFirstWordTests(unittest.TestCase):
    def test_control_found_when_first_label_word_is_missing(self):
        """A check through the box often eats "Doctor" from "Doctor's referral/recommendations"."""
        block = draw_block("How did you hear about us?", _old_specs({"Doctor's referral/recommendations": "x"}), "box")
        words = [w for w in block.words if w["text"] != "Doctor's"]
        layout = analyse(words, OLD_CHECKBOX, block.text_h)
        doctor = next(h for h in layout.hear if h.code == "doctor")
        self.assertNotIn("Doctor's", [w["text"] for w in doctor.words])
        ink = mask_lines(binarize(block.gray, 165), layout.text_h)
        locate = mask_lines(binarize(block.gray, 200), layout.text_h)
        controls = find_controls(ink, layout.hear, layout.text_h, OLD_CHECKBOX, locate, words)
        score_controls(controls, OLD_CHECKBOX)
        control = next(c for c in controls if c.code == "doctor")
        google = next(c for c in controls if c.code == "google")
        self.assertLessEqual(abs(control.x0 - google.x0), 4)
        self.assertEqual(marked_codes(controls), ["doctor"])


class OtherLineTests(unittest.TestCase):
    NEW = [
        "How did you hear about us?",
        "O Doctor referral|O Google",
        "O Zocdoc|O Social Media",
        "O Insurance|O Word of Mouth",
        "O Event / Outreach",
    ]

    def test_the_is_not_other(self):
        self.assertLess(similarity("the", "other"), 0.75)
        self.assertLess(similarity("er", "other"), 0.75)
        self.assertGreaterEqual(similarity("ther", "other"), 0.75)

    def test_other_placed_under_event_when_ocr_lost_it(self):
        words = words_from_text(self.NEW)
        layout = analyse(words, NEW_CIRCLE, 20)
        other = next(h for h in layout.hear if h.code == "other")
        event = next(h for h in layout.hear if h.code == "event")
        self.assertTrue(other.inferred)
        self.assertEqual(other.anchor_x, event.anchor_x)
        self.assertAlmostEqual(other.cy, event.cy + 34, delta=6)
        self.assertLessEqual(other.x1 - other.x0, 20 * 3.6)

    def test_handwriting_read_as_label_is_replaced(self):
        words = words_from_text(self.NEW + ["O the walking on the block"])
        layout = analyse(words, NEW_CIRCLE, 20)
        other = next(h for h in layout.hear if h.code == "other")
        self.assertTrue(other.inferred)
        self.assertLessEqual(other.x1 - other.x0, 20 * 3.6)
        event = next(h for h in layout.hear if h.code == "event")
        self.assertEqual(other.anchor_x, event.anchor_x)
        self.assertEqual(other.line_index, 5)

    def test_clean_other_label_kept(self):
        words = words_from_text(self.NEW + ["O Other:"])
        layout = analyse(words, NEW_CIRCLE, 20)
        other = next(h for h in layout.hear if h.code == "other")
        self.assertFalse(other.inferred)

    def test_tiny_other_on_next_row(self):
        words = words_from_text(
            [
                "How did you hear about us? O Doctor O Google O Social Media O Lives nearby",
                "Other: Spouse",
                "Insurance Information - Please provide insurance card(s)",
            ]
        )
        layout = analyse(words, TINY, 14)
        other = next(h for h in layout.hear if h.code == "other")
        self.assertEqual(other.line_index, 1)
        self.assertEqual(other.words[0]["text"], "Other:")

    def test_tiny_the_inside_row_is_dropped(self):
        words = words_from_text(
            [
                "How did you hear about us? O Doctor O Google O Social Media O Lives nearby",
                "the Spouse",
                "Insurance Information - Please provide insurance card(s)",
            ]
        )
        layout = analyse(words, TINY, 14)
        other = next((h for h in layout.hear if h.code == "other"), None)
        # "the" is not the label; the first word of the next row is taken as the Other line
        self.assertIsNotNone(other)
        self.assertEqual(other.words[0]["text"], "the")
        self.assertTrue(other.inferred)

    def test_tiny_other_has_no_control(self):
        words = words_from_text(
            [
                "How did you hear about us? O Doctor O Google O Social Media O Lives nearby",
                "Other:",
            ]
        )
        layout = analyse(words, TINY, 14)
        ink = np.zeros((120, 1200), dtype=bool)
        controls = find_controls(ink, layout.hear, 14, TINY, ink, words)
        self.assertNotIn("other", [c.code for c in controls])
        self.assertEqual(len(controls), 4)

    def test_inferred_label_needs_real_text(self):
        words = words_from_text(
            [
                "How did you hear about us? (*) Please check what applies",
                "O Doctor's referral/recommendations",
                "O Google",
                "O Zocdoc",
                "O Social Media",
                "O Insurance Recommendations",
                "O Direct Mail",
                "O Word of Mouth",
                "O Marketing Table",
                "O Event or community outreach",
                "O Clinic staff",
                "O From doctor office",
                "O From street distribution",
                "FP]",
            ]
        )
        layout = analyse(words, OLD_CHECKBOX, 20)
        self.assertNotIn("other", [h.code for h in layout.hear])


class WriteInFalsePositiveTests(unittest.TestCase):
    LABELS = ["Doctor's referral/recommendations", "Google", "Word of Mouth", "Clinic staff", "Other (please specify)"]

    def _block(self, specs, **kw):
        block = draw_block("How did you hear about us? (*) Please check what applies", specs, "box", **kw)
        layout = analyse(block.words, OLD_CHECKBOX, block.text_h)
        ink = mask_lines(binarize(block.gray), layout.text_h)
        return block, layout, ink

    def test_skewed_underline_is_not_handwriting(self):
        specs = [Spec(label, "", rule="solid" if label in ("Clinic staff", "Word of Mouth") else "") for label in self.LABELS]
        block, layout, ink = self._block(specs, skew_deg=1.2)
        for code in ("clinic_staff", "friend_family"):
            hit = next(h for h in layout.hear if h.code == code)
            self.assertIsNone(ink_detect_without_ocr(ink, block.gray, block.words, hit, "inline", layout.text_h), code)

    def test_dashed_page_border_is_not_handwriting(self):
        specs = [Spec(label, "", rule="dotted" if label.startswith("Other") else "") for label in self.LABELS]
        block, layout, ink = self._block(specs, border_dashes=True)
        hit = next(h for h in layout.hear if h.code == "other")
        self.assertIsNone(ink_detect_without_ocr(ink, block.gray, block.words, hit, "inline", layout.text_h))

    def test_handwriting_next_to_dotted_line_still_detected(self):
        specs = [Spec(label, "", "my sister" if label.startswith("Other") else "", rule="dotted" if label.startswith("Other") else "") for label in self.LABELS]
        block, layout, ink = self._block(specs, border_dashes=True)
        hit = next(h for h in layout.hear if h.code == "other")
        self.assertIsNotNone(ink_detect_without_ocr(ink, block.gray, block.words, hit, "inline", layout.text_h))

    def test_label_remainder_counts_as_printed(self):
        """When OCR attached only "Clinic" to the label, "staff" after it is still print."""
        specs = [Spec(label) for label in self.LABELS]
        block, layout, ink = self._block(specs)
        hit = next(h for h in layout.hear if h.code == "clinic_staff")
        short = LabelHit(hit.code, hit.option, hit.words[:1], hit.line_index, "hear", 0.8, 0, hit.right_limit)
        short.line_cy, short.line_h = hit.line_cy, hit.line_h
        self.assertIsNone(ink_detect_without_ocr(ink, block.gray, block.words, short, "inline", layout.text_h))

    def test_garbled_helper_text_is_masked(self):
        words = [
            {"text": "\\!ypedoctor's", "x": 100, "y": 50, "w": 120, "h": 16, "conf": 45.0, "line": (1, 1, 1)},
            {"text": "namefoffice)", "x": 230, "y": 50, "w": 110, "h": 16, "conf": 45.0, "line": (1, 1, 1)},
        ]
        label_words = [{"text": "Doctor's", "x": 100, "y": 20, "w": 90, "h": 20, "conf": 90.0, "line": (1, 1, 0)}]
        hit = LabelHit("doctor", OLD_CHECKBOX.options[0], label_words, 0, "hear", 0.9, 0, 400)
        boxes = _printed_boxes(words, (90, 42, 420, 100), hit, "below", 20)
        self.assertEqual(len(boxes), 2)

    def test_handwritten_friend_is_not_masked_as_print(self):
        words = [{"text": "friend", "x": 300, "y": 50, "w": 70, "h": 20, "conf": 40.0, "line": (1, 1, 1)}]
        label_words = [{"text": "Other:", "x": 200, "y": 48, "w": 60, "h": 20, "conf": 90.0, "line": (1, 1, 1)}]
        hit = LabelHit("other", NEW_CIRCLE.options[-1], label_words, 1, "hear", 0.9, 0, 900)
        hit.line_cy, hit.line_h = 58, 20
        self.assertEqual(_printed_boxes(words, (270, 40, 900, 80), hit, "inline", 20), [])

    def test_below_area_stops_at_next_label(self):
        label_words = [{"text": "Doctor's", "x": 100, "y": 20, "w": 90, "h": 20, "conf": 90.0, "line": (1, 1, 0)}]
        hit = LabelHit("doctor", OLD_CHECKBOX.options[0], label_words, 0, "hear", 0.9, 0, 400)
        _x0, y0, _x1, y1 = writein_area(hit, "below", 20, 900, 400, bounds=(None, 70))
        self.assertGreaterEqual(y0, 40)
        self.assertLessEqual(y1, 70)

    def test_inline_area_width_capped(self):
        label_words = [{"text": "Other:", "x": 200, "y": 48, "w": 60, "h": 20, "conf": 90.0, "line": (1, 1, 1)}]
        hit = LabelHit("other", NEW_CIRCLE.options[-1], label_words, 1, "hear", 0.9, 0, 2000)
        hit.line_cy, hit.line_h = 58, 20
        x0, _y0, x1, _y1 = writein_area(hit, "inline", 20, 2000, 400)
        self.assertLessEqual(x1 - x0, 20 * 20)


class FuzzyKeywordTests(unittest.TestCase):
    def test_one_ocr_error_still_maps(self):
        self.assertEqual(map_text("_Eciend fold me"), "friend_family")
        self.assertEqual(map_text("my hussband"), "friend_family")
        self.assertEqual(map_text("Gooogle"), "google")
        self.assertEqual(map_text("from list of aproved providers"), "insurance")

    def test_printed_words_and_short_tokens_do_not_map(self):
        self.assertIsNone(map_text("Other"))
        self.assertIsNone(map_text("please specify"))
        self.assertIsNone(map_text("AN"))
        self.assertIsNone(map_text("eee eee"))

    def test_exact_keywords_unchanged(self):
        self.assertEqual(map_text("walked passed"), "walk_in")
        self.assertEqual(map_text("Someone sent me your flyer"), "event")
        self.assertEqual(map_text("ENT"), "doctor")


if __name__ == "__main__":
    unittest.main()
