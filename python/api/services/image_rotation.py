"""Turn a stored image by a quarter, half or three-quarter turn.

Scans and phone photos often arrive sideways or upside down. The admin
UI lets someone turn the picture until it looks right and save it;
this module does the turning on disk.

The picture is rotated as it is *seen*: a phone photo that carries an
"I was held sideways" orientation note is first stood upright the way
every browser shows it, and the requested turn is applied to that. The
note is then removed, so the saved file looks the same everywhere,
including in programs that ignore such notes.

The rotated picture is written as a **new file** next to the old one
and the caller points the record at it, then deletes the old file.
Because the path changes, no browser or phone keeps showing a cached
copy of the unrotated picture.

The file keeps its format. PNG and other lossless formats come out
pixel-for-pixel identical, only turned. A JPEG has to be re-encoded,
which is done at high quality with the original's colour sampling;
one save costs one re-encode however many quarter turns it covers.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, JpegImagePlugin, UnidentifiedImageError

from .. import storage
from ..config import get_settings


logger = logging.getLogger(__name__)


class ImageRotationError(Exception):
    """The image can't be rotated; the message says why, in plain words."""


@dataclass(frozen=True)
class RotatedImage:
    relative_path: str  # the new file, relative to FA_STORAGE_ROOT
    size_bytes: int
    width: int
    height: int


# Clockwise degrees -> Pillow's transpose, which counts counter-clockwise.
_TURNS = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}

# Formats written back as themselves. MPO is what some phones call a
# JPEG with a second embedded picture; the main picture is a JPEG.
_SAVE_AS = {
    "JPEG": "JPEG",
    "MPO": "JPEG",
    "PNG": "PNG",
    "WEBP": "WEBP",
    "GIF": "GIF",
    "BMP": "BMP",
    "TIFF": "TIFF",
}
_DEFAULT_SUFFIX = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tif",
}

_EXIF_ORIENTATION = 0x0112
_HEX_ID = re.compile(r"[0-9a-f]{32}$")


def normalise_degrees(degrees: int) -> int:
    """Clockwise degrees as 90, 180 or 270. Raises for anything else."""
    turn = int(degrees) % 360
    if turn not in _TURNS:
        raise ImageRotationError(
            "Images can be rotated by 90, 180 or 270 degrees."
        )
    return turn


def _new_name(old: Path, suffix: str) -> str:
    """A fresh file name that keeps any prefix (``front_…``) the old one had."""
    prefix = _HEX_ID.sub("", old.stem)
    if prefix == old.stem:  # no id to replace: not one of our generated names
        prefix = ""
    return f"{prefix}{uuid.uuid4().hex}{suffix}"


def _save_options(source: Image.Image, turned: Image.Image, fmt: str) -> dict:
    options: dict = {}
    icc = source.info.get("icc_profile")
    if icc and fmt in ("JPEG", "PNG", "WEBP", "TIFF"):
        options["icc_profile"] = icc
    if fmt in ("JPEG", "PNG", "WEBP"):
        # Keep when and where the photo was taken, minus the orientation
        # note, which no longer applies.
        exif = turned.getexif()
        exif.pop(_EXIF_ORIENTATION, None)
        if len(exif):
            options["exif"] = exif.tobytes()
    dpi = source.info.get("dpi")
    if dpi and fmt in ("JPEG", "PNG", "TIFF"):
        options["dpi"] = dpi
    if fmt == "JPEG":
        options["quality"] = 95
        options["optimize"] = True
        if source.format in ("JPEG", "MPO"):
            sampling = JpegImagePlugin.get_sampling(source)
            if sampling in (0, 1, 2):
                options["subsampling"] = sampling
            if source.info.get("progressive") or source.info.get("progression"):
                options["progressive"] = True
    elif fmt == "WEBP":
        options["quality"] = 95
    return options


def rotate_stored_image(relative_path: str, degrees: int) -> RotatedImage:
    """Write a copy of the stored image turned ``degrees`` clockwise.

    Returns the new file. The original is left in place; the caller
    deletes it once the record points at the new one.
    """
    turn = normalise_degrees(degrees)
    try:
        source_path = storage.absolute_path(relative_path)
    except ValueError as exc:
        raise ImageRotationError("That file is not in storage.") from exc
    if not source_path.is_file():
        raise ImageRotationError("The image file is missing from storage.")

    try:
        with Image.open(source_path) as source:
            fmt = _SAVE_AS.get(source.format or "")
            if fmt is None:
                raise ImageRotationError(
                    f"{source.format or 'This kind of'} images can't be rotated "
                    "here. Convert it to a JPEG or PNG and upload it again."
                )
            if source.format != "MPO" and getattr(source, "n_frames", 1) > 1:
                raise ImageRotationError("Animated images can't be rotated.")
            upright = ImageOps.exif_transpose(source)
            turned = upright.transpose(_TURNS[turn])
            options = _save_options(source, turned, fmt)

            suffix = source_path.suffix or _DEFAULT_SUFFIX[fmt]
            dest_path = source_path.with_name(_new_name(source_path, suffix))
            temp_path = dest_path.with_name(f".{dest_path.name}.part")
            try:
                turned.save(temp_path, format=fmt, **options)
                os.replace(temp_path, dest_path)
            finally:
                temp_path.unlink(missing_ok=True)
            width, height = turned.size
    except ImageRotationError:
        raise
    except UnidentifiedImageError as exc:
        raise ImageRotationError(
            "That file isn't an image this server can read, so it can't be "
            "rotated."
        ) from exc
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        logger.warning("Rotating %s failed: %s", relative_path, exc)
        raise ImageRotationError("The image couldn't be rotated.") from exc

    root = get_settings().storage_root
    new_relative = str(dest_path.relative_to(root.resolve()))
    logger.info(
        "Rotated image %s by %d° -> %s (%dx%d)",
        relative_path,
        turn,
        new_relative,
        width,
        height,
    )
    return RotatedImage(
        relative_path=new_relative,
        size_bytes=dest_path.stat().st_size,
        width=width,
        height=height,
    )


__all__ = [
    "ImageRotationError",
    "RotatedImage",
    "normalise_degrees",
    "rotate_stored_image",
]
