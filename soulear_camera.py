"""Native UDP client for i4season/Soulear Wi-Fi cameras.

The implementation targets the protocol used by the YPC
BK7231U-XRH-FBPRO/HKV41B camera.  It contains no vendor binaries.
"""

from __future__ import annotations

import logging
import math
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


LOGGER = logging.getLogger(__name__)

MAGIC = 0xFFEEFFEE
CAMERA_IP = "192.168.1.1"
COMMAND_PORT = 10005
VIDEO_COMMAND_PORT = 10006
EVENT_PORT = 10007

CMD_DEVICE_INFO = 0x0001
CMD_LICENSE = 0x0002
CMD_OPEN_VIDEO = 0x0004
CMD_EVENT = 0x0009

HEADER = struct.Struct("<IHHBBH")


@dataclass(frozen=True)
class DeviceInfo:
    vendor: str
    product: str
    firmware: str
    ssid: str
    battery_percent: int


@dataclass(frozen=True)
class CameraFrame:
    jpeg: bytes
    width: int
    height: int
    roll_degrees: float | None
    flags: int
    received_at: float


class FrameAssembler:
    """Reassemble one JPEG from the camera's sequenced UDP chunks."""

    def __init__(self) -> None:
        self.frame_id = -1
        self.last_sequence = -1
        self.broken = False
        self.chunks: list[bytes] = []

    def reset(self) -> None:
        self.frame_id = -1
        self.last_sequence = -1
        self.broken = False
        self.chunks.clear()

    def feed(self, packet: bytes) -> tuple[bytes, int, int, int | None, int] | None:
        if not packet:
            return None
        packet_type = packet[0]
        header_size = 16 if packet_type == 1 else 28 if packet_type == 6 else 0
        if header_size == 0 or len(packet) <= header_size:
            return None

        sequence = packet[1]
        frame_id = packet[2]
        is_last = packet[3] != 0
        chunk_count = packet[4]
        flags = packet[5]

        if frame_id != self.frame_id:
            self.frame_id = frame_id
            self.chunks.clear()
            self.broken = False
        elif sequence != ((self.last_sequence + 1) & 0xFF):
            self.broken = True

        self.last_sequence = sequence
        self.chunks.append(packet[header_size:])
        if not is_last:
            return None

        complete = not self.broken and len(self.chunks) == chunk_count
        data = b"".join(self.chunks) if complete else b""
        self.frame_id = -1
        self.chunks.clear()

        if not data.startswith(b"\xff\xd8"):
            return None
        jpeg_end = data.rfind(b"\xff\xd9")
        if jpeg_end < 2:
            return None
        data = data[: jpeg_end + 2]

        gsensor = int.from_bytes(packet[6:10], "little") if flags & 0x01 else None
        width = int.from_bytes(packet[12:14], "little", signed=True)
        height = int.from_bytes(packet[14:16], "little", signed=True)
        return data, width, height, gsensor, flags


def _c_string(data: bytes, offset: int, size: int) -> str:
    raw = data[offset : offset + size]
    return raw.split(b"\0", 1)[0].decode("latin-1", "replace").strip()


def parse_device_info(payload: bytes) -> DeviceInfo:
    if len(payload) < 0x80:
        raise ValueError(f"Device-info payload is too short: {len(payload)} bytes")
    power = int.from_bytes(payload[0x77:0x79], "little")
    return DeviceInfo(
        vendor=_c_string(payload, 0x01, 32),
        product=_c_string(payload, 0x21, 32),
        firmware=_c_string(payload, 0x41, 16),
        ssid=_c_string(payload, 0x51, 32),
        battery_percent=max(0, min(100, power >> 9)),
    )


def _axis(sample: int, shift: int, sign_bit: int) -> int:
    magnitude = (sample >> shift) & 0x1FF
    return -magnitude if (sample >> sign_bit) & 1 else magnitude


def roll_degrees(sample: int, product: str) -> float | None:
    """Decode the camera's packed accelerometer sample into a roll angle."""

    y = _axis(sample, 10, 19)
    z = _axis(sample, 0, 9)
    if y == 0 and z == 0:
        return None
    angle = math.degrees(math.atan2(y, z)) % 360.0
    if "FBPRO" in product or "R1" in product:
        angle = (angle + 180.0) % 360.0
    return angle


def picture_button_presses(previous: int, current: int) -> int:
    """Return the accepted number of new shutter presses for 4-bit counters."""

    delta = (current - previous) & 0x0F
    return delta if 1 <= delta <= 9 else 0


