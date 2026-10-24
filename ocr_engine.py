"""Local multi-line OCR tuned for markings on electronic components."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
OCR_SCRIPT = PROJECT_DIR / "windows_ocr.ps1"
TEMP_DIR = PROJECT_DIR / "logs"
VENDOR_DIR = PROJECT_DIR / "vendor"

if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from PIL import Image, ImageEnhance, ImageFilter, ImageOps  # noqa: E402

try:
    import cv2  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]
    from rapidocr import RapidOCR  # type: ignore[import-not-found]
except ImportError:
    cv2 = None
    np = None
    RapidOCR = None


LOGGER = logging.getLogger(__name__)
_ENGINE = None
_ENGINE_LOCK = threading.Lock()

# RapidOCR keeps per-call overrides on the engine instance.  Pass the normal
# values explicitly on every ordinary run so a previous sensitive scan cannot
# silently make later photographs more permissive and order-dependent.
_NORMAL_RAPID_PARAMS = {
    "text_score": 0.50,
    "box_thresh": 0.50,
    "unclip_ratio": 1.60,
}
_SENSITIVE_RAPID_PARAMS = {
    "text_score": 0.40,
    "box_thresh": 0.25,
    "unclip_ratio": 1.80,
}
_RECTIFIED_RAPID_PARAMS = {
    "text_score": 0.50,
    "box_thresh": 0.50,
    # A little more padding keeps separated lot/date codes from being glued
    # together by the recognizer after perspective correction.
    "unclip_ratio": 1.80,
}


@dataclass(frozen=True)
class OcrTextResult:
    text: str
    engine: str
    line_count: int


def prepare_for_ocr(image: Image.Image) -> Image.Image:
    """Upscale and clarify small component markings for the OCR engine."""

    prepared = ImageOps.exif_transpose(image).convert("L")
    prepared = ImageOps.autocontrast(prepared, cutoff=1)
    scale = max(2, min(4, 1800 // max(prepared.width, prepared.height, 1)))
    prepared = prepared.resize(
        (prepared.width * scale, prepared.height * scale),
        Image.Resampling.LANCZOS,
    )
    prepared = ImageEnhance.Contrast(prepared).enhance(1.6)
    prepared = prepared.filter(ImageFilter.UnsharpMask(radius=1.4, percent=180, threshold=3))
    return prepared


def _recognize_windows(image: Image.Image) -> str:
    """Fallback recognition through the built-in Windows OCR runtime."""

    if os.name != "nt":
        raise RuntimeError("Windows OCR is available only on Windows")
    if not OCR_SCRIPT.exists():
        raise FileNotFoundError(OCR_SCRIPT)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="ocr-",
            suffix=".png",
            dir=TEMP_DIR,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        prepare_for_ocr(image).save(temporary_path, "PNG")

        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(OCR_SCRIPT),
                "-ImagePath",
                str(temporary_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            startupinfo=startup_info,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(message or f"Windows OCR exited with {completed.returncode}")

        lines = [line.strip() for line in completed.stdout.splitlines()]
        return "\n".join(line for line in lines if line).strip()
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _get_rapid_engine():
    global _ENGINE
    if RapidOCR is None:
        return None
    if _ENGINE is None:
        _ENGINE = RapidOCR()
    return _ENGINE


def _as_bgr(image: Image.Image):
    rgb = np.asarray(ImageOps.exif_transpose(image).convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _crop_nonblack(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    points = cv2.findNonZero((gray > 7).astype(np.uint8))
    if points is None:
        return image
    x, y, width, height = cv2.boundingRect(points)
    margin = 8
    left = max(0, x - margin)
    top = max(0, y - margin)
    right = min(image.shape[1], x + width + margin)
    bottom = min(image.shape[0], y + height + margin)
    return image[top:bottom, left:right]


def _gamma_bright_variant(image):
    cropped = _crop_nonblack(image)
    scale = max(1.5, min(2.5, 1300 / max(cropped.shape[:2])))
    enlarged = cv2.resize(
        cropped,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_CUBIC,
    )
    gamma_value = 0.65
    table = np.array(
        [
            min(255, round((index / 255.0) ** gamma_value * 255))
            for index in range(256)
        ],
        dtype=np.uint8,
    )
    return cv2.LUT(enlarged, table)


def _component_clahe_variant(image):
    """Emphasize faint laser markings in the central component work area."""

    height, width = image.shape[:2]
    # The probe is aimed at the component in the middle of the frame.  Removing
    # the bright board edges and the operator's finger gives the detector much
    # less high-contrast clutter to mistake for text.
    cropped = image[
        int(height * 0.25) : int(height * 0.88),
        int(width * 0.08) : int(width * 0.92),
    ]
    scale = max(2.5, min(3.0, 1400 / max(cropped.shape[:2])))
    enlarged = cv2.resize(
        cropped,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_CUBIC,
    )
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)


def _rectify_rotated_frame(image):
    """Remove the black diamond border produced by gyro-based UI rotation.

    The saved camera image is sometimes an expanded, rotated square.  Text
    detectors cope poorly with rows at 20-35 degrees even when the characters
    are sharp.  The four non-black corners let us map that square back to a
    level image without having to guess the chip's own outline.
    """

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    points = cv2.findNonZero((gray > 7).astype(np.uint8))
    if points is None:
        return None

    rect = cv2.minAreaRect(points)
    width, height = rect[1]
    if min(width, height) < min(image.shape[:2]) * 0.50:
        return None

    angle = float(rect[2])
    tilt = abs(90.0 + angle) if angle < -45.0 else abs(angle)
    if tilt < 5.0:
        return None

    box = cv2.boxPoints(rect).astype(np.float32)
    sums = box.sum(axis=1)
    differences = np.diff(box, axis=1).ravel()
    source_points = np.array(
        [
            box[np.argmin(sums)],
            box[np.argmin(differences)],
            box[np.argmax(sums)],
            box[np.argmax(differences)],
        ],
        dtype=np.float32,
    )
    side = int(
        max(
            np.linalg.norm(source_points[(index + 1) % 4] - source_points[index])
            for index in range(4)
        )
    )
    if side < 32:
        return None

    destination_points = np.array(
        [[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(source_points, destination_points)
    rectified = cv2.warpPerspective(image, matrix, (side, side))
    scale = max(1.5, min(2.5, 1300 / max(rectified.shape[:2])))
    return cv2.resize(
        rectified,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_CUBIC,
    )


def _clean_rapid_lines(
    output,
    minimum_score: float = 0.60,
) -> list[tuple[str, float]]:
    cleaned: list[tuple[str, float]] = []
    for text, score in zip(output.txts or (), output.scores or ()):
        line = re.sub(r"[^A-Za-z0-9.+_/#-]+", " ", str(text)).strip()
        normalized = re.sub(r"[^A-Za-z0-9]", "", line)
        numeric_score = float(score)
        if len(normalized) < 3 or numeric_score < minimum_score:
            continue
        cleaned.append((line, numeric_score))
    return cleaned


def _line_set_quality(lines: list[tuple[str, float]]) -> float:
    if not lines:
        return 0.0
    characters = sum(len(re.sub(r"\W", "", text)) for text, _ in lines)
    return len(lines) * 2.0 + sum(score for _, score in lines) + min(1.5, characters / 24)


def _average_score(lines: list[tuple[str, float]]) -> float:
    if not lines:
        return 0.0
    return sum(score for _, score in lines) / len(lines)


def _recognize_rapid(image: Image.Image) -> OcrTextResult | None:
    if RapidOCR is None or cv2 is None or np is None:
        return None
    source = _as_bgr(image)
    with _ENGINE_LOCK:
        engine = _get_rapid_engine()
        raw_lines = _clean_rapid_lines(engine(source, **_NORMAL_RAPID_PARAMS))
        candidates = [("raw", raw_lines)]
        average = (
            sum(score for _, score in raw_lines) / len(raw_lines)
            if raw_lines
            else 0.0
        )
        if len(raw_lines) < 2 or average < 0.85:
            enhanced = _gamma_bright_variant(source)
            enhanced_lines = _clean_rapid_lines(
                engine(enhanced, **_NORMAL_RAPID_PARAMS)
            )
            candidates.append(("gamma", enhanced_lines))
            # A low detector threshold recovers faint second/third chip rows,
            # but also sees board artwork as text.  Run it only when the raw
            # frame found nothing, and retain only confident non-trivial rows.
            if not raw_lines:
                relaxed_output = engine(enhanced, **_SENSITIVE_RAPID_PARAMS)
                candidates.append(
                    (
                        "gamma-sensitive",
                        _clean_rapid_lines(relaxed_output, minimum_score=0.75),
                    )
                )

            component_clahe = _component_clahe_variant(source)
            component_lines = _clean_rapid_lines(
                engine(component_clahe, **_SENSITIVE_RAPID_PARAMS),
                minimum_score=0.58,
            )
            candidates.append(("component-clahe", component_lines))

        # The application's gyro rotation creates black triangular borders.
        # Undoing that rotation gives the detector genuinely horizontal rows.
        rectified = _rectify_rotated_frame(source)
        rectified_lines: list[tuple[str, float]] = []
        if rectified is not None:
            rectified_lines = _clean_rapid_lines(
                engine(rectified, **_RECTIFIED_RAPID_PARAMS)
            )
            candidates.append(("rectified", rectified_lines))

    variant, lines = max(candidates, key=lambda item: _line_set_quality(item[1]))
    # Several low-confidence fragments can outscore two clean physical rows
    # merely because the old quality metric rewards row count.  Prefer a
    # confidently rectified multi-line result when it is materially clearer.
    if rectified_lines:
        rectified_average = _average_score(rectified_lines)
        selected_average = _average_score(lines)
        if (
            len(rectified_lines) >= 2
            and rectified_average >= 0.90
            and (len(lines) < 2 or rectified_average >= selected_average + 0.02)
        ):
            variant, lines = "rectified", rectified_lines
    if not lines:
        return None
    LOGGER.info("RapidOCR variant=%s lines=%s", variant, lines)
    return OcrTextResult(
        text="\n".join(text for text, _ in lines),
        engine=f"RapidOCR PP-OCRv6 ({variant})",
        line_count=len(lines),
    )


def recognize_image_detailed(image: Image.Image) -> OcrTextResult:
    """Recognize component markings, preserving one detected row per line."""

    rapid_result = _recognize_rapid(image)
    if rapid_result is not None:
        return rapid_result

    windows_text = _recognize_windows(image)
    lines = [line.strip() for line in windows_text.splitlines() if line.strip()]
    return OcrTextResult(
        text="\n".join(lines),
        engine="Windows OCR (fallback)",
        line_count=len(lines),
    )


def recognize_image(image: Image.Image) -> str:
    """Compatibility wrapper returning only the recognized plain text."""

    return recognize_image_detailed(image).text
