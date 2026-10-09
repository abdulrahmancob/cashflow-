"""Rescue rules for damaged OCR: glued glyphs, truncated labels, row-order inference, column
realignment and the two-sided ring test (R4, R5, R6b)."""

from __future__ import annotations

import unittest

import numpy as np
from PIL import Image, ImageDraw

from intake_reader.anchors import NEW_CIRCLE, OLD_CHECKBOX
from intake_reader.controls import Control, _align_columns, _ring, find_controls, score_controls
from intake_reader.labels import LabelHit, _glyph_prefix, analyse, similarity

from .synth import Spec, draw_block, font
from .test_labels import words_from_text


class GluedGlyphTests(unittest.TestCase):
    def test_prefix_detected_for_check_glued_into_first_letter(self):
        word = {"text": "Qcoctor", "x": 222, "y": 10, "w": 140, "h": 20}
        self.assertGreater(_glyph_prefix(word, "doctorreferral"), 10)

    def test_no_prefix_for_clean_or_merely_misread_word(self):
        self.assertEqual(_glyph_prefix({"text": "Doctor", "x": 0, "y": 0, "w": 100, "h": 20}, "doctorreferral"), 0)
        self.assertEqual(_glyph_prefix({"text": "Dodtor", "x": 0, "y": 0, "w": 100, "h": 20}, "doctorreferral"), 0)

    def test_leading_symbols_count(self):
        self.assertGreater(_glyph_prefix({"text": "@oogle", "x": 0, "y": 0, "w": 90, "h": 20}, "google"), 0)

    def test_glued_label_still_matched_and_anchored(self):
        words = words_from_text(["How did you hear about us?", "Qcoctor referral|O Google", "O Zocdoc|O Social Media", "O Insurance|O Word of Mouth", "O Event / Outreach", "O Other:"])
        layout = analyse(words, NEW_CIRCLE, 20)
        doctor = next(h for h in layout.hear if h.code == "doctor")
        self.assertGreater(doctor.prefix_px, 0)
        self.assertGreater(doctor.anchor_x, doctor.x0)


class TruncatedLabelTests(unittest.TestCase):
    def test_prefix_of_key_matches(self):
        self.assertGreaterEqual(similarity("goog", "google"), 0.75)
        self.assertGreaterEqual(similarity("zocd", "zocdoc"), 0.75)
        self.assertLess(similarity("goo", "google"), 0.75)

    def test_row_order_inference_fills_two_gaps(self):
        """Binder line ate the first letters: "oogle" and "ocdoc" are recovered by their rows."""
        words = words_from_text(
            [
                "How did you hear about us? (*) Please check what applies",
                "O Doctor's referral/recommendations",
                "O xxxxx",
                "O yyyyy",
                "O Social Media",
                "O Insurance Recommendations",
                "O Direct Mail",
                "O Word of Mouth",
                "O Marketing Table",
                "O Event or community outreach",
                "O Clinic staff",
                "O From doctor office",
                "O From street distribution",
                "O Other(please specify)",
            ]
        )
        layout = analyse(words, OLD_CHECKBOX, 20)
        codes = [h.code for h in layout.hear]
        self.assertIn("google", codes)
        self.assertIn("zocdoc", codes)
        google = next(h for h in layout.hear if h.code == "google")
        self.assertTrue(google.inferred)
        self.assertEqual(google.line_index, 2)

    def test_no_inference_when_candidates_do_not_match_gap(self):
        words = words_from_text(
            [
                "How did you hear about us? (*) Please check what applies",
                "O Doctor's referral/recommendations",
                "O Social Media",
                "O Insurance Recommendations",
                "O Direct Mail",
                "O Word of Mouth",
                "O Marketing Table",
                "O Event or community outreach",
                "O Clinic staff",
                "O From doctor office",
                "O From street distribution",
                "O Other(please specify)",
            ]
        )
        layout = analyse(words, OLD_CHECKBOX, 20)
        codes = [h.code for h in layout.hear]
        self.assertNotIn("google", codes)
        self.assertNotIn("zocdoc", codes)


class RealignTests(unittest.TestCase):
    def test_stray_control_moves_to_column(self):
        ink = np.zeros((300, 400), dtype=bool)
        controls = []
        for i, y in enumerate((20, 70, 120, 170)):
            x0 = 40 if i != 2 else 120  # the third strayed onto a letter
            c = Control("c%d" % i, x0, y, x0 + 24, y + 24, True)
            hit = LabelHit("c%d" % i, OLD_CHECKBOX.options[0], [{"text": "Label", "x": 160, "y": y, "w": 60, "h": 24}], i, "hear", 1.0, 0, 400)
            c.extra["hit"] = hit
            controls.append(c)
        _align_columns(ink, controls, 20)
        self.assertEqual(controls[2].x0, 40)
        self.assertEqual(controls[2].reason, "realigned")
        self.assertEqual(controls[0].reason, "")

    def test_two_columns_stay_separate(self):
        ink = np.zeros((300, 900), dtype=bool)
        controls = []
        for i, (x0, y) in enumerate(((40, 20), (40, 70), (40, 120), (500, 20), (500, 70), (500, 120))):
            c = Control("c%d" % i, x0, y, x0 + 24, y + 24, True)
            c.extra["hit"] = LabelHit("c%d" % i, OLD_CHECKBOX.options[0], [{"text": "Label", "x": x0 + 40, "y": y, "w": 60, "h": 24}], i, "hear", 1.0, 0, 900)
            controls.append(c)
        _align_columns(ink, controls, 20)
        self.assertTrue(all(c.reason == "" for c in controls))


class RingTests(unittest.TestCase):
    def _hit(self, x0, y0, x1, y1):
        return LabelHit("google", NEW_CIRCLE.options[1], [{"text": "Google", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}], 0, "hear", 1.0, 0, 600)

    def test_circle_around_label_scores_but_binder_line_does_not(self):
        image = Image.new("L", (400, 120), 255)
        draw = ImageDraw.Draw(image)
        draw.text((100, 40), "Google", fill=0, font=font(24))
        hit = self._hit(100, 40, 180, 64)
        plain = _ring(np.asarray(image) < 150, hit, 24)
        draw.ellipse((88, 28, 192, 78), outline=0, width=2)
        circled = _ring(np.asarray(image) < 150, hit, 24)
        self.assertGreater(circled, plain + 0.1)
        line = Image.new("L", (400, 120), 255)
        d2 = ImageDraw.Draw(line)
        d2.text((100, 40), "Google", fill=0, font=font(24))
        d2.line((92, 0, 92, 119), fill=0, width=4)
        self.assertLess(_ring(np.asarray(line) < 150, hit, 24), 0.08)


if __name__ == "__main__":
    unittest.main()