def parse_button_event(packet: bytes) -> tuple[int, int] | None:
    """Return ``(event_type, picture_counter)`` from a command-9 datagram.

    The vendor library binds UDP/10007 and copies the 17-byte payload into its
    ``eventSetEar`` structure.  Payload bytes 5..6 contain four wrapping
    counters; the low nibble is the physical photo button.
    """

    if len(packet) < HEADER.size:
        return None
    magic, _sequence, command, _category, status, length = HEADER.unpack_from(packet)
    if magic != MAGIC or command != CMD_EVENT or status != 0:
        return None
    payload = packet[HEADER.size : HEADER.size + length]
    if len(payload) < 7:
        return None
    counters = int.from_bytes(payload[5:7], "little")
    return payload[0], counters & 0x0F


class SoulearCamera:
    """Background camera session with frame and event callbacks."""

    def __init__(
        self,
        camera_ip: str = CAMERA_IP,
        on_frame: Callable[[CameraFrame], None] | None = None,
        on_event: Callable[[str, object], None] | None = None,
    ) -> None:
        self.camera_ip = camera_ip
        self.on_frame = on_frame
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket_lock = threading.Lock()
        self._control_socket: socket.socket | None = None
        self._video_socket: socket.socket | None = None
        self._event_socket: socket.socket | None = None
        self._sequence = 0
        self._device_info: DeviceInfo | None = None
        self._last_roll: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="soulear-camera",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._socket_lock:
            sockets = (
                self._control_socket,
                self._video_socket,
                self._event_socket,
            )
        for sock in sockets:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _emit(self, kind: str, value: object = None) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, value)
        except Exception:
            LOGGER.exception("Camera event callback failed")

    def _next_sequence(self) -> int:
        current = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        return current

    def _request(
        self,
        sock: socket.socket,
        command: int,
        payload: bytes = b"",
        port: int = COMMAND_PORT,
        attempts: int = 5,
    ) -> bytes | None:
        sequence = self._next_sequence()
        request = HEADER.pack(MAGIC, sequence, command, 1, 0, len(payload)) + payload
        old_timeout = sock.gettimeout()
        sock.settimeout(0.18)
        try:
            for _ in range(attempts):
                if self._stop.is_set():
                    return None
                sock.sendto(request, (self.camera_ip, port))
                try:
                    while True:
                        reply, sender = sock.recvfrom(4096)
                        if sender[0] != self.camera_ip or len(reply) < HEADER.size:
                            continue
                        magic, reply_seq, reply_cmd, _, status, length = HEADER.unpack_from(reply)
                        if magic != MAGIC or reply_seq != sequence or reply_cmd != command:
                            continue
                        if status != 0:
                            raise OSError(
                                f"Camera command 0x{command:04x} returned status {status}"
                            )
                        return reply[HEADER.size : HEADER.size + length]
                except TimeoutError:
                    continue
                except socket.timeout:
                    continue
        finally:
            # stop() closes the sockets to wake a pending recvfrom().  Restoring
            # a timeout on that deliberately closed socket must not turn a
            # normal disconnect into a user-visible WinError 10038.
            try:
                sock.settimeout(old_timeout)
            except OSError:
                if not self._stop.is_set():
                    raise
        return None

    def _query_device_info(self, sock: socket.socket) -> DeviceInfo | None:
        payload = self._request(sock, CMD_DEVICE_INFO, attempts=8)
        if payload is None:
            return None
        info = parse_device_info(payload)
        self._device_info = info
        self._emit("device_info", info)
        return info

    def _open_video(self, sock: socket.socket, local_port: int, client_id: int) -> None:
        payload = struct.pack("<HHI", local_port, 0, client_id)
        response = self._request(
            sock,
            CMD_OPEN_VIDEO,
            payload,
            port=VIDEO_COMMAND_PORT,
            attempts=3,
        )
        if response is None:
            LOGGER.debug("Open-video acknowledgement timed out; waiting for stream anyway")

    def _run(self) -> None:
        control: socket.socket | None = None
        video: socket.socket | None = None
        event: socket.socket | None = None
        try:
            self._emit("status", f"Подключение к {self.camera_ip}…")
            control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            control.bind(("0.0.0.0", 0))
            control.settimeout(0.18)

            video = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            video.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            video.bind(("0.0.0.0", 0))
            video.settimeout(0.25)

            # The physical controls do not ride in the video header.  The
            # vendor library listens for command-9 eventSetEar datagrams on
            # this fixed local port.
            event = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            event.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            event.bind(("0.0.0.0", EVENT_PORT))
            event.setblocking(False)

            with self._socket_lock:
                self._control_socket = control
                self._video_socket = video
                self._event_socket = event

            info = self._query_device_info(control)
            if info is None:
                raise ConnectionError("Камера не ответила на запрос сведений")

            # The vendor client asks for the licence before opening video.
            self._request(control, CMD_LICENSE, attempts=2)

            local_port = video.getsockname()[1]
            client_id = int(time.time()) & 0xFFFFFFFF
            self._open_video(control, local_port, client_id)
            self._emit("connected", {"port": local_port})

            assembler = FrameAssembler()
            last_packet_at = time.monotonic()
            last_open_at = last_packet_at
            last_info_at = last_packet_at
            started_at = last_packet_at
            # eventSetEar counters start at zero after camera boot.  This also
            # mirrors the zero-initialised global used by libWifiCamera.so.
            picture_button_counter = 0
            frame_times: deque[float] = deque(maxlen=120)
            last_stats_at = 0.0
            frames_received = 0
            bytes_received = 0

            while not self._stop.is_set():
                now = time.monotonic()
                try:
                    packet, sender = video.recvfrom(8192)
                except (TimeoutError, socket.timeout):
                    packet = b""
                    sender = ("", 0)
                except OSError:
                    if self._stop.is_set():
                        break
                    raise

                if packet and sender[0] == self.camera_ip:
                    now = time.monotonic()
                    last_packet_at = now
                    bytes_received += len(packet)

                    assembled = assembler.feed(packet)
                    if assembled is not None:
                        jpeg, width, height, sample, flags = assembled
                        if sample is not None:
                            decoded_roll = roll_degrees(sample, info.product)
                            if decoded_roll is not None:
                                self._last_roll = decoded_roll
                        frame = CameraFrame(
                            jpeg=jpeg,
                            width=width,
                            height=height,
                            roll_degrees=self._last_roll,
                            flags=flags,
                            received_at=time.time(),
                        )
                        frames_received += 1
                        frame_times.append(now)
                        if self.on_frame is not None:
                            try:
                                self.on_frame(frame)
                            except Exception:
                                LOGGER.exception("Frame callback failed")

                # Drain every pending event datagram without delaying video.
                while not self._stop.is_set():
                    try:
                        event_packet, event_sender = event.recvfrom(4096)
                    except (BlockingIOError, TimeoutError, socket.timeout):
                        break
                    except OSError:
                        if self._stop.is_set():
                            break
                        raise
                    if event_sender[0] != self.camera_ip:
                        continue
                    parsed_event = parse_button_event(event_packet)
                    if parsed_event is None:
                        continue
                    event_type, current_counter = parsed_event
                    delta = picture_button_presses(
                        picture_button_counter,
                        current_counter,
                    )
                    picture_button_counter = current_counter
                    self._emit(
                        "button_state",
                        {
                            "event_type": event_type,
                            "counter": current_counter,
                        },
                    )
                    if delta:
                        LOGGER.info(
                            "Physical photo button: counter=%d presses=%d",
                            current_counter,
                            delta,
                        )
                        self._emit(
                            "hardware_button",
                            {
                                "count": delta,
                                "counter": current_counter,
                                "received_at": time.time(),
                            },
                        )

                now = time.monotonic()
                if now - last_packet_at > 1.0 and now - last_open_at > 1.0:
                    self._open_video(control, local_port, client_id)
                    last_open_at = now
                    if now - started_at > 5.0:
                        self._emit("status", "Ожидание видеопотока…")

                if now - last_info_at >= 10.0:
                    refreshed = self._query_device_info(control)
                    if refreshed is not None:
                        info = refreshed
                    last_info_at = now

                if now - last_stats_at >= 1.0:
                    cutoff = now - 1.0
                    while frame_times and frame_times[0] < cutoff:
                        frame_times.popleft()
                    self._emit(
                        "stats",
                        {
                            "fps": len(frame_times),
                            "frames": frames_received,
                            "megabytes": bytes_received / (1024 * 1024),
                        },
                    )
                    last_stats_at = now

        except Exception as exc:
            if not self._stop.is_set():
                LOGGER.exception("Camera session failed")
                self._emit("error", f"{type(exc).__name__}: {exc}")
        finally:
            for sock in (event, video, control):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            with self._socket_lock:
                self._control_socket = None
                self._video_socket = None
                self._event_socket = None
            self._emit("disconnected", None)
