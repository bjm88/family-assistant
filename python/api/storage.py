"""Local filesystem storage for photos and documents.

Layout::

    <FA_STORAGE_ROOT>/
        family_<family_id>/
            people/
                person_<person_id>/
                    profile/<uuid>.jpg
                    documents/<uuid>.<ext>
            documents/<uuid>.<ext>

The DB stores the path *relative to* ``FA_STORAGE_ROOT`` so the whole family
directory can be relocated or synced to a different machine without breaking
any links.
"""

from __future__ import annotations

import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Tuple

from .config import get_settings


def _ext_from_filename(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix if suffix else ""


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_profile_photo(
    family_id: int, person_id: int, upload_file: BinaryIO, original_filename: str
) -> Tuple[str, int]:
    """Persist a profile photo and return ``(relative_path, bytes_written)``."""
    root = get_settings().storage_root
    dest_dir = _ensure_dir(
        root / f"family_{family_id}" / "people" / f"person_{person_id}" / "profile"
    )
    ext = _ext_from_filename(original_filename) or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / filename
    with open(dest_path, "wb") as out:
        shutil.copyfileobj(upload_file, out)
    size = dest_path.stat().st_size
    return str(dest_path.relative_to(root)), size


def save_assistant_avatar(
    family_id: int, image_bytes: bytes, extension: str = ".png"
) -> Tuple[str, int]:
    """Save a freshly generated assistant avatar to disk.

    Returns ``(relative_path, bytes_written)``.
    """
    root = get_settings().storage_root
    dest_dir = _ensure_dir(root / f"family_{family_id}" / "assistant")
    filename = f"{uuid.uuid4().hex}{extension or '.png'}"
    dest_path = dest_dir / filename
    with open(dest_path, "wb") as out:
        out.write(image_bytes)
    return str(dest_path.relative_to(root)), len(image_bytes)


def save_person_photo(
    family_id: int,
    person_id: int,
    upload_file: BinaryIO,
    original_filename: str,
) -> Tuple[str, int, str]:
    """Persist an extra person photo. Returns ``(relative_path, size, mime)``."""
    root = get_settings().storage_root
    dest_dir = _ensure_dir(
        root / f"family_{family_id}" / "people" / f"person_{person_id}" / "photos"
    )
    ext = _ext_from_filename(original_filename) or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / filename
    with open(dest_path, "wb") as out:
        shutil.copyfileobj(upload_file, out)
    size = dest_path.stat().st_size
    mime, _ = mimetypes.guess_type(original_filename)
    return str(dest_path.relative_to(root)), size, mime or "image/jpeg"


def save_pet_photo(
    family_id: int,
    pet_id: int,
    upload_file: BinaryIO,
    original_filename: str,
) -> Tuple[str, int, str]:
    """Persist an extra pet photo. Returns ``(relative_path, size, mime)``."""
    root = get_settings().storage_root
    dest_dir = _ensure_dir(
        root / f"family_{family_id}" / "pets" / f"pet_{pet_id}" / "photos"
    )
    ext = _ext_from_filename(original_filename) or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / filename
    with open(dest_path, "wb") as out:
        shutil.copyfileobj(upload_file, out)
    size = dest_path.stat().st_size
    mime, _ = mimetypes.guess_type(original_filename)
    return str(dest_path.relative_to(root)), size, mime or "image/jpeg"


def save_identity_document_image(
    family_id: int,
    person_id: int,
    identity_document_id: int,
    side: str,
    upload_file: BinaryIO,
    original_filename: str,
) -> Tuple[str, int, str]:
    """Persist a front/back scan of an identity document.

    ``side`` should be ``"front"`` or ``"back"``. Files land in
    ``family_<id>/people/person_<id>/identity_documents/<doc_id>/``. Returns
    ``(relative_path, size_bytes, mime_type)``.
    """
    if side not in ("front", "back"):
        raise ValueError("side must be 'front' or 'back'")
    root = get_settings().storage_root
    dest_dir = _ensure_dir(
        root
        / f"family_{family_id}"
        / "people"
        / f"person_{person_id}"
        / "identity_documents"
        / str(identity_document_id)
    )
    ext = _ext_from_filename(original_filename) or ".jpg"
    filename = f"{side}_{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / filename
    with open(dest_path, "wb") as out:
        shutil.copyfileobj(upload_file, out)
    size = dest_path.stat().st_size
    mime, _ = mimetypes.guess_type(original_filename)
    return str(dest_path.relative_to(root)), size, mime or "image/jpeg"


def save_document(
    family_id: int,
    person_id: int | None,
    upload_file: BinaryIO,
    original_filename: str,
) -> Tuple[str, int, str]:
    """Persist a generic document. Returns ``(relative_path, size, mime_type)``."""
    root = get_settings().storage_root
    base = root / f"family_{family_id}"
    if person_id is not None:
        dest_dir = base / "people" / f"person_{person_id}" / "documents"
    else:
        dest_dir = base / "documents"
    _ensure_dir(dest_dir)
    ext = _ext_from_filename(original_filename)
    filename = f"{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / filename
    with open(dest_path, "wb") as out:
        shutil.copyfileobj(upload_file, out)
    size = dest_path.stat().st_size
    mime, _ = mimetypes.guess_type(original_filename)
    return str(dest_path.relative_to(root)), size, mime or "application/octet-stream"


def absolute_path(relative_path: str) -> Path:
    root = get_settings().storage_root
    p = (root / relative_path).resolve()
    if root not in p.parents and p != root:
        raise ValueError("Refusing to serve path outside of FA_STORAGE_ROOT.")
    return p


def delete_if_exists(relative_path: str | None) -> None:
    if not relative_path:
        return
    try:
        absolute_path(relative_path).unlink(missing_ok=True)
    except ValueError:
        pass
