#!/usr/bin/env python3
"""Legacy full-session/video-header recorder.

Physical buttons are not carried by the video header.  Use ``event_probe.py``
for a passive UDP/10007 capture; the application itself now handles command-9
``eventSetEar`` packets through :mod:`soulear_camera`.
"""

from __future__ import annotations

import argparse
import csv
import socket
import struct
import time
from datetime import datetime
from pathlib import Path

from soulear_camera import (
    CAMERA_IP,
    CMD_DEVICE_INFO,
    CMD_LICENSE,
    CMD_OPEN_VIDEO,
    COMMAND_PORT,
    HEADER,
    MAGIC,
    VIDEO_COMMAND_PORT,
    parse_device_info,
)


PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"


def request(
    sock: socket.socket,
    camera_ip: str,
    sequence: int,
    command: int,
    payload: bytes = b"",
    port: int = COMMAND_PORT,
    attempts: int = 5,
) -> bytes | None:
    message = HEADER.pack(MAGIC, sequence, command, 1, 0, len(payload)) + payload
    for _ in range(attempts):
        sock.sendto(message, (camera_ip, port))
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            try:
                reply, sender = sock.recvfrom(4096)
            except (TimeoutError, socket.timeout):
                break
            if sender[0] != camera_ip or len(reply) < HEADER.size:
                continue
            magic, reply_seq, reply_cmd, _, status, length = HEADER.unpack_from(reply)
            if magic != MAGIC or reply_seq != sequence or reply_cmd != command:
                continue
            if status:
                raise OSError(f"command 0x{command:04x} returned status {status}")
            return reply[HEADER.size : HEADER.size + length]
    return None


def connect(camera_ip: str) -> tuple[socket.socket, socket.socket, str]:
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control.bind(("0.0.0.0", 0))
    control.settimeout(0.18)

    video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    video.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    video.bind(("0.0.0.0", 0))
    video.settimeout(0.5)

    try:
        info_payload = request(control, camera_ip, 1, CMD_DEVICE_INFO, attempts=8)
        if info_payload is None:
            raise TimeoutError("device-info request timed out")
        info = parse_device_info(info_payload)
        request(control, camera_ip, 2, CMD_LICENSE, attempts=2)
        local_port = video.getsockname()[1]
        open_payload = struct.pack("<HHI", local_port, 0, int(time.time()) & 0xFFFFFFFF)
        request(
            control,
            camera_ip,
            3,
            CMD_OPEN_VIDEO,
            open_payload,
            port=VIDEO_COMMAND_PORT,
            attempts=3,
        )
        description = (
            f"{info.vendor} {info.product}, firmware {info.firmware}, "
            f"battery {info.battery_percent}%, UDP {local_port}"
        )
        return control, video, description
    except Exception:
        control.close()
        video.close()
        raise


def run_probe(camera_ip: str, duration: float, wait: float) -> int:
    deadline = time.monotonic() + wait
    attempt = 0
    while True:
        attempt += 1
        try:
            control, video, description = connect(camera_ip)
            break
        except (OSError, TimeoutError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"ERROR: camera did not become ready: {exc}", flush=True)
                return 2
            print(
                f"WAITING: camera is not ready ({exc}); retrying, "
                f"{remaining:.0f}s left",
                flush=True,
            )
            time.sleep(min(2.0, max(0.1, remaining)))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"button-probe-{stamp}.csv"
    print(f"CONNECTED: {description}", flush=True)
    print("READY: press the camera button briefly every 2-3 seconds.", flush=True)
    print(f"RECORDING: {duration:.0f} seconds -> {path}", flush=True)

    started = time.monotonic()
    last_report = started
    packets = 0
    frames = 0
    last_byte5: int | None = None
    byte5_changes: list[tuple[float, int, int]] = []

    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "seconds",
                "local_time",
                "packet_type",
                "sequence",
                "frame_id",
                "last_chunk",
                "chunk_count",
                "byte5",
                "low_nibble",
                "high_nibble",
                "gsensor_hex",
                "header_hex",
                "packet_length",
            ]
        )

        while time.monotonic() - started < duration:
            try:
                packet, sender = video.recvfrom(8192)
            except (TimeoutError, socket.timeout):
                continue
            if sender[0] != camera_ip or len(packet) < 16:
                continue

            now = time.monotonic()
            elapsed = now - started
            header_size = 28 if packet[0] == 6 else 16
            header = packet[: min(header_size, len(packet))]
            byte5 = packet[5]
            packets += 1
            if packet[3]:
                frames += 1

            writer.writerow(
                [
                    f"{elapsed:.6f}",
                    datetime.now().isoformat(timespec="milliseconds"),
                    packet[0],
                    packet[1],
                    packet[2],
                    packet[3],
                    packet[4],
                    byte5,
                    byte5 & 0x0F,
                    byte5 >> 4,
                    packet[6:10].hex(),
                    header.hex(),
                    len(packet),
                ]
            )

            if last_byte5 is None:
                last_byte5 = byte5
                print(
                    f"BASELINE +{elapsed:06.2f}s: byte5=0x{byte5:02x} "
                    f"(low={byte5 & 15}, high={byte5 >> 4})",
                    flush=True,
                )
            elif byte5 != last_byte5:
                byte5_changes.append((elapsed, last_byte5, byte5))
                print(
                    f"CHANGE   +{elapsed:06.2f}s: byte5 0x{last_byte5:02x} -> "
                    f"0x{byte5:02x} (low={byte5 & 15}, high={byte5 >> 4})",
                    flush=True,
                )
                last_byte5 = byte5

            if now - last_report >= 5.0:
                print(
                    f"LIVE     +{elapsed:06.2f}s: {frames} frames, "
                    f"{packets} packets, {len(byte5_changes)} byte5 changes",
                    flush=True,
                )
                last_report = now

    control.close()
    video.close()
    print(
        f"DONE: {frames} frames, {packets} packets, "
        f"{len(byte5_changes)} byte5 changes",
        flush=True,
    )
    for elapsed, old, new in byte5_changes:
        print(f"  +{elapsed:06.2f}s  0x{old:02x} -> 0x{new:02x}", flush=True)
    print(f"CSV: {path}", flush=True)
    return 0


def main() -> int:
    print(
        "NOTE: this legacy tool records video headers, not button events. "
        "Use event_probe.py for UDP/10007 button diagnostics.",
        flush=True,
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-ip", default=CAMERA_IP)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--wait", type=float, default=90.0)
    args = parser.parse_args()
    return run_probe(args.camera_ip, args.duration, args.wait)


if __name__ == "__main__":
    raise SystemExit(main())
