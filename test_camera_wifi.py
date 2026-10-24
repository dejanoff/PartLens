"""Parser and profile tests for the Windows camera Wi-Fi helper."""

from __future__ import annotations

import unittest

from camera_wifi import (
    CAMERA_SSID_PREFIXES,
    build_open_profile_xml,
    parse_camera_profiles,
    parse_current_ssids,
    parse_visible_ssids,
)


class CameraWifiTests(unittest.TestCase):
    def test_parse_visible_networks(self) -> None:
        output = """
SSID 1 : Home
    Network type            : Infrastructure
    BSSID 1                 : 11:22:33:44:55:66
SSID 2 : Soulear-c80fc
"""
        self.assertEqual(["Home", "Soulear-c80fc"], parse_visible_ssids(output))

    def test_parse_current_interface(self) -> None:
        output = """
    Name                   : WLAN
    Status                 : connected
    SSID                   : Soulear-c80fc
    BSSID                  : aa:bb:cc:dd:ee:ff
"""
        self.assertEqual(["Soulear-c80fc"], parse_current_ssids(output))

    def test_parse_localized_profile_listing(self) -> None:
        output = """
    Gruppenrichtlinienprofile (schreibgeschützt)
    Benutzerprofile        : Home
    Benutzerprofile        : Soulear-c80fc
"""
        self.assertEqual(["Soulear-c80fc"], parse_camera_profiles(output))

    def test_open_profile_contains_utf8_ssid(self) -> None:
        xml = build_open_profile_xml("Soulear-c80fc")
        self.assertIn("<authentication>open</authentication>", xml)
        self.assertIn("<encryption>none</encryption>", xml)
        self.assertIn("536F756C6561722D6338306663", xml)

    def test_camera_prefix_does_not_depend_on_unique_suffix(self) -> None:
        def matches(ssid: str) -> bool:
            return any(
                ssid.casefold().startswith(prefix.casefold())
                for prefix in CAMERA_SSID_PREFIXES
            )

        self.assertTrue(matches("Soulear-c80fc"))
        self.assertTrue(matches("Soulear-a12b9"))
        self.assertTrue(matches("Soliar-SECOND-CAMERA"))
        self.assertFalse(matches("Home-Soulear-c80fc"))


if __name__ == "__main__":
    unittest.main()
