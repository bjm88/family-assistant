#!/usr/bin/env python3
"""Run the weekly DB + media backup and upload it to Google Drive.

Glue between the existing ``scripts/db_backup.sh`` (which writes a
timestamped ``.dump`` plus a ``.media.tar.gz`` under ``backups/``) and
Avi's already-authorised Google account. The macOS LaunchAgent
(``com.familyassistant.dbbackup``) fires it at login and then hourly
with ``--if-due-days 7``, so a week missed while the Mac was asleep or
powered off is caught up on the next run. It's a normal CLI too::

    uv run python scripts/db_backup_to_gdrive.py
    uv run python scripts/db_backup_to_gdrive.py --keep 8
    uv run python scripts/db_backup_to_gdrive.py --skip-upload    # dump only
    uv run python scripts/db_backup_to_gdrive.py --if-due-days 7  # scheduler mode

Two off-machine copies, independent of each other:

* **Sync folder** — the dump, ``.meta`` and media archive are copied into
  ``settings.backup_sync_dir`` (Google Drive for desktop's "My Drive" by
  default). The desktop client uploads them with its own sign-in, so
  this works even when Avi's Google OAuth has lapsed.
* **Drive API upload** — as Avi, into ``DB_BACKUP_GDRIVE``. A success
  leaves a ``<dump>.uploaded`` marker.

In scheduler mode a backup that isn't due yet still fills in whichever
copy the newest dump is missing (e.g. Google was disconnected that week,
or Drive for desktop was installed since).

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
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
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

BACKUPS_DIR = REPO_ROOT / "backups"


def media_path_for(dump_path: Path) -> Path:
    return dump_path.with_name(dump_path.name[: -len(".dump")] + ".media.tar.gz")


def uploaded_marker_for(dump_path: Path) -> Path:
    return dump_path.with_suffix(dump_path.suffix + ".uploaded")


def newest_dump() -> Optional[Path]:
    dumps = list(BACKUPS_DIR.glob("*.dump"))
    return max(dumps, key=lambda p: p.stat().st_mtime) if dumps else None


def backup_set(dump_path: Path) -> list[Path]:
    """The dump plus whichever sidecars exist next to it."""
    candidates = [
        dump_path,
        dump_path.with_suffix(dump_path.suffix + ".meta"),
        media_path_for(dump_path),
    ]
    return [p for p in candidates if p.exists()]


def mirror_to_sync_dir(dump_path: Path, *, keep: int) -> Optional[Path]:
    """Copy one backup set into the sync folder; prune it to ``keep`` sets.

    Returns the folder, or None when no sync folder is configured or
    detected. Files land under a ``.partial`` name and are renamed when
    complete so the sync client never uploads a half-written archive.
    """
    target = get_settings().backup_sync_dir
    if target is None:
        return None
    target.mkdir(parents=True, exist_ok=True)

    for src in backup_set(dump_path):
        dest = target / src.name
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            continue
        partial = dest.with_name(dest.name + ".partial")
        shutil.copy2(src, partial)
        os.replace(partial, dest)
        logger.info("Copied %s → %s", src.name, target)

    stale = sorted(
        target.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True
    )[keep:]
    for old in stale:
        for f in (old, old.with_suffix(".dump.meta"), media_path_for(old)):
            f.unlink(missing_ok=True)
        logger.info("Pruned %s from %s", old.name, target)
    return target


def is_mirrored(dump_path: Path) -> bool:
    target = get_settings().backup_sync_dir
    return target is not None and all(
        (target / p.name).exists() for p in backup_set(dump_path)
    )


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
    backups_dir = BACKUPS_DIR
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
    """Push ``dump_path`` plus its ``.meta`` and media archive to Drive."""
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

        media_path = media_path_for(dump_path)
        if media_path.exists():
            media_uploaded = google_drive.upload_file(
                creds,
                file_path=media_path,
                folder_id=folder_id,
                mime_type="application/gzip",
            )
            logger.info(
                "Uploaded media → file_id=%s name=%s size=%s",
                media_uploaded.file_id,
                media_uploaded.name,
                media_uploaded.size_bytes,
            )
        else:
            logger.warning("No media archive next to %s; uploaded DB only.", dump_path.name)

    uploaded_marker_for(dump_path).write_text(
        f"{datetime.now(timezone.utc).isoformat()} file_id={uploaded.file_id}\n"
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
        "--sync-keep",
        type=int,
        default=None,
        help=(
            "How many backups to keep in the sync folder (defaults to "
            "DB_BACKUP_SYNC_KEEP). Each is ~100 MB with media."
        ),
    )
    parser.add_argument(
        "--if-due-days",
        type=float,
        default=None,
        help=(
            "Scheduler mode: only take a new backup when the newest dump "
            "in backups/ is at least this many days old. Otherwise just "
            "retry the Drive upload of the newest dump if it never made it."
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
    if args.sync_keep is None:
        args.sync_keep = settings.DB_BACKUP_SYNC_KEEP

    folder_ref = (settings.DB_BACKUP_GDRIVE or "").strip()
    folder_id: Optional[str] = None
    # A Drive problem must never cost us the local backup, so config
    # errors downgrade to local-only instead of exiting.
    if not args.skip_upload:
        if not folder_ref:
            logger.warning(
                "DB_BACKUP_GDRIVE is not set in .env; backing up locally only."
            )
            args.skip_upload = True
        else:
            try:
                folder_id = google_drive.parse_folder_id(folder_ref)
            except google_drive.DriveNotConfigured as exc:
                logger.error("Bad DB_BACKUP_GDRIVE value (%s); backing up locally only.", exc)
                args.skip_upload = True

    if args.if_due_days is not None:
        latest = newest_dump()
        if latest is not None:
            age_days = (time.time() - latest.stat().st_mtime) / 86400
            if age_days < args.if_due_days:
                mirror_rc = retry_pending_mirror(latest, keep=args.sync_keep)
                upload_rc = retry_pending_upload(latest, folder_id)
                return mirror_rc or upload_rc
            logger.info(
                "Newest backup %s is %.1f days old — backup is due.",
                latest.name,
                age_days,
            )
        else:
            logger.info("No backups on disk yet — backup is due.")

    if folder_id:
        logger.info("Drive destination folder id: %s", folder_id)

    try:
        dump_path = run_pg_dump(keep=args.keep)
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        logger.error("Backup step failed: %s", exc)
        return 3

    mirror_rc = 0
    try:
        target = mirror_to_sync_dir(dump_path, keep=args.sync_keep)
        if target is None:
            logger.warning(
                "No sync folder (install Google Drive for desktop or set "
                "DB_BACKUP_SYNC_DIR); this backup has no synced copy yet."
            )
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        logger.error("Copy to sync folder failed: %s", exc)
        mirror_rc = 5

    if args.skip_upload:
        logger.info("--skip-upload set; not uploading %s via the Drive API.", dump_path)
        return mirror_rc

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
        return mirror_rc or 4

    logger.info("Done.")
    return mirror_rc


def retry_pending_mirror(dump_path: Path, *, keep: int) -> int:
    """Not due for a new backup: copy the newest one if the sync folder lacks it."""
    if get_settings().backup_sync_dir is None or is_mirrored(dump_path):
        return 0
    try:
        mirror_to_sync_dir(dump_path, keep=keep)
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        logger.error("Copy to sync folder failed: %s", exc)
        return 5
    return 0


def retry_pending_upload(dump_path: Path, folder_id: Optional[str]) -> int:
    """Not due for a new backup: push the newest one if Drive never got it.

    Runs hourly, so the "Google not connected" case stays silent; the
    weekly run already logged it, and the status page shows it.
    """
    if folder_id is None or uploaded_marker_for(dump_path).exists():
        return 0
    try:
        with SessionLocal() as db:
            assistant_id = find_uploader_assistant_id(db)
    except RuntimeError as exc:
        logger.debug("Pending upload of %s skipped: %s", dump_path.name, exc)
        return 0
    logger.info("Retrying Drive upload of %s as assistant_id=%s", dump_path.name, assistant_id)
    try:
        upload_dump(dump_path=dump_path, folder_id=folder_id, assistant_id=assistant_id)
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        logger.error("Upload retry failed: %s", exc)
        return 4
    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
