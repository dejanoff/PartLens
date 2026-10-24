#!/usr/bin/env python3
"""Listen directly for Soulear physical-control events on UDP/10007."""

from __future__ import annotations

import argparse
import csv
import socket
import time
from datetime import datetime
from pathlib import Path

from soulear_camera import CAMERA_IP, EVENT_PORT, parse_button_event


LOG_DIR = Path(__file__).resolve().parent / "logs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--camera-ip", default=CAMERA_IP)
    args = parser.parse_args()

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
    listener.bind(("0.0.0.0", EVENT_PORT))
    listener.settimeout(0.5)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"event-probe-{stamp}.csv"
    print(f"READY: listening on UDP {EVENT_PORT} for {args.duration:.0f} seconds", flush=True)
    print("Press the physical camera button briefly every 2-3 seconds.", flush=True)

    started = time.monotonic()
    packets = 0
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["seconds", "local_time", "sender", "length", "parsed", "raw_hex"]
        )
        while time.monotonic() - started < args.duration:
            try:
                packet, sender = listener.recvfrom(4096)
            except (TimeoutError, socket.timeout):
                continue
            elapsed = time.monotonic() - started
            parsed = parse_button_event(packet)
            writer.writerow(
                [
                    f"{elapsed:.6f}",
                    datetime.now().isoformat(timespec="milliseconds"),
                    f"{sender[0]}:{sender[1]}",
                    len(packet),
                    repr(parsed),
                    packet.hex(),
                ]
            )
            stream.flush()
            packets += 1
            if parsed is None:
                print(
                    f"RAW   +{elapsed:06.2f}s {sender[0]}:{sender[1]} "
                    f"len={len(packet)} {packet.hex()}",
                    flush=True,
                )
            else:
                event_type, counter = parsed
                print(
                    f"EVENT +{elapsed:06.2f}s type={event_type} "
                    f"picture_counter={counter} raw={packet.hex()}",
                    flush=True,
                )

    listener.close()
    print(f"DONE: {packets} event datagrams; CSV: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
