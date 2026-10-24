#!/usr/bin/env python3
"""PartLens live viewer for the Soulear Wi-Fi inspection camera."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from collections import deque
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tkinter import ttk
from typing import Iterable
from urllib.parse import quote_plus

from PIL import Image, ImageOps, ImageTk

from camera_wifi import CameraWifiError, WifiConnectResult, connect_to_camera_wifi
from ocr_engine import recognize_image_detailed
from soulear_camera import CameraFrame, DeviceInfo, SoulearCamera


APP_NAME = "PartLens"
PROJECT_DIR = Path(__file__).resolve().parent
CAPTURE_DIR = PROJECT_DIR / "captures"
LOG_DIR = PROJECT_DIR / "logs"


def choose_pre_shutter_frame(
    frames: Iterable[CameraFrame],
    event_time: float,
    delay_ms: int,
) -> CameraFrame | None:
    """Choose the newest cached frame at or before the requested pre-shot time."""

    available = list(frames)
    if not available:
        return None
    target_time = event_time - max(0, delay_ms) / 1000.0
    older = [frame for frame in available if frame.received_at <= target_time]
    if older:
        return max(older, key=lambda frame: frame.received_at)
    return min(available, key=lambda frame: frame.received_at)


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "camsorter_camera.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


class CameraApplication:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1180x780")
        self.root.minsize(900, 620)

        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._frame_lock = threading.Lock()
        self._incoming_frame: CameraFrame | None = None
        self._frame_history: deque[CameraFrame] = deque(maxlen=180)
        self._incoming_serial = 0
        self._rendered_serial = -1
        self._latest_source_image: Image.Image | None = None
        self._latest_frame: CameraFrame | None = None
        self._tk_image: ImageTk.PhotoImage | None = None
        self._latest_snapshot_image: Image.Image | None = None
        self._latest_snapshot_path: Path | None = None
        self._snapshot_ocr_lines: list[str] = []
        self._snapshot_tk_image: ImageTk.PhotoImage | None = None
        self._frame_times: deque[float] = deque(maxlen=120)
        self._device_info: DeviceInfo | None = None
        self._closing = False
        self._ocr_generation = 0
        self._ocr_text_value = ""
        self._wifi_busy = False

        self.auto_rotate = tk.BooleanVar(value=True)
        self.mirror = tk.BooleanVar(value=False)
        self.pre_shutter_ms = tk.IntVar(value=250)
        self.status_text = tk.StringVar(value="Запуск…")
        self.device_text = tk.StringVar(value="Камера: ожидается подключение")
        self.stats_text = tk.StringVar(value="0 fps")
        self.ocr_status_text = tk.StringVar(value="OCR: ожидается фотография")

        self._build_style()
        self._build_ui()
        self._load_last_snapshot()

        self.camera = SoulearCamera(
            on_frame=self._receive_frame,
            on_event=self._receive_event,
        )

        self.root.bind("<space>", self._keyboard_snapshot)
        self.root.bind("<Key-s>", self._keyboard_snapshot)
        self.root.bind("<Key-S>", self._keyboard_snapshot)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.after(50, self._poll)
        self.root.after(33, self._render_latest)
        self.root.after(300, lambda: self._connect_camera_wifi(auto=True))

    def _build_style(self) -> None:
        self.root.configure(bg="#111820")
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background="#111820")
        style.configure(
            "Title.TLabel",
            background="#111820",
            foreground="#f3f6f8",
            font=("Segoe UI", 15, "bold"),
        )
        style.configure(
            "Info.TLabel",
            background="#111820",
            foreground="#aebdca",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Status.TLabel",
            background="#1a2632",
            foreground="#dbe7ef",
            padding=(10, 7),
            font=("Segoe UI", 10),
        )
        style.configure(
            "Capture.TButton",
            font=("Segoe UI", 12, "bold"),
            padding=(18, 10),
        )
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7))
        style.configure("LineCopy.TButton", font=("Segoe UI", 9), padding=(7, 4))
        style.configure(
            "TCheckbutton",
            background="#111820",
            foreground="#e3ebf1",
            font=("Segoe UI", 10),
        )
        style.map("TCheckbutton", background=[("active", "#111820")])
        style.configure(
            "OCR.TLabelframe",
            background="#111820",
            foreground="#dbe7ef",
            bordercolor="#314353",
        )
        style.configure(
            "OCR.TLabelframe.Label",
            background="#111820",
            foreground="#dbe7ef",
            font=("Segoe UI", 10, "bold"),
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.stats_text, style="Info.TLabel").pack(
            side=tk.RIGHT
        )

        ttk.Label(outer, textvariable=self.device_text, style="Info.TLabel").pack(
            fill=tk.X, pady=(0, 8)
        )

        image_area = ttk.Frame(outer, style="App.TFrame")
        image_area.pack(fill=tk.BOTH, expand=True)
        image_area.columnconfigure(0, weight=1)
        image_area.columnconfigure(1, weight=1)
        image_area.rowconfigure(0, weight=1)

        snapshot_frame = ttk.LabelFrame(
            image_area,
            text="Последний снимок",
            style="OCR.TLabelframe",
            padding=4,
        )
        snapshot_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        live_frame = ttk.LabelFrame(
            image_area,
            text="LIVE — камера",
            style="OCR.TLabelframe",
            padding=4,
        )
        live_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        self.snapshot_canvas = tk.Canvas(
            snapshot_frame,
            bg="#030609",
            highlightthickness=1,
            highlightbackground="#314353",
        )
        self.snapshot_canvas.pack(fill=tk.BOTH, expand=True)
        self.snapshot_canvas.create_text(
            20,
            20,
            anchor="nw",
            text="Здесь появится последний снимок",
            fill="#8193a1",
            font=("Segoe UI", 12),
            tags=("placeholder",),
        )
        self.snapshot_canvas.bind("<Configure>", lambda _event: self._draw_snapshot())

        self.canvas = tk.Canvas(
            live_frame,
            bg="#030609",
            highlightthickness=1,
            highlightbackground="#314353",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_text(
            20,
            20,
            anchor="nw",
            text="Ожидание изображения с камеры…",
            fill="#8193a1",
            font=("Segoe UI", 14),
            tags=("placeholder",),
        )

        controls = ttk.Frame(outer, style="App.TFrame")
        controls.pack(fill=tk.X, pady=(10, 6))

        self.capture_button = ttk.Button(
            controls,
            text="📷  СДЕЛАТЬ ФОТО",
            style="Capture.TButton",
            command=lambda: self._take_photo("экранная кнопка"),
            state=tk.DISABLED,
        )
        self.capture_button.pack(side=tk.LEFT, padx=(0, 8))

        self.connect_button = ttk.Button(
            controls,
            text="Подключиться",
            command=self._toggle_connection,
        )
        self.connect_button.pack(side=tk.LEFT, padx=4)

        self.wifi_button = ttk.Button(
            controls,
            text="Подключить Wi-Fi камеры",
            command=self._connect_camera_wifi,
        )
        self.wifi_button.pack(side=tk.LEFT, padx=4)

        ttk.Button(
            controls,
            text="Папка снимков",
            command=self._open_capture_folder,
        ).pack(side=tk.LEFT, padx=4)

        options = ttk.Frame(outer, style="App.TFrame")
        options.pack(fill=tk.X, pady=(2, 6))
        ttk.Checkbutton(
            options,
            text="Автоповорот по гироскопу",
            variable=self.auto_rotate,
            command=self._force_redraw,
        ).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Checkbutton(
            options,
            text="Зеркальное изображение",
            variable=self.mirror,
            command=self._force_redraw,
        ).pack(side=tk.LEFT)
        ttk.Label(
            options,
            text="Предкадр физической кнопки:",
            style="Info.TLabel",
        ).pack(side=tk.LEFT, padx=(22, 5))
        self.pre_shutter_spinbox = ttk.Spinbox(
            options,
            from_=0,
            to=1000,
            increment=50,
            width=5,
            textvariable=self.pre_shutter_ms,
            justify=tk.RIGHT,
        )
        self.pre_shutter_spinbox.pack(side=tk.LEFT)
        ttk.Label(options, text="мс", style="Info.TLabel").pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Label(
            options,
            text="Пробел / S — снимок",
            style="Info.TLabel",
        ).pack(side=tk.RIGHT)

        ocr_frame = ttk.LabelFrame(
            outer,
            text="Распознанный текст",
            style="OCR.TLabelframe",
            padding=(8, 6),
        )
        ocr_frame.pack(fill=tk.X, pady=(2, 8))

        ocr_toolbar = ttk.Frame(ocr_frame, style="App.TFrame")
        ocr_toolbar.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(
            ocr_toolbar,
            textvariable=self.ocr_status_text,
            style="Info.TLabel",
        ).pack(side=tk.LEFT)
        self.copy_ocr_button = ttk.Button(
            ocr_toolbar,
            text="Копировать текст",
            command=self._copy_ocr_text,
            state=tk.DISABLED,
        )
        self.copy_ocr_button.pack(side=tk.RIGHT)

        self.ocr_lines_frame = tk.Frame(
            ocr_frame,
            bg="#071019",
            padx=6,
            pady=5,
        )
        self.ocr_lines_frame.pack(fill=tk.X)
        self._rebuild_ocr_lines([])

        self.status_label = ttk.Label(
            outer,
            textvariable=self.status_text,
            style="Status.TLabel",
            anchor="w",
        )
        self.status_label.pack(fill=tk.X)

    def _receive_frame(self, frame: CameraFrame) -> None:
        with self._frame_lock:
            self._incoming_frame = frame
            self._incoming_serial += 1
            self._frame_history.append(frame)

    def _receive_event(self, kind: str, value: object) -> None:
        self._events.put((kind, value))

    def _connect_camera_wifi(self, auto: bool = False) -> None:
        if self._wifi_busy:
            if not auto:
                self.status_text.set("Поиск Wi-Fi камеры уже выполняется…")
            return

        self._wifi_busy = True
        self.wifi_button.config(state=tk.DISABLED, text="Поиск Wi-Fi…")
        self.status_text.set("Поиск сети Soulear и подключение Windows…")

        def worker() -> None:
            try:
                result = connect_to_camera_wifi(LOG_DIR)
                self._events.put(("wifi_connected", result))
            except (CameraWifiError, OSError, subprocess.SubprocessError) as exc:
                self._events.put(("wifi_error", str(exc)))
            except Exception as exc:
                logging.getLogger(__name__).exception("Wi-Fi connection failed")
                self._events.put(("wifi_error", str(exc)))

        threading.Thread(target=worker, name="camera-wifi", daemon=True).start()

    def _connect(self) -> None:
        if not self.camera.running:
            self.status_text.set("Подключение к камере 192.168.1.1…")
            self.connect_button.config(text="Отключиться")
            self.camera.start()

    def _disconnect(self) -> None:
        self.status_text.set("Отключение…")
        self.camera.stop()

    def _toggle_connection(self) -> None:
        if self.camera.running:
            self._disconnect()
        else:
            self._connect()

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self._events.get_nowait()
                self._handle_event(kind, value)
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(50, self._poll)

    def _handle_event(self, kind: str, value: object) -> None:
        if kind == "wifi_connected" and isinstance(value, WifiConnectResult):
            self._wifi_busy = False
            self.wifi_button.config(state=tk.NORMAL, text="Подключить Wi-Fi камеры")
            if value.already_connected:
                self.status_text.set(f"Wi-Fi камеры уже подключён: {value.ssid}")
            elif value.profile_created:
                self.status_text.set(
                    f"Подключено к {value.ssid}; профиль Windows создан"
                )
            else:
                self.status_text.set(f"Windows подключена к Wi-Fi камеры: {value.ssid}")
            self.root.after(250, self._connect)
        elif kind == "wifi_error":
            self._wifi_busy = False
            self.wifi_button.config(state=tk.NORMAL, text="Повторить подключение Wi-Fi")
            self.status_text.set(f"Wi-Fi камеры: {value}")
            self.connect_button.config(text="Подключиться")
        elif kind == "status":
            self.status_text.set(str(value))
        elif kind == "device_info" and isinstance(value, DeviceInfo):
            self._device_info = value
            self.device_text.set(
                f"Камера: {value.vendor} {value.product}   •   "
                f"прошивка {value.firmware}   •   батарея {value.battery_percent}%"
            )
        elif kind == "connected":
            port = value.get("port") if isinstance(value, dict) else "?"
            self.status_text.set(f"Подключено. Видеопорт Windows: UDP {port}")
            self.connect_button.config(text="Отключиться")
        elif kind == "stats" and isinstance(value, dict):
            self.stats_text.set(
                f"{value.get('fps', 0)} fps   •   {value.get('frames', 0)} кадров   •   "
                f"{value.get('megabytes', 0.0):.1f} МБ"
            )
        elif kind == "hardware_button":
            count = value.get("count", 1) if isinstance(value, dict) else 1
            event_time = (
                float(value.get("received_at", time.time()))
                if isinstance(value, dict)
                else time.time()
            )
            for _ in range(max(1, int(count))):
                self._take_photo(
                    "кнопка на камере",
                    pre_shutter_ms=self._configured_pre_shutter_ms(),
                    event_time=event_time,
                )
        elif kind == "ocr_result" and isinstance(value, dict):
            if value.get("generation") != self._ocr_generation:
                return
            text = str(value.get("text", "")).strip()
            self._set_ocr_text(text)
            filename = value.get("filename", "")
            self._set_snapshot_ocr(text, str(filename))
            if text:
                engine = value.get("engine", "OCR")
                line_count = value.get("line_count", 0)
                self.ocr_status_text.set(
                    f"{engine}: найдено строк — {line_count}   •   {filename}"
                )
                self.copy_ocr_button.config(state=tk.NORMAL)
            else:
                self.ocr_status_text.set(f"OCR: текст не найден — {filename}")
                self.copy_ocr_button.config(state=tk.DISABLED)
        elif kind == "ocr_error" and isinstance(value, dict):
            if value.get("generation") != self._ocr_generation:
                return
            self._set_ocr_text("")
            self._set_snapshot_ocr("", str(value.get("filename", "")))
            self.ocr_status_text.set(f"Ошибка OCR: {value.get('error', 'неизвестно')}")
            self.copy_ocr_button.config(state=tk.DISABLED)
        elif kind == "error":
            self.status_text.set(f"Ошибка: {value}")
            self.connect_button.config(text="Подключиться")
        elif kind == "disconnected":
            if not self._closing:
                self.status_text.set("Камера отключена")
                self.connect_button.config(text="Подключиться")

    def _transform_image(self, source: Image.Image, roll: float | None) -> Image.Image:
        image = source.convert("RGB")
        mirrored = self.mirror.get()
        if mirrored:
            image = ImageOps.mirror(image)
        if self.auto_rotate.get() and roll is not None:
            # Android's positive rotation is visually clockwise; Pillow's is CCW.
            angle = roll if mirrored else -roll
            image = image.rotate(
                angle,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(0, 0, 0),
            )
        return image

    def _render_latest(self) -> None:
        with self._frame_lock:
            serial = self._incoming_serial
            frame = self._incoming_frame

        if frame is not None and serial != self._rendered_serial:
            try:
                source = Image.open(BytesIO(frame.jpeg))
                source.load()
                self._latest_source_image = source.convert("RGB")
                self._latest_frame = frame
                self._rendered_serial = serial
                self.capture_button.config(state=tk.NORMAL)
                self._draw_image()
            except Exception as exc:
                logging.getLogger(__name__).warning("JPEG decode failed: %s", exc)

        if not self._closing:
            self.root.after(33, self._render_latest)

    def _draw_image(self) -> None:
        if self._latest_source_image is None or self._latest_frame is None:
            return
        image = self._transform_image(
            self._latest_source_image.copy(),
            self._latest_frame.roll_degrees,
        )
        canvas_width = max(self.canvas.winfo_width() - 12, 1)
        canvas_height = max(self.canvas.winfo_height() - 12, 1)
        image.thumbnail((canvas_width, canvas_height), Image.Resampling.LANCZOS)
        self._tk_image = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(
            self.canvas.winfo_width() // 2,
            self.canvas.winfo_height() // 2,
            image=self._tk_image,
            anchor="center",
        )

    def _draw_snapshot(self) -> None:
        if self._latest_snapshot_image is None:
            return
        image = self._latest_snapshot_image.copy()
        canvas_width = max(self.snapshot_canvas.winfo_width() - 12, 1)
        canvas_height = max(self.snapshot_canvas.winfo_height() - 12, 1)
        image.thumbnail((canvas_width, canvas_height), Image.Resampling.LANCZOS)
        self._snapshot_tk_image = ImageTk.PhotoImage(image)
        self.snapshot_canvas.delete("all")
        center_x = self.snapshot_canvas.winfo_width() // 2
        center_y = self.snapshot_canvas.winfo_height() // 2
        self.snapshot_canvas.create_image(
            center_x,
            center_y,
            image=self._snapshot_tk_image,
            anchor="center",
        )
        if self._snapshot_ocr_lines:
            overlay = "\n".join(self._snapshot_ocr_lines[-6:])
            right = center_x + image.width // 2 - 8
            bottom = center_y + image.height // 2 - 8
            text_id = self.snapshot_canvas.create_text(
                right,
                bottom,
                text=overlay,
                anchor="se",
                justify=tk.RIGHT,
                width=max(90, image.width - 20),
                fill="#f4fbff",
                font=("Consolas", 9, "bold"),
                tags=("snapshot-ocr",),
            )
            bounds = self.snapshot_canvas.bbox(text_id)
            if bounds is not None:
                left, top, text_right, text_bottom = bounds
                background_id = self.snapshot_canvas.create_rectangle(
                    left - 5,
                    top - 4,
                    text_right + 5,
                    text_bottom + 4,
                    fill="#071019",
                    outline="#58d68d",
                    width=1,
                    tags=("snapshot-ocr",),
                )
                self.snapshot_canvas.tag_lower(background_id, text_id)

    def _load_last_snapshot(self) -> None:
        if not CAPTURE_DIR.exists():
            return
        try:
            latest = max(CAPTURE_DIR.glob("camsorter-*.jpg"), key=lambda path: path.stat().st_mtime)
        except ValueError:
            return
        try:
            with Image.open(latest) as image:
                self._latest_snapshot_image = ImageOps.exif_transpose(image).convert("RGB")
                self._latest_snapshot_image.load()
            self._latest_snapshot_path = latest
            ocr_sidecar = latest.with_suffix(".ocr.txt")
            if ocr_sidecar.exists():
                self._snapshot_ocr_lines = [
                    line.strip()
                    for line in ocr_sidecar.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            self.root.after_idle(self._draw_snapshot)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Cannot load previous snapshot %s: %s", latest, exc
            )

    def _force_redraw(self) -> None:
        self._draw_image()
        self._draw_snapshot()

    def _keyboard_snapshot(self, _event: tk.Event) -> str:
        self._take_photo("клавиатура")
        return "break"

    def _configured_pre_shutter_ms(self) -> int:
        try:
            return max(0, min(1000, int(self.pre_shutter_ms.get())))
        except (tk.TclError, ValueError):
            self.pre_shutter_ms.set(250)
            return 250

    def _take_photo(
        self,
        source: str,
        pre_shutter_ms: int = 0,
        event_time: float | None = None,
    ) -> None:
        if self._latest_source_image is None or self._latest_frame is None:
            self.status_text.set("Снимок невозможен: первый кадр ещё не получен")
            return
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = CAPTURE_DIR / f"camsorter-{timestamp}.jpg"
        try:
            selected_source = self._latest_source_image.copy()
            selected_frame = self._latest_frame
            actual_pre_shutter_ms = 0
            if pre_shutter_ms > 0:
                button_time = event_time if event_time is not None else time.time()
                with self._frame_lock:
                    cached = choose_pre_shutter_frame(
                        tuple(self._frame_history),
                        button_time,
                        pre_shutter_ms,
                    )
                if cached is not None:
                    cached_source = Image.open(BytesIO(cached.jpeg))
                    cached_source.load()
                    selected_source = cached_source.convert("RGB")
                    selected_frame = cached
                    actual_pre_shutter_ms = max(
                        0,
                        round((button_time - cached.received_at) * 1000),
                    )
            image = self._transform_image(
                selected_source,
                selected_frame.roll_degrees,
            )
            image.save(path, "JPEG", quality=95, subsampling=0)
            self._latest_snapshot_image = image.copy()
            self._latest_snapshot_path = path
            self._snapshot_ocr_lines = []
            self._draw_snapshot()
            pre_shutter_note = (
                f", предкадр {actual_pre_shutter_ms} мс"
                if actual_pre_shutter_ms
                else ""
            )
            self.status_text.set(
                f"Фото сохранено ({source}{pre_shutter_note}): {path.name}"
            )
            self._flash_capture()
            logging.getLogger(__name__).info("Snapshot saved from %s: %s", source, path)
            self._start_ocr(image.copy(), path.name)
        except Exception as exc:
            self.status_text.set(f"Не удалось сохранить фото: {exc}")
            logging.getLogger(__name__).exception("Snapshot failed")

    def _start_ocr(self, image: Image.Image, filename: str) -> None:
        self._ocr_generation += 1
        generation = self._ocr_generation
        self._set_ocr_text("")
        self.ocr_status_text.set(f"OCR: распознавание {filename}…")
        self.copy_ocr_button.config(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = recognize_image_detailed(image)
                self._events.put(
                    (
                        "ocr_result",
                        {
                            "generation": generation,
                            "filename": filename,
                            "text": result.text,
                            "engine": result.engine,
                            "line_count": result.line_count,
                        },
                    )
                )
            except Exception as exc:
                logging.getLogger(__name__).exception("OCR failed for %s", filename)
                self._events.put(
                    (
                        "ocr_error",
                        {
                            "generation": generation,
                            "filename": filename,
                            "error": str(exc),
                        },
                    )
                )

        threading.Thread(
            target=worker,
            name=f"ocr-{generation}",
            daemon=True,
        ).start()

    def _set_ocr_text(self, text: str) -> None:
        self._ocr_text_value = text
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        self._rebuild_ocr_lines(lines)

    def _set_snapshot_ocr(self, text: str, filename: str) -> None:
        if (
            self._latest_snapshot_path is None
            or self._latest_snapshot_path.name != filename
        ):
            return
        self._snapshot_ocr_lines = [
            line.strip() for line in text.splitlines() if line.strip()
        ]
        if self._snapshot_ocr_lines:
            try:
                self._latest_snapshot_path.with_suffix(".ocr.txt").write_text(
                    "\n".join(self._snapshot_ocr_lines) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logging.getLogger(__name__).warning("Cannot save OCR sidecar: %s", exc)
        self._draw_snapshot()

    def _rebuild_ocr_lines(self, lines: list[str]) -> None:
        for child in self.ocr_lines_frame.winfo_children():
            child.destroy()

        if not lines:
            tk.Label(
                self.ocr_lines_frame,
                text="Распознанные строки появятся здесь",
                bg="#071019",
                fg="#8193a1",
                anchor="w",
                padx=5,
                pady=5,
                font=("Segoe UI", 10),
            ).pack(fill=tk.X)
            return

        for index, line in enumerate(lines, start=1):
            row = tk.Frame(self.ocr_lines_frame, bg="#071019")
            row.pack(fill=tk.X, pady=2)
            tk.Label(
                row,
                text=f"{index}.",
                width=3,
                bg="#071019",
                fg="#8193a1",
                anchor="e",
                font=("Consolas", 11),
            ).pack(side=tk.LEFT, padx=(0, 5))
            field = tk.Entry(
                row,
                bg="#0d1923",
                fg="#f1f5f8",
                readonlybackground="#0d1923",
                relief=tk.FLAT,
                font=("Consolas", 11),
            )
            field.insert(0, line)
            field.config(state="readonly")
            field.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
            ttk.Button(
                row,
                text="Копировать строку",
                style="LineCopy.TButton",
                command=lambda value=line, number=index: self._copy_ocr_line(
                    value, number
                ),
            ).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(
                row,
                text="Google",
                style="LineCopy.TButton",
                command=lambda value=line: self._search_ocr_line(value),
            ).pack(side=tk.LEFT, padx=(4, 0))

    def _copy_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _copy_ocr_text(self) -> None:
        if not self._ocr_text_value:
            return
        self._copy_to_clipboard(self._ocr_text_value)
        self.ocr_status_text.set("OCR: текст скопирован в буфер обмена")

    def _copy_ocr_line(self, text: str, line_number: int) -> None:
        self._copy_to_clipboard(text)
        self.ocr_status_text.set(
            f"OCR: строка {line_number} скопирована в буфер обмена"
        )

    def _search_ocr_line(self, text: str) -> None:
        url = f"https://www.google.com/search?q={quote_plus(text)}"
        if webbrowser.open_new_tab(url):
            self.ocr_status_text.set(f"Google: поиск «{text}» открыт в браузере")
        else:
            self.ocr_status_text.set("Не удалось открыть браузер для поиска Google")

    def _flash_capture(self) -> None:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        self.canvas.create_rectangle(
            4,
            4,
            width - 4,
            height - 4,
            outline="#58d68d",
            width=6,
            tags=("capture-flash",),
        )
        self.root.after(180, lambda: self.canvas.delete("capture-flash"))

    def _open_capture_folder(self) -> None:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(CAPTURE_DIR)  # type: ignore[attr-defined]

    def _quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.camera.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    configure_logging()
    CameraApplication().run()


if __name__ == "__main__":
    main()
