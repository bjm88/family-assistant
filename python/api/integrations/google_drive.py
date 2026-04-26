"""Thin Google Drive adapter.

A *very* small surface — currently just enough to upload a single file
into a known folder, which is all the weekly DB-backup cron
(``scripts/db_backup_to_gdrive.py``) needs. The pattern mirrors
:mod:`api.integrations.gmail` and :mod:`api.integrations.google_calendar`
so future Drive features (download, share, etc.) drop in here without
touching callers.

Scope strategy
--------------
Avi authenticates with the ``drive.file`` scope (added to
``api.integrations.google_oauth.DEFAULT_SCOPES``). That grant gives
the app access ONLY to files it creates or that the user opens via a
Google Picker — Avi can put the pg_dump into the configured folder
but cannot enumerate or read anything else in the user's Drive. The
folder itself does NOT need to be created by Avi: as long as the
parent folder id is known and the OAuth account has write access to
it (either it owns the folder, or the folder was shared with edit
permission), the upload succeeds.

Folder-id parsing
-----------------
Users paste the destination folder URL straight from Google Drive's
address bar into ``.env`` (``DB_BACKUP_GDRIVE``) — e.g.
``https://drive.google.com/drive/u/1/folders/<FOLDER_ID>?usp=sharing``.
:func:`parse_folder_id` plucks the id out of the URL so callers don't
have to think about it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


logger = logging.getLogger(__name__)


class DriveError(RuntimeError):
    """Raised when a Drive API call fails (auth, scope, quota, 5xx)."""


class DriveNotConfigured(RuntimeError):
    """Raised when the destination folder id is missing or unparseable."""


@dataclass(frozen=True)
class UploadedFile:
    """Successful upload result. ``web_view_link`` is the share URL the
    Drive UI shows when you right-click a file → "Get link"."""

    file_id: str
    name: str
    mime_type: str
    size_bytes: Optional[int]
    web_view_link: Optional[str]


# Match either ``/folders/<id>`` (the canonical share URL Drive shows
# in the address bar) or ``id=<id>`` (the legacy "open by id" form).
# Folder ids are URL-safe base64-ish: letters, digits, dash, underscore.
_FOLDER_ID_PATTERNS = (
    re.compile(r"/folders/([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
)


def parse_folder_id(value: str) -> str:
    """Extract a Drive folder id from ``value``.

    Accepts either a bare folder id (returned as-is after a sanity
    check) or any of the URL shapes Google Drive emits — typically
    ``https://drive.google.com/drive/u/1/folders/<ID>?usp=sharing``.

    Raises :class:`DriveNotConfigured` if no id can be found.
    """
    raw = (value or "").strip()
    if not raw:
        raise DriveNotConfigured(
            "Drive folder reference is empty. Set DB_BACKUP_GDRIVE in .env "
            "to the destination folder's URL or id."
        )

    # If the user pasted just the id (no scheme, no slashes, looks like
    # a Drive id), accept it directly.
    if "://" not in raw and "/" not in raw and "?" not in raw:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
            raise DriveNotConfigured(
                f"Value {raw!r} is neither a Drive URL nor a valid folder id."
            )
        return raw

    parsed = urlparse(raw)
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    for pat in _FOLDER_ID_PATTERNS:
        m = pat.search(target)
        if m:
            return m.group(1)

    raise DriveNotConfigured(
        f"Could not extract a Drive folder id from {raw!r}. Expected a URL "
        "of the form https://drive.google.com/drive/folders/<FOLDER_ID>."
    )


def _service(creds: Credentials):
    """Return a Drive v3 client; cache_discovery=False keeps logs quiet."""
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(
    creds: Credentials,
    *,
    file_path: Path,
    folder_id: str,
    mime_type: str = "application/octet-stream",
    display_name: Optional[str] = None,
) -> UploadedFile:
    """Upload ``file_path`` into ``folder_id`` and return the new file's metadata.

    Parameters
    ----------
    creds:
        Authorised credentials carrying the ``drive.file`` scope.
    file_path:
        Local file to upload. Must exist; we read it via resumable
        upload so multi-MB pg_dumps don't blow up on a flaky network.
    folder_id:
        Destination Drive folder id (use :func:`parse_folder_id` if you
        only have the URL). Must be a folder Avi has write access to —
        either it owns the folder, or someone has shared it with the
        account's email with "Editor" permission.
    mime_type:
        Best-effort MIME type Google records for the uploaded file. The
        default ``application/octet-stream`` is safe for binary dumps
        (Drive will still preview text files correctly when you click
        them in the UI). For ``.dump`` files specifically Google has no
        registered type, so the octet-stream default is what we want.
    display_name:
        Optional override for the file name shown in Drive. Defaults to
        the local filename (``file_path.name``).

    Errors
    ------
    * :class:`FileNotFoundError` — local file is missing.
    * :class:`DriveError` — Google rejected the call (insufficient scope,
      folder not found, quota exhausted, transient 5xx). The wrapped
      message includes Google's payload so the operator can diagnose.
    """
    if not file_path.exists():
        raise FileNotFoundError(file_path)

    name = display_name or file_path.name
    body = {
        "name": name,
        # An empty/missing ``parents`` would drop the file at "My Drive"
        # root, which is silently confusing. Always pin to the folder.
        "parents": [folder_id],
    }
    media = MediaFileUpload(
        str(file_path),
        mimetype=mime_type,
        # resumable=True is robust against transient network errors
        # and is recommended for anything > a few MB.
        resumable=True,
    )

    try:
        svc = _service(creds)
        resp = (
            svc.files()
            .create(
                body=body,
                media_body=media,
                # Ask Google to echo back the bits we want to log.
                fields="id, name, mimeType, size, webViewLink",
                # supportsAllDrives=True lets the upload land in
                # Shared Drives too if the folder lives in one. No
                # effect on a regular My Drive folder so it's a free
                # win for forward-compatibility.
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as exc:
        raise DriveError(_summarise_http_error(exc)) from exc

    size_str = resp.get("size")
    return UploadedFile(
        file_id=resp.get("id", ""),
        name=resp.get("name", name),
        mime_type=resp.get("mimeType", mime_type),
        size_bytes=int(size_str) if size_str is not None else None,
        web_view_link=resp.get("webViewLink"),
    )


def _summarise_http_error(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", None) if exc.resp else None
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        message = payload.get("error", {}).get("message") or str(exc)
    except Exception:  # noqa: BLE001 - raw fallback
        message = str(exc)
    hint = ""
    if status == 403:
        hint = (
            " (likely missing drive.file scope — disconnect + reconnect "
            "Google on the AI Assistant settings page to re-grant)"
        )
    elif status == 404:
        hint = (
            " (folder id not found — verify DB_BACKUP_GDRIVE in .env and "
            "make sure the folder is shared with Avi's Google account)"
        )
    return f"Drive HTTP {status}: {message}{hint}"
