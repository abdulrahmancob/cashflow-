"""Label matching, grouping and family detection (R2, R3, R4, R10, R11)."""

from __future__ import annotations

import unittest

from intake_reader.anchors import ES_CHECKBOX, ES_CIRCLE, NEW_CIRCLE, OLD_BULLET, OLD_CHECKBOX, TINY, detect_family
from intake_reader.labels import analyse, group_lines, similarity

from .synth import Spec, draw_block


def words_from_text(lines: list[str], x0: int = 40, y0: int = 20, h: int = 20, gap: int = 34, cols: dict[int, int] | None = None) -> list[dict]:
    """Fake OCR words laid out line by line (optionally with a second column at cols[line])."""
    words: list[dict] = []
    for index, line in enumerate(lines):
        x = x0
        y = y0 + index * gap
        parts = line.split("|")
        for col, part in enumerate(parts):
            x = x0 if col == 0 else (cols or {}).get(index, 500)
            for token in part.split():
                w = len(token) * 11
                words.append({"text": token, "x": x, "y": y, "w": w, "h": h, "conf": 90.0, "line": (1, 1, index)})
                x += w + 9
    return words


class SimilarityTests(unittest.TestCase):
    def test_merged_glyph_prefixes(self):
        for window, key in (("oogie", "google"), ("qzocdoc", "zocdoc"), ("qweraofmouth", "wordofmouth"), ("ygoogle", "google"), ("eygoogle", "google"), ("cliniestaff", "clinicstaff"), ("ourwebsite", "ourwebsite")):
            self.assertGreaterEqual(similarity(window, key), 0.75, (window, key))

    def test_no_cross_option_matches(self):
        for window, key in (("doctor", "zocdoc"), ("seguro", "google"), ("recommendations", "insurancerecommendations"), ("insurance", "insurancerecommendations"), ("typedoctorsnameoffice", "doctorsreferralrecommendations")):
            self.assertLess(similarity(window, key), 0.7, (window, key))


