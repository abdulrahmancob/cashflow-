"""Decision logic: multiple marks, write-ins, booking exclusion, review flags (R7, R8, R12–R15)."""

from __future__ import annotations

import unittest

from intake_reader.controls import Control
from intake_reader.decide import Reading, decide, merge_readings, primary
from intake_reader.writein import WriteIn, map_text


def ctrl(code: str, score: float) -> Control:
    c = Control(code, 0, 0, 10, 10, True)
    c.score = score
    c.marked = score >= 0
    return c


def wi(code: str, text: str, kind: str = "inline", mapped: str | None = None) -> WriteIn:
    return WriteIn(code, kind, text, 80.0, mapped if mapped is not None else map_text(text), 300, (0, 0, 10, 10))


class DecideTests(unittest.TestCase):
    def test_single_mark(self):
        r = decide("new_circle", 0, [ctrl("doctor", -1), ctrl("google", 0.9), ctrl("zocdoc", -1)], [], [])
        self.assertEqual(r.source, "google")
        self.assertEqual(r.marks, ["google"])
        self.assertFalse(r.needs_review)
        self.assertGreaterEqual(r.confidence, 0.9)

    def test_two_marks_become_multiple_with_priority(self):
        r = decide("old_checkbox", 0, [ctrl("google", 0.8), ctrl("zocdoc", 0.7), ctrl("doctor", -2)], [], [])
        self.assertEqual(r.source, "multiple")
        self.assertEqual(r.marks, ["zocdoc", "google"])
        self.assertTrue(r.needs_review)
        self.assertIn("primary:zocdoc", r.reasons)

    def test_unmarked_only_when_all_quiet(self):
        r = decide("old_checkbox", 0, [ctrl("google", -1.5), ctrl("doctor", -1.2)], [], [])
        self.assertEqual(r.source, "unmarked")
        self.assertFalse(r.needs_review)

    def test_near_miss_flags_review(self):
        r = decide("old_checkbox", 0, [ctrl("google", -0.1), ctrl("doctor", -1.2)], [], [])
        self.assertEqual(r.source, "unmarked")
        self.assertTrue(r.needs_review)

    def test_text_hint_breaks_near_tie_only(self):
        r = decide("old_checkbox", 0, [ctrl("google", -0.1), ctrl("doctor", -1.2)], [], [], text_hints={"google"})
        self.assertEqual(r.source, "google")
        self.assertIn("text_hint", r.reasons)
        r2 = decide("old_checkbox", 0, [ctrl("google", -1.5), ctrl("doctor", -1.2)], [], [], text_hints={"google"})
        self.assertEqual(r2.source, "unmarked")

    def test_booking_never_feeds_source(self):
        r = decide("new_circle", 0, [ctrl("google", -1), ctrl("doctor", -1)], [ctrl("walk_in", 1.0), ctrl("phone", -1)], [])
        self.assertEqual(r.source, "unmarked")
        self.assertEqual(r.booking, ["walk_in"])

    def test_other_writein_mapped(self):
        r = decide("new_circle", 0, [ctrl("other", -1), ctrl("google", -1)], [], [wi("other", "my sister")])
        self.assertEqual(r.source, "friend_family")
        self.assertEqual(r.other_text, "my sister")
        self.assertFalse(r.needs_review)

    def test_other_writein_unmapped_stays_other_for_review(self):
        r = decide("new_circle", 0, [ctrl("other", 0.5), ctrl("google", -1)], [], [wi("other", "question", mapped=None)])
        self.assertEqual(r.source, "other")
        self.assertTrue(r.needs_review)

    def test_doctor_name_without_box(self):
        r = decide("old_checkbox", 0, [ctrl("doctor", -1), ctrl("google", -1)], [], [wi("doctor", "Charles Aron Popcin", "below", mapped=None)])
        self.assertEqual(r.source, "doctor")
        self.assertTrue(r.other_text.startswith("doctor:"))

    def test_doctor_line_text_says_friend(self):
        r = decide("old_checkbox", 0, [ctrl("doctor", -1), ctrl("google", -1)], [], [wi("doctor", "My friend told me", "below")])
        self.assertEqual(r.source, "friend_family")

    def test_checked_option_beats_its_own_writein(self):
        r = decide("old_checkbox", 0, [ctrl("friend_family", 0.9), ctrl("google", -1)], [], [wi("friend_family", "in neighborhood")])
        self.assertEqual(r.source, "friend_family")
        self.assertIn("in neighborhood", r.other_text)

    def test_word_of_mouth_line_text_without_box(self):
        r = decide("old_checkbox", 0, [ctrl("friend_family", -1), ctrl("google", -1)], [], [wi("friend_family", "Friend told me")])
        self.assertEqual(r.source, "friend_family")

    def test_no_block(self):
        r = decide("", -1, [], [], [], block_found=False)
        self.assertEqual(r.source, "no_question")
        self.assertFalse(r.needs_review)

    def test_no_controls(self):
        r = decide("generic", 0, [], [], [])
        self.assertEqual(r.source, "unreadable")
        self.assertIn("controls_not_found", r.reasons)

    def test_block_cut_flags_review(self):
        r = decide("es_checkbox", 0, [ctrl("doctor", -1), ctrl("google", -1)], [], [], block_cut=True)
        self.assertTrue(r.needs_review)
        self.assertIn("block_cut", r.reasons)

    def test_priority_order(self):
        self.assertEqual(primary({"google", "zocdoc"}), "zocdoc")
        self.assertEqual(primary({"friend_family", "doctor"}), "doctor")
        self.assertEqual(primary({"other", "walk_in"}), "walk_in")


class KeywordTests(unittest.TestCase):
    def test_mapping(self):
        cases = {
            "my sister": "friend_family",
            "Mi hijo": "friend_family",
            "Through my wife": "friend_family",
            "walking on the block and saw the office": "walk_in",
            "WALK-BY": "walk_in",
            "I live next door": "walk_in",
            "Neighborhood": "walk_in",
            "AI search": "google",
            "search": "google",
            "Insurance Website": "insurance",
            "from list of approved providers": "insurance",
            "Anthem blue cross": "insurance",
            "another PT referred": "doctor",
            "ENT": "doctor",
            "Dr. Alexis": "doctor",
            "someone sent me your flyer": "event",
            "Zoc Doc": "zocdoc",
            "facebook": "social_media",
        }
        for text, code in cases.items():
            self.assertEqual(map_text(text), code, text)
        self.assertIsNone(map_text("not sure"))
        self.assertIsNone(map_text("Antonio Gaston"))


class MergeTests(unittest.TestCase):
    def test_best_evidence(self):
        a = Reading(source="unmarked", confidence=0.9, needs_review=False)
        b = Reading(source="google", marks=["google"], confidence=0.8, needs_review=False)
        self.assertEqual(merge_readings([a, b]).source, "google")

    def test_disagreement(self):
        a = Reading(source="doctor", marks=["doctor"], confidence=0.9, needs_review=False)
        b = Reading(source="google", marks=["google"], confidence=0.85, needs_review=False)
        m = merge_readings([a, b])
        self.assertEqual(m.source, "multiple")
        self.assertTrue(m.needs_review)
        self.assertEqual(m.marks, ["doctor", "google"])

    def test_unreadable_only(self):
        a = Reading(source="unreadable")
        self.assertEqual(merge_readings([a]).source, "unreadable")


if __name__ == "__main__":
    unittest.main()
