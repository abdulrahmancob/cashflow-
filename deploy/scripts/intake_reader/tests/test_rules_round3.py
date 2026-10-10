"""Rules added after the tenth golden eval: loose windows stop before printed words, a ring around
one word of a label counts, fallback windows follow the siblings' row offset, floating handwriting
is read as the answer, unclear doctor-line text goes to review, a check running far out of its box
counts."""

from __future__ import annotations

import unittest

import numpy as np
from PIL import Image, ImageDraw

from intake_reader.anchors import NEW_CIRCLE, OLD_CHECKBOX
from intake_reader.controls import Control, _window, find_controls, score_controls
from intake_reader.decide import decide
from intake_reader.labels import LabelHit, analyse
from intake_reader.page import binarize, mask_lines
from intake_reader.writein import WriteIn, map_text, stray_ink

from .synth import Spec, _words_for, draw_block, font
from .test_controls import NEW_LEFT, NEW_RIGHT, OLD_LABELS, marked_codes, run_block


def _option(family, code):
    return next(o for o in family.options if o.code == code)


def _word(text, x, y, w, h, conf=95.0, line=(1, 1, 0)):
    return {"text": text, "x": x, "y": y, "w": w, "h": h, "conf": conf, "line": line}


class RoundEightTests(unittest.TestCase):
    def test_one_word_label_window_has_no_lost_word_extension(self):
        th = 20
        website = _word("Website", 100, 200, 80, 20)
        slash = _word("/", 190, 200, 8, 20)
        google = _word("Google", 300, 200, 70, 20, line=(1, 1, 1))
        hit = LabelHit("google", _option(NEW_CIRCLE, "google"), [google], 1, "hear", 1.0, 0, 900)
        lo, _hi = _window(hit, th, [website, slash, google], NEW_CIRCLE)
        self.assertAlmostEqual(lo, 300 - th * 5.0)
        # a label whose first word OCR lost still reaches back over the loose word
        doctor = _word("Doctor", 100, 300, 55, 20, line=(1, 1, 2))
        referral = _word("referral", 160, 300, 80, 20, line=(1, 1, 3))
        hit2 = LabelHit("doctor", _option(NEW_CIRCLE, "doctor"), [referral], 3, "hear", 0.9, 0, 900)
        lo2, _hi2 = _window(hit2, th, [doctor, referral], NEW_CIRCLE)
        self.assertAlmostEqual(lo2, 100 - th * 5.0)

    def test_loose_window_stops_before_printed_words(self):
        """A typed copy of the circle form prints no controls and its booking row "Website /
        Google" below the options: the google window must not sit on "Website /"."""
        image = Image.new("L", (900, 420), 255)
        draw = ImageDraw.Draw(image)
        fnt = font(22)
        words: list[dict] = []
        hits: list[LabelHit] = []
        rows = [("doctor", "Doctor referral"), ("zocdoc", "Zocdoc"), ("insurance", "Insurance"), ("event", "Event / Outreach")]
        for i, (code, text) in enumerate(rows):
            ws = _words_for(draw, text, 300, 40 + i * 44, fnt, (1, 1, i))
            words += ws
            hits.append(LabelHit(code, _option(NEW_CIRCLE, code), ws, i, "hear", 1.0, 0, 900))
        booking = _words_for(draw, "Website / Google", 190, 40 + 4 * 44, fnt, (1, 1, 4))
        words += booking
        google = [w for w in booking if w["text"] == "Google"]
        hits.append(LabelHit("google", _option(NEW_CIRCLE, "google"), google, 5, "hear", 1.0, 0, 900))
        th = 22
        ink = mask_lines(binarize(np.asarray(image), 165), th)
        controls = find_controls(ink, hits, th, NEW_CIRCLE, ink, words)
        score_controls(controls, NEW_CIRCLE)
        ctrl = next(c for c in controls if c.code == "google")
        website = next(w for w in booking if w["text"] == "Website")
        self.assertLessEqual(ctrl.x1, website["x"], (ctrl.x0, ctrl.x1, website["x"]))
        self.assertEqual(marked_codes(controls), [])
        self.assertTrue(all(c.extra.get("unverified") for c in controls))

    def test_ring_around_one_word_of_a_label_marks_it(self):
        block = draw_block("How did you hear about us?", [Spec(l, "", column=0) for l in NEW_LEFT] + [Spec(l, "", column=1) for l in NEW_RIGHT], "circle")
        image = block.image.copy()
        outreach = next(w for w in block.words if w["text"] == "Outreach")
        ImageDraw.Draw(image).ellipse(
            (outreach["x"] - 10, outreach["y"] - 9, outreach["x"] + outreach["w"] + 10, outreach["y"] + outreach["h"] + 9), outline=0, width=2
        )
        block.gray = np.asarray(image)
        layout, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["event"])
        self.assertEqual(next(c for c in controls if c.code == "event").reason, "circled")

    def test_fallback_window_follows_the_siblings_row_offset(self):
        """Boxes printed half a text height below their labels; the one box that did not print
        is measured where its siblings say it is, not on the label's row."""
        block = draw_block("How did you hear about us?", [Spec(label, "") for label in OLD_LABELS], "box", control_dy=14)
        gray = block.gray.copy()
        _spec, (x0, y0, x1, y1) = next(r for r in block.rows if r[0].label == "Google")
        gray[y0 - 3 : y1 + 4, x0 - 3 : x1 + 4] = 255
        image = Image.fromarray(gray)
        r = (x1 - x0) // 3
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        ImageDraw.Draw(image).ellipse((cx - r, cy - r, cx + r, cy + r), fill=0)  # a pen dot where the box should be
        block.gray = np.asarray(image)
        layout, controls = run_block(block, OLD_CHECKBOX)
        ctrl = next(c for c in controls if c.code == "google")
        self.assertFalse(ctrl.found)
        self.assertLessEqual(abs((ctrl.y0 + ctrl.y1) / 2 - cy), 4, (ctrl.y0, ctrl.y1, cy))
        self.assertEqual(marked_codes(controls), ["google"])

    def test_stray_handwriting_survives_low_confidence_words_and_has_a_box(self):
        block = draw_block("How did you hear about us?", [Spec(l, "") for l in OLD_LABELS], "box", width=1100)
        image = block.image.copy()
        ImageDraw.Draw(image).text((700, 296), "not sure", fill=0, font=font(40))
        gray = np.asarray(image)
        layout = analyse(block.words, OLD_CHECKBOX, block.text_h)
        ink = mask_lines(binarize(gray, 165), layout.text_h)
        controls = find_controls(ink, layout.hear, layout.text_h, OLD_CHECKBOX, ink, block.words)
        low = [_word("nof", 700, 302, 50, 26, conf=31.0), _word("sure", 760, 302, 70, 26, conf=28.0)]
        found = stray_ink(ink, block.words + low, controls, [], layout.hear, layout.text_h)
        self.assertIsNotNone(found)
        tall, total, box = found
        self.assertGreaterEqual(tall, 2)
        self.assertLessEqual(box[0], 702, box)
        self.assertGreaterEqual(box[2], 800, box)
        self.assertLessEqual(box[3] - box[1], layout.text_h * 3, box)
        confident = [dict(w, conf=90.0) for w in low]
        self.assertIsNone(stray_ink(ink, block.words + confident, controls, [], layout.hear, layout.text_h))

    def test_unclear_doctor_line_text_goes_to_review(self):
        controls = [Control(code, 40, 20 + 40 * i, 64, 44 + 40 * i, True) for i, code in enumerate(["doctor", "google", "zocdoc", "insurance"])]
        unclear = WriteIn("doctor", "below", "wate ww", 31.5, None, 1900, (0, 0, 10, 10))
        reading = decide("old_checkbox", 0, controls, [], [unclear])
        self.assertEqual(reading.source, "doctor")
        self.assertTrue(reading.needs_review)
        self.assertIn("doctor_text_unclear", reading.reasons)
        clear = WriteIn("doctor", "below", "Dr Smith", 82.0, None, 1900, (0, 0, 10, 10))
        reading = decide("old_checkbox", 0, controls, [], [clear])
        self.assertEqual(reading.source, "doctor")
        self.assertFalse(reading.needs_review)

    def test_check_running_far_out_of_its_box_counts(self):
        """The pen touched the box's corner and the check ran up into the gutter between the
        columns: the box itself stays clean, the stroke marks it (reviewed)."""
        block = draw_block("How did you hear about us?", [Spec(l, "", column=0) for l in NEW_LEFT] + [Spec(l, "", column=1) for l in NEW_RIGHT], "box")
        image = block.image.copy()
        _spec, (x0, y0, x1, y1) = next(r for r in block.rows if r[0].label == "Social Media")
        th = block.text_h
        ImageDraw.Draw(image).line((x0 - 1, y1 + 2, x0 - th * 5, y0 - th * 1.6), fill=0, width=3)
        block.gray = np.asarray(image)
        layout, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["social_media"])
        ctrl = next(c for c in controls if c.code == "social_media")
        self.assertFalse(ctrl.found)
        self.assertEqual(ctrl.reason, "swollen")

    def test_spanish_vivo_maps_to_walk_in(self):
        self.assertEqual(map_text("yo vivo aqui"), "walk_in")
        self.assertEqual(map_text("vive al lado"), "walk_in")
        self.assertEqual(map_text("vivo con mi hija"), "friend_family")