class AnalyseTests(unittest.TestCase):
    def test_old_checkbox_with_booking_above(self):
        words = words_from_text(
            [
                "How did you book your appointment? (*) Please check what applies",
                "O Phone Call/ Text",
                "O Zocdoc",
                "O Our website (www.ptofthecity.com)/Google",
                "O Walk-in",
                "How did you hear about us? (*) Please check what applies",
                "O Doctor's referral/recommendations",
                "(Typedoctor's name/office)",
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
                "O Other(please specify)",
                "Insurance Information - Please provide insurance card(s)",
                "Policy Holder's Name:",
            ]
        )
        layout = analyse(words, OLD_CHECKBOX, 20)
        self.assertEqual(layout.question_line, 5)
        self.assertEqual(layout.booking_line, 0)
        self.assertEqual(layout.end_line, 20)
        self.assertEqual([h.code for h in layout.hear], ["doctor", "google", "zocdoc", "social_media", "insurance", "direct_mail", "friend_family", "marketing_table", "event", "clinic_staff", "flyer_doctor_office", "flyer_street", "other"])
        self.assertEqual([h.code for h in layout.booking], ["phone", "zocdoc", "website", "walk_in"])
        self.assertEqual(detect_family(layout.block_text).id, "old_checkbox")

    def test_ocr_garbled_labels(self):
        words = words_from_text(
            [
                "How did you hearabout us? (*) Please check what applies",
                "O Doctor's referral/recommendations",
                "Er Google",
                "O Zocdoc",
                "(| Event or community outreach",
                "O WordofMouth_",
                "O Cliniestaff____",
                "O Other(please Specify) <ansay|SentweYoOya",
            ]
        )
        layout = analyse(words, OLD_CHECKBOX, 20)
        self.assertEqual(layout.question_line, 0)
        self.assertEqual([h.code for h in layout.hear], ["doctor", "google", "zocdoc", "event", "friend_family", "clinic_staff", "other"])

    def test_new_circle_two_columns_and_booking(self):
        words = words_from_text(
            [
                "HOW DID YOU FIND US",
                "How did you book your appointment?",
                "O Phone / Text|O Website / Google",
                "O Zocdoc|O Walk-in",
                "How did you hear about us?",
                "O Doctor referral|O Google",
                "O Zocdoc|O Social Media",
                "O Insurance|O Word of Mouth",
                "O Event / Outreach",
                "O Other:",
                "INSURANCE INFORMATION",
            ]
        )
        layout = analyse(words, NEW_CIRCLE, 20)
        self.assertEqual(layout.question_line, 4)
        self.assertEqual(layout.booking_line, 1)
        self.assertEqual(sorted(h.code for h in layout.hear), ["doctor", "event", "friend_family", "google", "insurance", "other", "social_media", "zocdoc"])
        self.assertEqual(sorted(h.code for h in layout.booking), ["phone", "walk_in", "website", "zocdoc"])
        google = next(h for h in layout.hear if h.code == "google")
        self.assertGreater(google.left_limit, 0)
        self.assertEqual(detect_family(layout.block_text, " ".join(w["text"] for w in words)).id, "new_circle")

    def test_es_circle(self):
        words = words_from_text(
            [
                "¿Por cuál medio has programado su cita?",
                "O Teléfono / Mensaje de texto|O Sitio web / Google",
                "O Zocdoc|O Paciente Sin cita previa",
                "¿Cómo has escuchado sobre nosotros?",
                "O Una referencia médica|O Google",
                "O Zocdoc|O Redes sociales",
                "O Seguro|O Recomendación personal",
                "O Evento / Divulgación",
                "O Otro:",
                "INFORMACIÓN DEL SEGURO",
            ]
        )
        layout = analyse(words, ES_CIRCLE, 20)
        self.assertEqual(sorted(h.code for h in layout.hear), ["doctor", "event", "friend_family", "google", "insurance", "other", "social_media", "zocdoc"])
        self.assertEqual(sorted(h.code for h in layout.booking), ["phone", "walk_in", "website", "zocdoc"])
        self.assertEqual(detect_family(layout.block_text).id, "es_circle")

    def test_es_checkbox(self):
        words = words_from_text(
            [
                "¿Cómo reservó su cita? Marque lo que corresponda",
                "O Llamada telefónica",
                "O Zocdoc",
                "O Nuestro sitio web (www.ptofthecity.com)/ Google",
                "O Entrando/Pasando la oficina",
                "¿Cómo nos conoció? Marque lo que corresponda",
                "O Remisión/recomendaciones del médico",
                "(Escriba el nombre del médico/Oficina)",
                "O Google",
                "O Zocdoc",
                "O Redes sociales",
                "O Recomendaciones sobre seguros",
                "O Publicidad directa",
                "O De boca en boca",
                "O Tabla de Marketing",
                "O Evento o difusión en la comunidad",
                "O Personal de la clínica",
                "Folletos",
                "O Folletos de la oficina del doctor",
                "O Folletos de distribución en la calle",
                "O Otros (especifique)",
            ]
        )
        layout = analyse(words, ES_CHECKBOX, 20)
        self.assertEqual([h.code for h in layout.hear], ["doctor", "google", "zocdoc", "social_media", "insurance", "direct_mail", "friend_family", "marketing_table", "event", "clinic_staff", "flyer_doctor_office", "flyer_street", "other"])
        self.assertEqual([h.code for h in layout.booking], ["phone", "zocdoc", "website", "walk_in"])
        self.assertEqual(detect_family(layout.block_text).id, "es_checkbox")

    def test_tiny_form_inline_options(self):
        words = words_from_text(
            [
                "Have you received physical therapy this year somewhere else? Y/N",
                "How did you hear about us? O Doctor O Google O Social Media O Zocdoc",
                "O Walk-in O Flyers O Friends/Family Other:",
                "Insurance Information - Please provide insurance card(s)",
            ]
        )
        layout = analyse(words, TINY, 14)
        self.assertEqual(layout.question_line, 1)
        self.assertEqual(sorted(h.code for h in layout.hear), ["doctor", "event", "friend_family", "google", "other", "social_media", "walk_in", "zocdoc"])
        self.assertEqual(detect_family(layout.block_text).id, "tiny")

    def test_bullet_family(self):
        text = "How did you hear about us? Please check what applies o Doctor's referral/recommendations (Type doctor's name) o Google o Zocdoc o Social Media o Insurance recommendations o Direct mail o Word of Mouth o Event or community outreach (flyers) o Other (please specify)"
        self.assertIs(detect_family(text), OLD_BULLET)

    def test_question_missing(self):
        words = words_from_text(["Patient Information", "Full Name:", "Date of Birth:"])
        layout = analyse(words, OLD_CHECKBOX, 20)
        self.assertIsNone(layout.question_line)
        self.assertEqual(layout.hear, [])

    def test_group_lines_two_columns(self):
        words = words_from_text(["O Doctor referral|O Google"])
        lines = group_lines(words, 20)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].segments), 2)


class SynthLayoutTests(unittest.TestCase):
    def test_drawn_block_labels(self):
        block = draw_block("How did you hear about us?", [Spec("Doctor referral"), Spec("Google", column=1), Spec("Other:")], "circle")
        layout = analyse(block.words, NEW_CIRCLE, block.text_h)
        self.assertEqual(sorted(h.code for h in layout.hear), ["doctor", "google", "other"])


if __name__ == "__main__":
    unittest.main()
