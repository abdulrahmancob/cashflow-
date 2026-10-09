"""Line masks, known-word ratio, rotation helpers and write-in ink detection (R1, R6, R7)."""

from __future__ import annotations

import unittest

import numpy as np
from PIL import Image, ImageDraw

from intake_reader.anchors import NEW_CIRCLE, OLD_CHECKBOX
from intake_reader.labels import analyse
from intake_reader.page import binarize, known_word_ratio, mask_lines, rotate
from intake_reader.writein import _printed_boxes, writein_area

from .synth import Spec, draw_block, font


def ink_detect_without_ocr(ink, gray, words, hit, kind, th):
    """Run the geometric part of writein.detect (no tesseract)."""
    from intake_reader import writein as module

    original = module.ocr_strip
    module.ocr_strip = lambda *_a, **_k: ("stub", 50.0)
    try:
        return module.detect(ink, gray, words, hit, kind, th, "eng")
    finally:
        module.ocr_strip = original


class MaskTests(unittest.TestCase):
    def test_solid_rule_and_binder_line_removed_boxes_kept(self):
        image = Image.new("L", (600, 200), 255)
        draw = ImageDraw.Draw(image)
        draw.rectangle((50, 50, 76, 76), outline=0, width=2)
        draw.line((100, 120, 500, 120), fill=0, width=2)  # rule
        draw.line((20, 0, 20, 199), fill=0, width=3)  # binder
        ink = binarize(np.asarray(image))
        out = mask_lines(ink, 20)
        self.assertFalse(out[120, 300])
        self.assertFalse(out[100, 20])
        self.assertTrue(out[50, 60])

    def test_dotted_rule_removed(self):
        image = Image.new("L", (600, 100), 255)
        draw = ImageDraw.Draw(image)
        for x in range(100, 460, 6):
            draw.line((x, 60, x + 3, 60), fill=0, width=1)
        draw.text((110, 20), "Hi", fill=0, font=font(22))
        ink = binarize(np.asarray(image))
        out = mask_lines(ink, 20)
        self.assertFalse(out[60, 100:460].any())
        self.assertTrue(out[15:45, 100:160].any())

    def test_highlighter_dropped_by_threshold(self):
        gray = np.full((50, 50), 255, dtype=np.uint8)
        gray[10:30, :] = 205  # yellow marker luminance
        gray[20, 10:40] = 20  # pen
        ink = binarize(gray)
        self.assertEqual(int(ink.sum()), 30)


class RotationTests(unittest.TestCase):
    def test_rotate_round_trip(self):
        gray = np.arange(12, dtype=np.uint8).reshape(3, 4)
        self.assertTrue(np.array_equal(rotate(rotate(gray, 180), 180), gray))
        self.assertTrue(np.array_equal(rotate(rotate(gray, 90), 270), gray))
        self.assertEqual(rotate(gray, 90).shape, (4, 3))

    def test_known_word_ratio(self):
        good = "How did you hear about us Doctor referral Google Social Media Word of Mouth Insurance Patient Name Date of Birth Address Phone"
        bad = "yoeauno uan o ayoads asea d y joyio o anbiyisadsa somo yeso usn o qwerty zxcv"
        self.assertGreater(known_word_ratio(good), 0.5)
        self.assertLess(known_word_ratio(bad), 0.1)


class WriteInTests(unittest.TestCase):
    LABELS = ["Doctor's referral/recommendations", "Google", "Word of Mouth", "Other (please specify)"]

    def _block(self, writein_on: str, text: str, dotted: bool = False):
        specs = [Spec(label, "", text if label == writein_on else "") for label in self.LABELS]
        block = draw_block("How did you hear about us? (*) Please check what applies", specs, "box", dotted_writein=dotted)
        layout = analyse(block.words, OLD_CHECKBOX, block.text_h)
        ink = mask_lines(binarize(block.gray), layout.text_h)
        return block, layout, ink

    def test_other_handwriting_detected(self):
        block, layout, ink = self._block("Other (please specify)", "my sister")
        hit = next(h for h in layout.hear if h.code == "other")
        found = ink_detect_without_ocr(ink, block.gray, block.words, hit, "inline", layout.text_h)
        self.assertIsNotNone(found)
        self.assertGreater(found.ink, 50)

    def test_other_blank_not_detected(self):
        block, layout, ink = self._block("Other (please specify)", "")
        hit = next(h for h in layout.hear if h.code == "other")
        self.assertIsNone(ink_detect_without_ocr(ink, block.gray, block.words, hit, "inline", layout.text_h))

    def test_dotted_line_alone_not_detected(self):
        block, layout, ink = self._block("Other (please specify)", "", dotted=True)
        hit = next(h for h in layout.hear if h.code == "other")
        self.assertIsNone(ink_detect_without_ocr(ink, block.gray, block.words, hit, "inline", layout.text_h))

    def test_word_of_mouth_inline_text(self):
        block, layout, ink = self._block("Word of Mouth", "Friend told me")
        hit = next(h for h in layout.hear if h.code == "friend_family")
        self.assertIsNotNone(ink_detect_without_ocr(ink, block.gray, block.words, hit, "inline", layout.text_h))

    def test_writein_area_below_label(self):
        block, layout, ink = self._block("Other (please specify)", "")
        hit = next(h for h in layout.hear if h.code == "doctor")
        x0, y0, x1, y1 = writein_area(hit, "below", layout.text_h, ink.shape[1], ink.shape[0])
        self.assertGreater(y0, hit.y1 - 1)
        self.assertLess(y1 - y0, layout.text_h * 3)

    def test_printed_hint_words_are_masked(self):
        words = [
            {"text": "(Typedoctor's", "x": 100, "y": 50, "w": 120, "h": 16, "conf": 90.0, "line": (1, 1, 1)},
            {"text": "name/office)", "x": 230, "y": 50, "w": 110, "h": 16, "conf": 90.0, "line": (1, 1, 1)},
            {"text": "Charles", "x": 100, "y": 75, "w": 80, "h": 20, "conf": 40.0, "line": (1, 1, 2)},
        ]
        from intake_reader.labels import LabelHit
        from intake_reader.anchors import OLD_CHECKBOX as fam

        label_words = [{"text": "Doctor's", "x": 100, "y": 20, "w": 90, "h": 20, "conf": 90.0, "line": (1, 1, 0)}]
        hit = LabelHit("doctor", fam.options[0], label_words, 0, "hear", 0.9, 0, 400)
        boxes = _printed_boxes(words, (90, 42, 420, 100), hit)
        self.assertEqual(len(boxes), 2)


if __name__ == "__main__":
    unittest.main()
