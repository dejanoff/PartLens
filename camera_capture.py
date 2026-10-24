#!/usr/bin/env python3
"""Read device information and capture one JPEG frame from a Soulear camera."""

from __future__ import annotations

import argparse
import queue
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
REFERENCE_DIR = PROJECT_DIR / "third_party" / "Suear-Web-Viewer"

if not REFERENCE_DIR.is_dir():
    raise SystemExit(
        "Missing third_party/Suear-Web-Viewer. See README.md for setup details."
    )

sys.path.insert(0, str(REFERENCE_DIR))

from suear_mirror import SuearClient  # noqa: E402


def default_output_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return PROJECT_DIR / "captures" / f"soulear-{timestamp}.jpg"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture one JPEG frame from a Soulear Wi-Fi camera."
    )
    parser.add_argument(
        "--camera-ip",
        default="192.168.1.1",
        help="Camera IP address (default: 192.168.1.1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Maximum seconds to wait for a complete frame (default: 20)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JPEG path; defaults to a new timestamped file in captures/",
    )
    return parser.parse_args()


def capture_one_frame(client: SuearClient, timeout: float):
    results: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            client.open_video()
            frame = client.get_frame()
            if frame is None:
                raise RuntimeError("The camera stream ended before a complete frame arrived")
            results.put(("frame", frame))
        except BaseException as exc:  # Forward worker errors to the main thread.
            results.put(("error", exc))

    thread = threading.Thread(target=worker, name="soulear-frame-reader", daemon=True)
    thread.start()

    try:
        kind, value = results.get(timeout=timeout)
    except queue.Empty as exc:
        client.disconnect()
        raise TimeoutError(
            f"No complete camera frame arrived within {timeout:g} seconds"
        ) from exc

    if kind == "error":
        raise value
    return value


def save_new_jpeg(data: bytes, output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {output}")
    jpeg_end = data.rfind(b"\xff\xd9")
    if not data.startswith(b"\xff\xd8") or jpeg_end < 2:
        raise ValueError("Received frame is not a complete JPEG image")

    # The camera sends fixed-size UDP payloads and may pad the final JPEG chunk
    # with zero bytes. Keep the image only through its end-of-image marker.
    data = data[: jpeg_end + 2]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(output)
    return output


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")

    # Command requests should fail promptly if the selected device is unavailable.
    socket.setdefaulttimeout(min(args.timeout, 3.0))
    output = args.output if args.output is not None else default_output_path()
    client = SuearClient(args.camera_ip)

    try:
        info = client.device_info()
        print(f"Device:   {info.vendor} {info.product_id}")
        print(f"Firmware: {info.fw_version}")
        print(f"SSID:     {info.ssid}")
        print(f"Battery:  {info.battery}%")

        frame = capture_one_frame(client, args.timeout)
        saved = save_new_jpeg(bytes(frame.data), output)
        print(f"Frame:    {frame.width}x{frame.height}, {len(frame.data)} bytes")
        print(f"Saved:    {saved}")
        return 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
