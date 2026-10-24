"""Image helpers for the tools that show Archicad's views (fork addition).

The view images themselves come from Archicad: Tapir 1.7.2's SaveCurrentWindowAsImage
writes the current window to a file, as File > Save As with a picture format does.
Archicad draws that image itself, so it does not depend on how the window looks on
screen (the server used to capture the window with PrintWindow, which returned a grey
canvas whenever Archicad had not painted the window). A screenshot of the whole
desktop is better taken with an OS-level tool.
"""

import io
import logging
import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageStat

log = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 2048
JPEG_QUALITY = 90
CAPTURE_DIR_ENV = "TAPIR_MCP_CAPTURE_DIR"
# An image whose pixels barely vary is most likely a view with nothing drawn in it.
BLANK_STDDEV = 1.5


def capture_dir() -> Path:
    """Folder that keeps the full-resolution images (TAPIR_MCP_CAPTURE_DIR overrides it)."""
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
