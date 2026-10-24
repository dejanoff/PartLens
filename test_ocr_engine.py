"""Integration tests for the local component-marking OCR."""

from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ocr_engine import RapidOCR, recognize_image, recognize_image_detailed


@unittest.skipUnless(RapidOCR is not None, "RapidOCR is not installed")
class ComponentOcrTests(unittest.TestCase):
    def test_component_markings(self) -> None:
        image = Image.new("RGB", (640, 240), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(r"C:\Windows\Fonts\consolab.ttf", 62)
        draw.text((28, 35), "NE555P", font=font, fill="black")
        draw.text((28, 120), "74HC595N", font=font, fill="black")

        text = recognize_image(image).replace("\n", " ")

        self.assertIn("NE555P", text)
        self.assertIn("74HC595N", text)

    def test_real_multiline_winbond_capture(self) -> None:
        sample = (
            Path(__file__).resolve().parent
            / "captures"
            / "camsorter-20261024-160325-452.jpg"
        )
        if not sample.exists():
            self.skipTest("User's real Winbond test capture is not present")

        with Image.open(sample) as image:
            result = recognize_image_detailed(image)

        self.assertGreaterEqual(result.line_count, 3)
        self.assertIn("25Q128JVSQ", result.text)
        self.assertIn("2218", result.text)

    def test_real_rotated_pal_capture(self) -> None:
        sample = (
            Path(__file__).resolve().parent
            / "captures"
            / "camsorter-20261024-162125-949.jpg"
        )
        if not sample.exists():
            self.skipTest("User's real rotated PAL test capture is not present")

        with Image.open(sample) as image:
            result = recognize_image_detailed(image)

        self.assertEqual("RapidOCR PP-OCRv6 (rectified)", result.engine)
        self.assertIn("PAL16R8ACN", result.text)
        self.assertIn("8513 1820-3605", result.text)

    def test_real_faint_esp_capture_finds_multiple_rows(self) -> None:
        sample = (
            Path(__file__).resolve().parent
            / "captures"
            / "camsorter-20261024-173038-419.jpg"
        )
        if not sample.exists():
            self.skipTest("User's faint ESP test capture is not present")

        with Image.open(sample) as image:
            result = recognize_image_detailed(image)

        self.assertEqual("RapidOCR PP-OCRv6 (component-clahe)", result.engine)
        self.assertGreaterEqual(result.line_count, 3)
        self.assertIn("ESP8266EX", result.text)
        self.assertTrue(any("J" in line for line in result.text.splitlines()))


if __name__ == "__main__":
    unittest.main()
