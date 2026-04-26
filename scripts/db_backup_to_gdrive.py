#!/usr/bin/env python3
"""Run the weekly DB backup and upload it to Google Drive.

Glue between the existing ``scripts/db_backup.sh`` (which writes a
timestamped ``.dump`` file under ``backups/``) and Avi's already-
authorised Google account. Designed to be run from a macOS
LaunchAgent (``com.familyassistant.dbbackup``) every Sunday at
23:55 local time, but it's a normal CLI you can fire ad-hoc::

    uv run python scripts/db_backup_to_gdrive.py
    uv run python scripts/db_backup_to_gdrive.py --keep 8
    uv run python scripts/db_backup_to_gdrive.py --skip-upload    # dump only

Behaviour
---------
1. Invokes ``scripts/db_backup.sh`` (forwarding ``--keep`` if given).
   That step is the source of truth for HOW we dump — pg_dump flags,
   filename layout, sidecar ``.meta`` checksum, retention pruning all
   live there.
2. Locates the most recent ``.dump`` file written by that step (the
   shell script doesn't print the path in a machine-readable way; we
   just sort ``backups/*.dump`` by mtime, which is robust against the
   shell script gaining/losing log lines later).
3. Resolves the destination Drive folder id from the
   ``DB_BACKUP_GDRIVE`` setting and finds the assistant whose Google
   credentials carry the ``drive.file`` scope.
4. Uploads the dump (and its ``.meta`` sidecar) into that folder.
5. Prints a one-paragraph summary and exits 0 on success, non-zero
   on any step's failure so launchd / make / a human shell loop can
   notice.

Why a Python script instead of bash + ``rclone``
-------------------------------------------------
We already encrypt-and-store Avi's OAuth refresh token in Postgres
and have a battle-tested ``load_credentials`` helper that auto-
refreshes the access token on demand. Reusing it means:

* zero extra credentials to manage / rotate / leak,
* the same Google project / consent screen the rest of the
  integration uses,
* the upload runs as the assistant ("Avi"), so the file shows up
  in the destination folder with that account as the owner — easy
  for the household admin to inspect.

Re-consent reminder
-------------------
The first time you run this after pulling the change that adds
``drive.file`` to ``DEFAULT_SCOPES``, the upload step will fail
with a 403 ``insufficient_scope`` because the existing OAuth grant
doesn't include Drive yet. Open the AI Assistant settings page,
click *Disconnect Google*, then *Connect with Google* again so the
new scope is requested at the consent screen. After that one-time
re-grant the script runs unattended.
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Bootstrapping — make ``api.*`` importable without a console_scripts entry.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_PKG_ROOT = REPO_ROOT / "python"
if str(PYTHON_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_PKG_ROOT))


# Imports below depend on the sys.path tweak above.
from api import models  # noqa: E402,F401  (ensure model registry is populated)
from api.config import get_settings  # noqa: E402
from api.db import SessionLocal  # noqa: E402
from api.integrations import google_drive, google_oauth  # noqa: E402


logger = logging.getLogger("db_backup_to_gdrive")


# ---------------------------------------------------------------------------
# Step 1 — dump
# ---------------------------------------------------------------------------


def run_pg_dump(*, keep: Optional[int]) -> Path:
    """Invoke ``scripts/db_backup.sh`` and return the path it produced.

    We don't try to parse the shell script's stdout; instead we
    snapshot the set of dumps before the call and pick the one that
    appeared after. That's robust to the shell script's logging
    changing over time.
    """
    backups_dir = REPO_ROOT / "backups"
    backups_dir.mkdir(exist_ok=True)
    before = {p.name for p in backups_dir.glob("*.dump")}

    cmd = [str(REPO_ROOT / "scripts" / "db_backup.sh")]
    if keep is not None:
        cmd.extend(["--keep", str(keep)])

    logger.info("Running %s", " ".join(cmd))
    # Inherit stdout/stderr so the LaunchAgent log captures the full
    # pg_dump banner — useful when something goes wrong.
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"scripts/db_backup.sh exited with code {result.returncode}"
        )

    after = {p.name for p in backups_dir.glob("*.dump")}
    new_dumps = sorted(after - before)
    if new_dumps:
        # Exactly one new dump per invocation in normal operation; if
        # somehow more exist take the newest by mtime.
        chosen = max(
            (backups_dir / n for n in new_dumps),
            key=lambda p: p.stat().st_mtime,
        )
    else:
        # Fallback: pruning may have trimmed the new file's siblings
        # such that ``after - before`` is empty (e.g. KEEP=1 deletes
        # the older one). In that case the newest mtime in the dir
        # is still the file we just wrote.
        existing = sorted(
            backups_dir.glob("*.dump"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not existing:
            raise RuntimeError(
                "scripts/db_backup.sh succeeded but no .dump file is present "
                f"under {backups_dir}."
            )
        chosen = existing[0]

    logger.info("Backup file: %s (%d bytes)", chosen, chosen.stat().st_size)
    return chosen


# ---------------------------------------------------------------------------
# Step 2 — pick the assistant whose Google account we'll upload as.
# ---------------------------------------------------------------------------


def _has_drive_scope(scopes_str: str) -> bool:
    return any(
        s.endswith("/drive.file") or s.endswith("/drive")
        for s in (scopes_str or "").split()
    )


def find_uploader_assistant_id(db) -> int:
    """Return the assistant_id whose stored creds can write to Drive.

    Uses the first row that carries the ``drive.file`` scope (or the
    broader ``drive`` scope, in case someone manually requested it).
    Most installs have a single assistant, so this is unambiguous; if
    you ever run multiple assistants and only want one of them to be
    the backup uploader, pin it via the ``--assistant-id`` CLI flag.
    """
    rows = (
        db.query(models.GoogleOAuthCredential)
        .order_by(models.GoogleOAuthCredential.assistant_id.asc())
        .all()
    )
    if not rows:
        raise RuntimeError(
            "No Google credentials are stored. Connect Avi to a Google "
            "account on the AI Assistant settings page first."
        )
    for row in rows:
        if _has_drive_scope(row.scopes or ""):
            return row.assistant_id
    raise RuntimeError(
        "None of the connected Google accounts hold the 'drive.file' scope. "
        "On the AI Assistant settings page click 'Disconnect Google' then "
        "'Connect with Google' again so the new scope is granted."
    )


# ---------------------------------------------------------------------------
# Step 3 — upload
# ---------------------------------------------------------------------------


def upload_dump(
    *,
    dump_path: Path,
    folder_id: str,
    assistant_id: int,
) -> google_drive.UploadedFile:
    """Push ``dump_path`` (and its ``.meta`` sidecar if present) to Drive."""
    with SessionLocal() as db:
        _row, creds = google_oauth.load_credentials(db, assistant_id)
        # Persist token-refresh side effect right away so we don't pay
        # the same refresh on the very next run.
        db.commit()

        mime, _ = mimetypes.guess_type(dump_path.name)
        uploaded = google_drive.upload_file(
            creds,
            file_path=dump_path,
            folder_id=folder_id,
            mime_type=mime or "application/octet-stream",
        )
        logger.info(
            "Uploaded dump → file_id=%s name=%s size=%s link=%s",
            uploaded.file_id,
            uploaded.name,
            uploaded.size_bytes,
            uploaded.web_view_link,
        )

        meta_path = dump_path.with_suffix(dump_path.suffix + ".meta")
        if meta_path.exists():
            meta_uploaded = google_drive.upload_file(
                creds,
                file_path=meta_path,
                folder_id=folder_id,
                mime_type="text/plain",
            )
            logger.info(
                "Uploaded meta → file_id=%s name=%s",
                meta_uploaded.file_id,
                meta_uploaded.name,
            )

    return uploaded


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run scripts/db_backup.sh and upload the resulting dump to the "
            "Google Drive folder configured via DB_BACKUP_GDRIVE."
        )
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help=(
            "Forwarded to db_backup.sh — keep only the newest N dumps in "
            "backups/ after this run. Useful so the local dir doesn't "
            "grow unbounded once Drive is the long-term home."
        ),
    )
    parser.add_argument(
        "--assistant-id",
        type=int,
        default=None,
        help=(
            "Pin which assistant's Google account to upload as. Defaults "
            "to the first assistant whose stored credentials carry the "
            "drive.file scope."
        ),
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help=(
            "Run the local dump but do not upload to Drive. Handy for "
            "verifying the cron without burning Drive quota."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging for our modules.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    settings = get_settings()

    folder_ref = (settings.DB_BACKUP_GDRIVE or "").strip()
    folder_id: Optional[str] = None
    if not args.skip_upload:
        if not folder_ref:
            logger.error(
                "DB_BACKUP_GDRIVE is not set in .env; either set it to the "
                "destination folder URL or pass --skip-upload."
            )
            return 2
        try:
            folder_id = google_drive.parse_folder_id(folder_ref)
        except google_drive.DriveNotConfigured as exc:
            logger.error("Bad DB_BACKUP_GDRIVE value: %s", exc)
            return 2
        logger.info("Drive destination folder id: %s", folder_id)

    try:
        dump_path = run_pg_dump(keep=args.keep)
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        logger.error("Backup step failed: %s", exc)
        return 3

    if args.skip_upload:
        logger.info("--skip-upload set; leaving %s on local disk only.", dump_path)
        return 0

    try:
        with SessionLocal() as db:
            assistant_id = args.assistant_id or find_uploader_assistant_id(db)
        logger.info("Uploading as assistant_id=%s", assistant_id)
        upload_dump(
            dump_path=dump_path,
            folder_id=folder_id,  # type: ignore[arg-type]
            assistant_id=assistant_id,
        )
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        logger.error("Upload step failed: %s", exc)
        return 4

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
