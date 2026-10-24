"""Offline regression tests for the Soulear UDP protocol helpers."""

import unittest

from soulear_camera import (
    CMD_EVENT,
    HEADER,
    MAGIC,
    FrameAssembler,
    parse_button_event,
    picture_button_presses,
    roll_degrees,
)


class PictureButtonTests(unittest.TestCase):
    def test_single_press(self) -> None:
        self.assertEqual(picture_button_presses(1, 2), 1)

    def test_wraparound_press(self) -> None:
        self.assertEqual(picture_button_presses(15, 0), 1)

    def test_backward_or_implausible_jump_is_ignored(self) -> None:
        self.assertEqual(picture_button_presses(8, 7), 0)

    def test_command_9_event_payload_uses_low_nibble_at_offset_5(self) -> None:
        payload = bytearray(17)
        payload[0] = 2
        payload[5:7] = (0x4325).to_bytes(2, "little")
        packet = HEADER.pack(MAGIC, 7, CMD_EVENT, 1, 0, len(payload)) + payload
        self.assertEqual(parse_button_event(packet), (2, 5))

    def test_non_event_packet_is_ignored(self) -> None:
        packet = HEADER.pack(MAGIC, 7, 4, 1, 0, 0)
        self.assertIsNone(parse_button_event(packet))


class FrameAssemblerTests(unittest.TestCase):
    def test_complete_frame_and_trailing_bytes(self) -> None:
        jpeg = b"\xff\xd8payload\xff\xd9"
        header = bytearray(16)
        header[0] = 1
        header[1] = 7
        header[2] = 4
        header[3] = 1
        header[4] = 1
        header[12:14] = (640).to_bytes(2, "little", signed=True)
        header[14:16] = (480).to_bytes(2, "little", signed=True)

        result = FrameAssembler().feed(bytes(header) + jpeg + b"trailer")

        self.assertIsNotNone(result)
        assert result is not None
        data, width, height, gsensor, flags = result
        self.assertEqual(data, jpeg)
        self.assertEqual((width, height, gsensor, flags), (640, 480, None, 0))


class RotationTests(unittest.TestCase):
    def test_fbpro_mount_offset(self) -> None:
        self.assertEqual(roll_degrees(1, "BK7231U-XRH-FBPRO"), 180.0)


if __name__ == "__main__":
    unittest.main()
