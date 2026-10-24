"""Tests for camera-viewer helpers that do not require a Tk window."""

from __future__ import annotations

import unittest

from CAMSORTER_Camera_v1 import choose_pre_shutter_frame
from soulear_camera import CameraFrame


def frame(received_at: float) -> CameraFrame:
    return CameraFrame(b"jpeg", 640, 480, 0.0, 0, received_at)


class PreShutterTests(unittest.TestCase):
    def test_chooses_frame_before_configured_delay(self) -> None:
        frames = [frame(9.60), frame(9.70), frame(9.80), frame(9.90)]
        selected = choose_pre_shutter_frame(frames, event_time=10.0, delay_ms=250)
        self.assertIsNotNone(selected)
        self.assertEqual(9.70, selected.received_at)

    def test_uses_oldest_frame_while_buffer_is_still_filling(self) -> None:
        frames = [frame(9.90), frame(9.95)]
        selected = choose_pre_shutter_frame(frames, event_time=10.0, delay_ms=500)
        self.assertIsNotNone(selected)
        self.assertEqual(9.90, selected.received_at)

    def test_empty_buffer(self) -> None:
        self.assertIsNone(choose_pre_shutter_frame([], 10.0, 250))


if __name__ == "__main__":
    unittest.main()
