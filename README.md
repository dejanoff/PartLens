# PartLens

PartLens is a Windows desktop application for inspecting and identifying
electronic components with a Soulear/i4season Wi-Fi inspection camera. It
combines a live camera viewer, physical-shutter support, local multi-line OCR,
and one-click web search for chip markings.

The application was developed and tested with this device:

```text
Vendor:   YPC
Model:    BK7231U-XRH-FBPRO
Firmware: HKV41B
SSID:     Soulear-c80fc
Address:  192.168.1.1
Video:    640x480 JPEG over UDP
```

Other cameras using the same protocol may work, but have not yet been tested.

## Features

- automatic discovery and connection to open Wi-Fi networks whose names begin
  with `Soulear` or `Soliar`, regardless of the camera-specific suffix;
- live video with optional gyro-based rotation and mirroring;
- physical camera shutter support through the UDP event channel;
- an adjustable RAM pre-shutter buffer that can save a sharp frame from
  0–1000 ms before a stiff physical-button press;
- side-by-side live view and most recent photograph;
- fully local multi-line OCR powered by RapidOCR PP-OCRv6;
- preprocessing for faint laser markings, rotated frames, low contrast, and
  dark component packages;
- per-line copy and Google Search buttons, plus copy-all;
- recognized text overlay on the latest photograph;
- unique JPEG names and `.ocr.txt` sidecars in `captures`;
- device model, firmware, battery, frame rate, and traffic statistics.

Images and OCR results are processed locally. PartLens does not upload camera
frames to a cloud OCR service.

## Requirements

- Windows 10 or Windows 11;
- Python 3.11 or newer (Python 3.13 is used in CI);
- a supported Soulear/i4season camera;
- a Wi-Fi adapter for the camera access point;
- optionally, Ethernet or a second network connection for simultaneous
  internet access.

## Installation

Clone the repository and create a virtual environment:

```powershell
git clone https://github.com/dejanoff/PartLens.git
cd PartLens
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start PartLens by double-clicking `Start_PartLens.bat`, or run:

```powershell
.\.venv\Scripts\python.exe .\partlens.py
```

On first use, PartLens may create a normal current-user Windows profile for the
camera's passwordless Wi-Fi network. It does not remove or modify profiles for
other networks. If the camera is still booting, use the retry camera Wi-Fi
button in the application after its SSID appears.

## Taking photographs

- Use the large on-screen button, `Space`, or `S` for an immediate frame.
- Use the physical camera button to apply the configured pre-shutter delay.
- Start with 250 ms. Try 400–450 ms if the mechanical press still causes blur.
- Set the delay to 0 ms to disable pre-shutter selection.

The buffer contains compressed JPEG frames only in RAM. Temporary frames are
not written to disk.

## Command-line single-frame capture

With Windows already connected to the camera Wi-Fi:

```powershell
python .\camera_capture.py
```

An explicit output path can also be supplied:

```powershell
python .\camera_capture.py --output .\captures\chip-test.jpg
```

Existing files are not overwritten.

## Protocol notes

- control commands use UDP ports `10005` and `10006`;
- the physical controls use UDP port `10007`;
- JPEG video is returned to a negotiated local UDP port;
- command `9` carries the 17-byte `eventSetEar` payload;
- the low nibble of the word at payload offset `5` is the wrapping physical
  picture counter (`15 -> 0`).

The native implementation is in `soulear_camera.py` and contains no vendor
binaries. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for protocol
research references and dependency acknowledgements.

## Development

Run the complete test suite with:

```powershell
python -m unittest -v
```

Real-device captures are intentionally excluded from Git. OCR regression tests
using private local captures skip automatically when those files are absent.

## Privacy and repository contents

The public repository intentionally excludes:

- photographs and OCR sidecars from `captures`;
- logs;
- the locally installed `vendor` directory and OCR model copies;
- Android APKs and reverse-engineering artifacts;
- local copies of third-party repositories.

## License

PartLens is licensed under the GNU General Public License v3.0. See
[LICENSE](LICENSE).
