"""Capture an Archicad window as an image, and the image helpers around it (fork addition).

The Archicad 26 JSON API and Tapir have no command that renders the current view
to a picture, so the server captures the Archicad window itself: it finds the
process behind the API port (the same lookup the dialog inspection uses) and asks
Windows to paint that window into a bitmap with PrintWindow. PW_RENDERFULLCONTENT
makes Windows render the OpenGL 3D window too, and the window does not have to be
in front.

Only Win32 calls through ctypes are used for the capture; Pillow does the image
work. On other platforms capturing raises CaptureError.
"""

import io
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageStat

from tapir_archicad_mcp.dialogs import MAIN_FRAME_CLASSES, pid_for_port

log = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 2048
JPEG_QUALITY = 90
CAPTURE_DIR_ENV = "TAPIR_MCP_CAPTURE_DIR"
# A capture whose pixels barely vary is most likely a window that was not painted.
BLANK_STDDEV = 1.5


class CaptureError(Exception):
    """The window could not be captured; the message says why."""


def capture_dir() -> Path:
    """Folder that keeps the full-resolution captures (TAPIR_MCP_CAPTURE_DIR overrides it)."""
    configured = os.getenv(CAPTURE_DIR_ENV)
    folder = Path(configured) if configured else Path.home() / ".tapir_mcp" / "captures"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def timestamped_path(folder: Path, stem: str, suffix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return folder / f"{stamp}_{stem}{suffix}"


def crop_fraction(image: Image.Image, crop: list[float] | None) -> Image.Image:
    """Crop to [left, top, right, bottom] given as fractions (0-1) of the image size."""
    if crop is None:
        return image
    if len(crop) != 4 or any(not 0 <= value <= 1 for value in crop):
        raise ValueError("crop must be [left, top, right, bottom] with each value between 0 and 1.")
    left, top, right, bottom = crop
    if left >= right or top >= bottom:
        raise ValueError("crop needs left < right and top < bottom.")
    width, height = image.size
    box = (round(left * width), round(top * height), round(right * width), round(bottom * height))
    return image.crop(box)


def encode_image(image: Image.Image, max_size: int, image_format: str) -> tuple[bytes, tuple[int, int]]:
    """Scale the long side down to max_size (0 keeps the size) and encode as jpeg or png."""
    if max_size < 0:
        raise ValueError("max_size must be 0 (keep the full resolution) or a positive number of pixels.")
    if max_size and max(image.size) > max_size:
        image = image.copy()
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    if image_format == "jpeg":
        image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    elif image_format == "png":
        image.save(buffer, format="PNG", optimize=True)
    else:
        raise ValueError("image_format must be 'jpeg' or 'png'.")
    return buffer.getvalue(), image.size


def looks_blank(image: Image.Image) -> bool:
    return max(ImageStat.Stat(image.convert("L").resize((128, 128))).stddev) < BLANK_STDDEV


def _fingerprint(image: Image.Image) -> bytes:
    # Small enough to ignore a blinking cursor, large enough to see a redraw.
    return image.convert("L").resize((96, 96)).tobytes()


def capture_when_settled(
    grab: Callable[[], Image.Image],
    timeout_seconds: float,
    interval_seconds: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Image.Image, bool]:
    """Grab until two consecutive images match, so a view that is still redrawing is not returned.

    Returns the last image and whether it settled before the timeout.
    """
    deadline = clock() + max(timeout_seconds, 0)
    previous = grab()
    while clock() < deadline:
        sleep(interval_seconds)
        current = grab()
        if _fingerprint(current) == _fingerprint(previous):
            return current, True
        previous = current
    return previous, timeout_seconds <= 0


def capture_supported() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from tapir_archicad_mcp.dialogs import _top_level_windows, _window_class, _window_text

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    # Handles are pointer-sized; without explicit types ctypes truncates them to 32 bits.
    for _function, _restype, _argtypes in (
        (_user32.SetThreadDpiAwarenessContext, ctypes.c_void_p, [ctypes.c_void_p]),
        (_user32.GetWindowRect, wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
        (_user32.IsIconic, wintypes.BOOL, [wintypes.HWND]),
        (_user32.GetWindowDC, wintypes.HDC, [wintypes.HWND]),
        (_user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC]),
        (_user32.PrintWindow, wintypes.BOOL, [wintypes.HWND, wintypes.HDC, wintypes.UINT]),
        (_gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC]),
        (_gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]),
        (_gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
        (_gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ]),
        (_gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC]),
        (_gdi32.GetDIBits, ctypes.c_int, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]),
    ):
        _function.restype, _function.argtypes = _restype, _argtypes
    _PW_RENDERFULLCONTENT = 2
    _DPI_AWARENESS_PER_MONITOR_V2 = ctypes.c_void_p(-4)

    class _BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
        ]

    def main_window_for_port(port: int) -> tuple[int, str]:
        """The Archicad main frame window behind an API port, and its title."""
        pid = pid_for_port(port)
        if pid is None:
            raise CaptureError(f"No process is listening on port {port}; is Archicad still running?")
        frames = [hwnd for hwnd in _top_level_windows(pid) if _window_class(hwnd) in MAIN_FRAME_CLASSES]
        if not frames:
            raise CaptureError(f"The Archicad process on port {port} has no visible main window.")

        def area(hwnd: int) -> int:
            rect = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return (rect.right - rect.left) * (rect.bottom - rect.top)

        hwnd = max(frames, key=area)
        return hwnd, _window_text(hwnd)

    def capture_window(hwnd: int) -> Image.Image:
        """Paint one window into an image with PrintWindow, at its real pixel size."""
        if _user32.IsIconic(hwnd):
            raise CaptureError("The Archicad window is minimized. Ask the user to restore it, then capture again.")
        previous_awareness = _user32.SetThreadDpiAwarenessContext(_DPI_AWARENESS_PER_MONITOR_V2)
        window_dc = memory_dc = bitmap = old_object = None
        try:
            rect = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width <= 0 or height <= 0:
                raise CaptureError("The Archicad window has no visible area.")
            window_dc = _user32.GetWindowDC(hwnd)
            memory_dc = _gdi32.CreateCompatibleDC(window_dc)
            bitmap = _gdi32.CreateCompatibleBitmap(window_dc, width, height)
            old_object = _gdi32.SelectObject(memory_dc, bitmap)
            if not _user32.PrintWindow(hwnd, memory_dc, _PW_RENDERFULLCONTENT):
                raise CaptureError("Windows could not paint the Archicad window (PrintWindow failed).")
            header = _BitmapInfoHeader()
            header.biSize = ctypes.sizeof(_BitmapInfoHeader)
            header.biWidth, header.biHeight = width, -height  # negative: rows top to bottom
            header.biPlanes, header.biBitCount = 1, 32
            buffer = ctypes.create_string_buffer(width * height * 4)
            if not _gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
                raise CaptureError("Reading the captured pixels failed (GetDIBits).")
            return Image.frombuffer("RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1)
        finally:
            if old_object:
                _gdi32.SelectObject(memory_dc, old_object)
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if memory_dc:
                _gdi32.DeleteDC(memory_dc)
            if window_dc:
                _user32.ReleaseDC(hwnd, window_dc)
            if previous_awareness:
                _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous_awareness))

else:

    def main_window_for_port(port: int) -> tuple[int, str]:
        raise CaptureError("Capturing the Archicad window is only available on Windows.")

    def capture_window(hwnd: int) -> Image.Image:
        raise CaptureError("Capturing the Archicad window is only available on Windows.")
