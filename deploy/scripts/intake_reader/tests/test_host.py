"""Tests that need tesseract: orientation, write-in OCR and end-to-end readings of the four
sample intakes under intake_test/. Skipped automatically when tesseract is not installed.

Run inside the scraper container:
  INTAKE_TEST_PDFS=/app/intake_test python -m unittest intake_reader.tests.test_host
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

import numpy as np

HAS_TESSERACT = shutil.which("tesseract") is not None
PDF_DIR = Path(os.environ.get("INTAKE_TEST_PDFS", str(Path(__file__).resolve().parents[3] / "intake_test")))
EXPECTED = {
    "5fb9dc571bb2f7e3f9057684ed9.pdf": ("event", "old_bullet", ["phone"]),
    "33292e25ce3f631483da54a95c9.pdf": ("insurance", "new_circle", ["phone"]),
    "5a2115f5adf8f0fc836eca1b604.pdf": ("doctor", "new_circle", ["phone"]),
    "7395beaf47a7e383be74b9b198a.pdf": ("doctor", "new_circle", ["website"]),
}


@unittest.skipUnless(HAS_TESSERACT, "tesseract not installed")
class HostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PDF_DIR.is_dir():
            raise unittest.SkipTest(f"sample PDFs missing: {PDF_DIR}")

    def test_end_to_end_samples(self):
        from intake_reader import read_intake

        for name, (source, family, booking) in EXPECTED.items():
            path = PDF_DIR / name
            if not path.is_file():
                continue
            result = read_intake(str(path))
            reading = result.reading
            self.assertEqual(reading.source, source, (name, reading))
            self.assertEqual(reading.family, family, (name, reading.family))
            self.assertEqual(reading.booking, booking, (name, reading.booking))
            self.assertFalse(reading.needs_review, (name, reading.reasons))

    def test_upside_down_pdf_is_read(self):
        """Rotate a sample 180° in memory and expect the same answer (R1)."""
        import fitz

        from intake_reader import read_intake

        name = "33292e25ce3f631483da54a95c9.pdf"
        path = PDF_DIR / name
        if not path.is_file():
            self.skipTest("sample missing")
        doc = fitz.open(str(path))
        for page in doc:
            page.set_rotation(180)
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "intake_rot180.pdf"
        doc.save(str(tmp))
        doc.close()
        result = read_intake(str(tmp))
        self.assertEqual(result.reading.source, "insurance", result.reading)
        self.assertIn(180, [s.angle for s in result.scanned])

    def test_rotated_90(self):
        import fitz

        from intake_reader import read_intake

        name = "7395beaf47a7e383be74b9b198a.pdf"
        path = PDF_DIR / name
        if not path.is_file():
            self.skipTest("sample missing")
        doc = fitz.open(str(path))
        for page in doc:
            page.set_rotation(90)
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "intake_rot90.pdf"
        doc.save(str(tmp))
        doc.close()
        result = read_intake(str(tmp))
        self.assertEqual(result.reading.source, "doctor", result.reading)

    def test_osd_on_synthetic_text(self):
        from PIL import Image, ImageDraw

        from intake_reader.page import upright
        from .synth import font

        image = Image.new("L", (700, 900), 255)  # portrait, like a scanned intake page
        draw = ImageDraw.Draw(image)
        fnt = font(24)
        lines = [
            "Patient Information Full Name",
            "Date of Birth Address Phone",
            "How did you hear about us?",
            "Doctor referral Google Zocdoc",
            "Social Media Insurance",
            "Word of Mouth Event Outreach Other",
            "Insurance Information",
            "Primary Insurance Company Member ID",
        ]
        for i, line in enumerate(lines):
            draw.text((30, 60 + i * 80), line, fill=0, font=fnt)
        gray = np.asarray(image)
        flipped = np.ascontiguousarray(np.rot90(gray, 2))
        fixed, angle, how = upright(flipped, "eng")
        self.assertEqual(angle, 180, how)
        self.assertTrue(np.array_equal(fixed, gray))

    def test_writein_ocr_maps_keywords(self):
        from PIL import Image, ImageDraw

        from intake_reader.writein import map_text, ocr_strip
        from .synth import font

        image = Image.new("L", (500, 60), 255)
        ImageDraw.Draw(image).text((8, 10), "my sister", fill=0, font=font(30))
        text, conf = ocr_strip(np.asarray(image), (0, 0, 500, 60), "eng")
        self.assertEqual(map_text(text), "friend_family", text)


if __name__ == "__main__":
    unittest.main()
