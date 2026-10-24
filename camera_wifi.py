"""Windows Wi-Fi helper for the Soulear camera access point."""

from __future__ import annotations

import html
import locale
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


# The suffix identifies an individual camera and therefore must never be
# hard-coded.  Accept the spelling printed by this model plus the common
# phonetic/transliterated variant requested by the user.
CAMERA_SSID_PREFIXES = ("Soulear", "Soliar")
_NETWORK_RE = re.compile(r"^\s*SSID\s+\d+\s*:\s*(.+?)\s*$", re.IGNORECASE)
_CURRENT_RE = re.compile(r"^\s*SSID\s*:\s*(.+?)\s*$", re.IGNORECASE)


class CameraWifiError(RuntimeError):
    """Raised when Windows cannot find or join the camera network."""


@dataclass(frozen=True)
class WifiConnectResult:
    ssid: str
    already_connected: bool
    profile_created: bool


def _startup_info():
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return info


def _run_netsh(*arguments: str, timeout: float = 20.0) -> str:
    if os.name != "nt":
        raise CameraWifiError("Автоподключение Wi-Fi поддерживается только в Windows")
    completed = subprocess.run(
        ["netsh.exe", "wlan", *arguments],
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        timeout=timeout,
        startupinfo=_startup_info(),
        check=False,
    )
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode != 0:
        raise CameraWifiError(output or f"netsh завершился с кодом {completed.returncode}")
    return output


def _is_camera_ssid(ssid: str) -> bool:
    folded = ssid.casefold()
    return any(folded.startswith(prefix.casefold()) for prefix in CAMERA_SSID_PREFIXES)


def parse_visible_ssids(output: str) -> list[str]:
    """Parse SSIDs without depending on the Windows display language."""

    result: list[str] = []
    for line in output.splitlines():
        match = _NETWORK_RE.match(line)
        if not match:
            continue
        ssid = match.group(1).strip()
        if ssid and ssid not in result:
            result.append(ssid)
    return result


def parse_current_ssids(output: str) -> list[str]:
    result: list[str] = []
    for line in output.splitlines():
        match = _CURRENT_RE.match(line)
        if not match:
            continue
        ssid = match.group(1).strip()
        if ssid and ssid not in result:
            result.append(ssid)
    return result


def parse_camera_profiles(output: str) -> list[str]:
    """Extract known Soulear profiles from localized ``netsh`` output."""

    result: list[str] = []
    for line in output.splitlines():
        if ":" not in line:
            continue
        candidate = line.split(":", 1)[1].strip()
        if _is_camera_ssid(candidate) and candidate not in result:
            result.append(candidate)
    return result


def current_wifi_ssids() -> list[str]:
    return parse_current_ssids(_run_netsh("show", "interfaces"))


def visible_camera_ssids() -> list[str]:
    visible = parse_visible_ssids(_run_netsh("show", "networks", "mode=bssid"))
    return [ssid for ssid in visible if _is_camera_ssid(ssid)]


def known_camera_profiles() -> list[str]:
    return parse_camera_profiles(_run_netsh("show", "profiles"))


def build_open_profile_xml(ssid: str) -> str:
    """Build a current-user profile for the camera's passwordless AP."""

    escaped = html.escape(ssid, quote=True)
    ssid_hex = ssid.encode("utf-8").hex().upper()
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{escaped}</name>
  <SSIDConfig>
    <SSID><hex>{ssid_hex}</hex><name>{escaped}</name></SSID>
    <nonBroadcast>false</nonBroadcast>
  </SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM>
    <security>
      <authEncryption>
        <authentication>open</authentication>
        <encryption>none</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
    </security>
  </MSM>
</WLANProfile>
"""


def _wait_until_connected(ssid: str, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if ssid in current_wifi_ssids():
                return True
        except (CameraWifiError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.5)
    return False


def _add_open_profile(ssid: str, temp_directory: Path) -> None:
    temp_directory.mkdir(parents=True, exist_ok=True)
    profile_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".xml",
            prefix="soulear-wifi-",
            dir=temp_directory,
            encoding="utf-8",
            newline="\n",
            delete=False,
        ) as profile_file:
            profile_file.write(build_open_profile_xml(ssid))
            profile_path = Path(profile_file.name)
        _run_netsh("add", "profile", f"filename={profile_path}", "user=current")
    finally:
        if profile_path is not None:
            try:
                profile_path.unlink()
            except OSError:
                pass


def connect_to_camera_wifi(temp_directory: Path) -> WifiConnectResult:
    """Find and join a visible Soulear network, creating a profile if needed."""

    current = current_wifi_ssids()
    connected_camera = next((ssid for ssid in current if _is_camera_ssid(ssid)), None)
    if connected_camera:
        return WifiConnectResult(connected_camera, True, False)

    visible = visible_camera_ssids()
    profiles = known_camera_profiles()
    candidates = visible or profiles
    if not candidates:
        raise CameraWifiError(
            "сеть Soulear не найдена. Включите камеру и повторите поиск; "
            "если Windows скрывает список сетей, проверьте разрешение геолокации"
        )

    ssid = candidates[0]
    profile_created = False
    if ssid not in profiles:
        if ssid not in visible:
            raise CameraWifiError(f"сеть {ssid} сейчас не видна")
        _add_open_profile(ssid, temp_directory)
        profile_created = True

    _run_netsh("connect", f"name={ssid}", f"ssid={ssid}")
    if not _wait_until_connected(ssid):
        raise CameraWifiError(
            f"Windows не подключилась к {ssid}. Убедитесь, что камера включена и заряжена"
        )
    return WifiConnectResult(ssid, False, profile_created)