class RoundNineTests(unittest.TestCase):
    def test_filled_mark_interior_is_kept_but_a_line_is_dropped(self):
        from intake_reader.controls import _measure

        def measure(ink):
            c = Control("c", 30, 30, 60, 60, True)
            _measure(ink, c, 90, 0, 20)
            return c.interior

        blank = np.zeros((100, 100), dtype=bool)
        filled = blank.copy()
        filled[30:61, 30:61] = True  # a box scribbled full
        line = blank.copy()
        line[:, 44:47] = True  # a binder line down the whole crop through the box
        self.assertEqual(measure(blank), 0.0)
        self.assertGreater(measure(filled), 0.95)
        self.assertEqual(measure(line), 0.0)

    def test_pieces_left_by_a_binder_line_still_place_the_column(self):
        """A binder line through every box leaves two pieces per box and no whole control; the
        pieces still vote the column, and the one box with a cross in it is read."""
        block = draw_block("How did you hear about us?", [Spec(l, "x" if l == "Google" else "") for l in OLD_LABELS], "box")
        gray = block.gray.copy()
        _spec, (x0, y0, x1, y1) = next(r for r in block.rows if r[0].label == "Google")
        xc = (x0 + x1) // 2
        gray[:, xc - 1 : xc + 2] = 0
        block.gray = gray
        layout, controls = run_block(block, OLD_CHECKBOX)
        self.assertEqual(marked_codes(controls), ["google"])
        ctrl = next(c for c in controls if c.code == "google")
        self.assertEqual(ctrl.extra.get("place"), "column")
        self.assertLessEqual(abs(ctrl.x0 - x0), 4, (ctrl.x0, x0))

    def test_refine_window_needs_all_four_sides(self):
        from intake_reader.controls import _refine_window

        image = Image.new("L", (200, 120), 255)
        draw = ImageDraw.Draw(image)
        draw.line((80, 40, 80, 68), fill=0, width=2)  # left
        draw.line((80, 68, 108, 68), fill=0, width=2)  # bottom
        draw.line((108, 40, 108, 68), fill=0, width=2)  # right: a bracket, no top
        mask = np.asarray(image) < 128
        self.assertIsNone(_refine_window(mask, 62, 40, 92, 70, 20))
        draw.line((80, 40, 108, 40), fill=0, width=2)  # now a box
        mask = np.asarray(image) < 128
        self.assertIsNotNone(_refine_window(mask, 62, 40, 92, 70, 20))

    def test_check_leaving_the_box_sideways_counts(self):
        """A check drawn from the box's corner out into the gutter without rising above the
        row (B00): the merged component is far bigger than a box and carries ink around it."""
        block = draw_block("How did you hear about us?", [Spec(l, "", column=0) for l in NEW_LEFT] + [Spec(l, "", column=1) for l in NEW_RIGHT], "box")
        image = block.image.copy()
        _spec, (x0, y0, x1, y1) = next(r for r in block.rows if r[0].label == "Social Media")
        th = block.text_h
        ImageDraw.Draw(image).line((x0 + 2, y0 + 2, x0 - th * 1.5, y0 - th * 0.6), fill=0, width=3)
        block.gray = np.asarray(image)
        layout, controls = run_block(block, NEW_CIRCLE)
        self.assertEqual(marked_codes(controls), ["social_media"])
