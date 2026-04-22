#!/usr/bin/env python3
"""scripts/bootstrap_from_filesystem.py

One-time disaster-recovery bootstrap. Reconstructs the minimum DB state
needed to make the app functional after a catastrophic data loss, using
the surviving on-disk uploads under ``$FA_STORAGE_ROOT/family_<id>/...``
as the source of truth for what existed.

What it does
------------

1. Prints the resolved target DB and pauses for ``--yes`` (or
   interactive confirmation). Refuses to run if the resolved DB name
   ends in ``_test`` — bootstraps belong on the live DB only.

2. Runs ``alembic upgrade head`` against the target. After the post-
   incident DROP SCHEMA the DB is empty; this rebuilds the schema.

3. Walks ``resources/family/family_<id>/`` and INSERTs:

   - One ``Family`` row with the right ``family_id`` (recovered from
     the directory name).
   - One ``Assistant`` row, pointing at the surviving avatar in
     ``family_<id>/assistant/``.
   - One ``Person`` row per ``people/person_<id>/`` directory, with
     placeholder names (``"Person 7"``) and the ``profile_photo_path``
     wired to the surviving image. ``ai_can_write_calendar`` defaults
     to false; the admin can opt in later.
   - One ``PersonPhoto`` row per file under ``people/person_<id>/photos/``.
   - One ``Pet`` row per ``pets/pet_<id>/`` directory, with placeholder
     name and ``animal_type='other'``. ``PetPhoto`` rows for each file
     under ``photos/``.
   - One ``Residence`` row per ``residences/residence_<id>/``, with
     placeholder address ('(unknown)' / '(unknown)'). ``ResidencePhoto``
     rows for each file under ``photos/``.
   - One ``Vehicle`` row per ``vehicles/vehicle_<id>/``, with
     placeholder ``make/model='(unknown)'``, ``vehicle_type='car'``,
     and ``profile_image_path`` pointing at the surviving file.

4. Resets the relevant Postgres sequences (``..._id_seq``) to
   ``MAX(id) + 1`` so future inserts via the admin UI keep going up
   without colliding with the explicit IDs we just inserted.

5. Prints a summary table.

What it does NOT do
-------------------

- Does NOT re-create chats, audit rows, agent_tasks, tasks, jobs,
  goals, medical records, financial accounts, identity documents,
  insurance policies, or relationships. Those have no on-disk
  evidence and were lost with the DROP SCHEMA. The admin re-enters
  them by hand after this bootstrap finishes.
- Does NOT touch any directory whose ID isn't an integer (skips
  ``tts_cache/`` etc.). The walk is conservative.
- Does NOT delete or move any file on disk. The bootstrap only INSERTs.
- Does NOT seed Google OAuth credentials, Telegram contact verifications,
  or invites. Those have to be recreated through the normal admin flow.

Usage
-----

::

    # Interactive — prompts for DB-name confirmation:
    python scripts/bootstrap_from_filesystem.py

    # Non-interactive (CI / automated):
    python scripts/bootstrap_from_filesystem.py --yes

    # Dry-run: prints the plan but doesn't touch the DB at all:
    python scripts/bootstrap_from_filesystem.py --dry-run

The script is idempotent in the sense that a second run aborts at the
"family already exists" check rather than double-inserting. To re-run
from scratch, drop the schema first.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))

from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from api import models  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.db import SessionLocal, engine  # noqa: E402


# ---------------------------------------------------------------------------
# CLI + safety
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reconstruct DB shell records from on-disk uploads.",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the interactive DB-name confirmation prompt.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print what would be inserted without touching the DB. "
            "Implies --yes (no destructive write happens either way)."
        ),
    )
    return p.parse_args()


def _confirm_target(assume_yes: bool, dry_run: bool) -> str:
    """Print the resolved target and either prompt or accept --yes.

    Returns the DB name. Refuses to run against anything whose name
    contains ``test`` — bootstraps belong on the live DB; tests use
    the harness in ``tests/integration/conftest.py``.
    """
    settings = get_settings()
    db_name = settings.FA_DB_NAME
    print("=" * 64)
    print("family-assistant DR BOOTSTRAP")
    print("=" * 64)
    print(f"  target DB     : {db_name}")
    print(f"  host:port     : {settings.FA_DB_HOST}:{settings.FA_DB_PORT}")
    print(f"  user          : {settings.FA_DB_USER}")
    print(f"  storage root  : {settings.storage_root}")
    print(f"  mode          : {'dry-run' if dry_run else 'WRITE'}")
    print("=" * 64)

    if "test" in db_name.lower():
        sys.exit(
            f"\nERROR: refusing to bootstrap against {db_name!r} — DB "
            "name contains 'test'. The DR bootstrap is for the live "
            "DB only; tests use tests/integration/conftest.py."
        )

    if dry_run or assume_yes:
        return db_name

    confirm = input(f"\nType the target DB name to confirm: ").strip()
    if confirm != db_name:
        sys.exit(f"Mismatch ({confirm!r} != {db_name!r}). Aborting.")
    return db_name


# ---------------------------------------------------------------------------
# Filesystem inventory
# ---------------------------------------------------------------------------


_ID_RE = re.compile(r"^(?P<kind>\w+?)_(?P<id>\d+)$")


def _id_from_dir(name: str, expected_kind: str) -> int | None:
    """``person_7`` → ``7``; returns ``None`` for ``tts_cache`` etc."""
    m = _ID_RE.match(name)
    if not m or m.group("kind") != expected_kind:
        return None
    return int(m.group("id"))


@dataclass
class PersonRecovery:
    person_id: int
    profile_path: Path | None = None  # absolute path to the surviving profile image
    photo_paths: list[Path] = field(default_factory=list)


@dataclass
class PetRecovery:
    pet_id: int
    photo_paths: list[Path] = field(default_factory=list)


@dataclass
class ResidenceRecovery:
    residence_id: int
    photo_paths: list[Path] = field(default_factory=list)


@dataclass
class VehicleRecovery:
    vehicle_id: int
    profile_path: Path | None = None


@dataclass
class FamilyRecovery:
    family_id: int
    family_root: Path
    assistant_avatar: Path | None = None
    people: dict[int, PersonRecovery] = field(default_factory=dict)
    pets: dict[int, PetRecovery] = field(default_factory=dict)
    residences: dict[int, ResidenceRecovery] = field(default_factory=dict)
    vehicles: dict[int, VehicleRecovery] = field(default_factory=dict)


def _scan_filesystem(storage_root: Path) -> list[FamilyRecovery]:
    """Walk ``storage_root`` and collect everything we can reseed."""
    families: list[FamilyRecovery] = []
    if not storage_root.exists():
        return families

    for fam_dir in sorted(storage_root.iterdir()):
        if not fam_dir.is_dir():
            continue
        fam_id = _id_from_dir(fam_dir.name, "family")
        if fam_id is None:
            continue
        rec = FamilyRecovery(family_id=fam_id, family_root=fam_dir)

        # Assistant avatar
        assistant_dir = fam_dir / "assistant"
        if assistant_dir.exists():
            avatars = sorted(p for p in assistant_dir.iterdir() if p.is_file())
            if avatars:
                # Pick the most-recently-modified — that's the latest
                # generation in the live system's logic too.
                rec.assistant_avatar = max(avatars, key=lambda p: p.stat().st_mtime)

        # People
        people_dir = fam_dir / "people"
        if people_dir.exists():
            for pdir in sorted(people_dir.iterdir()):
                pid = _id_from_dir(pdir.name, "person")
                if pid is None:
                    continue
                pr = PersonRecovery(person_id=pid)
                profile_dir = pdir / "profile"
                if profile_dir.exists():
                    profiles = sorted(p for p in profile_dir.iterdir() if p.is_file())
                    if profiles:
                        pr.profile_path = max(profiles, key=lambda p: p.stat().st_mtime)
                photos_dir = pdir / "photos"
                if photos_dir.exists():
                    pr.photo_paths = sorted(p for p in photos_dir.iterdir() if p.is_file())
                rec.people[pid] = pr

        # Pets
        pets_dir = fam_dir / "pets"
        if pets_dir.exists():
            for pdir in sorted(pets_dir.iterdir()):
                pid = _id_from_dir(pdir.name, "pet")
                if pid is None:
                    continue
                pets_rec = PetRecovery(pet_id=pid)
                photos_dir = pdir / "photos"
                if photos_dir.exists():
                    pets_rec.photo_paths = sorted(
                        p for p in photos_dir.iterdir() if p.is_file()
                    )
                rec.pets[pid] = pets_rec

        # Residences
        res_dir = fam_dir / "residences"
        if res_dir.exists():
            for rdir in sorted(res_dir.iterdir()):
                rid = _id_from_dir(rdir.name, "residence")
                if rid is None:
                    continue
                rr = ResidenceRecovery(residence_id=rid)
                photos_dir = rdir / "photos"
                if photos_dir.exists():
                    rr.photo_paths = sorted(p for p in photos_dir.iterdir() if p.is_file())
                rec.residences[rid] = rr

        # Vehicles — single profile image directly in vehicle_<id>/
        veh_dir = fam_dir / "vehicles"
        if veh_dir.exists():
            for vdir in sorted(veh_dir.iterdir()):
                vid = _id_from_dir(vdir.name, "vehicle")
                if vid is None:
                    continue
                vr = VehicleRecovery(vehicle_id=vid)
                files = sorted(p for p in vdir.iterdir() if p.is_file())
                if files:
                    vr.profile_path = max(files, key=lambda p: p.stat().st_mtime)
                rec.vehicles[vid] = vr

        families.append(rec)

    return families


def _relative_to_root(p: Path, storage_root: Path) -> str:
    """``stored_file_path`` columns are paths relative to ``FA_STORAGE_ROOT``."""
    return str(p.resolve().relative_to(storage_root.resolve()))


# ---------------------------------------------------------------------------
# DB seeding
# ---------------------------------------------------------------------------


# Tables whose primary keys we set explicitly (not via auto-increment)
# during the bootstrap. After inserting we have to bump the matching
# Postgres sequence past MAX(id) so the next admin-UI insert doesn't
# collide. The pairs below are (sequence_name, table.id_column).
_SEQUENCES_TO_RESET: list[tuple[str, str, str]] = [
    ("families_family_id_seq", "families", "family_id"),
    ("people_person_id_seq", "people", "person_id"),
    ("pets_pet_id_seq", "pets", "pet_id"),
    ("residences_residence_id_seq", "residences", "residence_id"),
    ("vehicles_vehicle_id_seq", "vehicles", "vehicle_id"),
    ("assistants_assistant_id_seq", "assistants", "assistant_id"),
    ("person_photos_person_photo_id_seq", "person_photos", "person_photo_id"),
    ("pet_photos_pet_photo_id_seq", "pet_photos", "pet_photo_id"),
    ("residence_photos_residence_photo_id_seq", "residence_photos", "residence_photo_id"),
]


def _seed(rec: FamilyRecovery, storage_root: Path) -> dict[str, int]:
    """Insert one family's recovered shell records. Returns a count summary."""
    counts = {k: 0 for k in (
        "family", "assistant", "people", "person_photos",
        "pets", "pet_photos", "residences", "residence_photos", "vehicles",
    )}

    with SessionLocal() as db:
        existing = db.execute(
            select(models.Family).where(models.Family.family_id == rec.family_id)
        ).scalar_one_or_none()
        if existing is not None:
            print(
                f"  family_id={rec.family_id} already exists "
                f"({existing.family_name!r}); skipping insert. Photos/people/etc "
                "for an existing family are not re-applied to avoid double-inserts."
            )
            return counts

        # Family
        family = models.Family(
            family_id=rec.family_id,
            family_name=f"Family {rec.family_id} (Restored)",
            head_of_household_notes=(
                "Auto-restored shell record after the 2026-04-21 incident. "
                "Replace this name and fill in details via the admin UI."
            ),
        )
        db.add(family)
        db.flush()
        counts["family"] = 1

        # Assistant
        assistant_avatar_rel = (
            _relative_to_root(rec.assistant_avatar, storage_root)
            if rec.assistant_avatar else None
        )
        db.add(models.Assistant(
            family_id=rec.family_id,
            assistant_name="Avi",
            profile_image_path=assistant_avatar_rel,
        ))
        counts["assistant"] = 1

        # People
        for pid in sorted(rec.people):
            pr = rec.people[pid]
            db.add(models.Person(
                person_id=pid,
                family_id=rec.family_id,
                first_name=f"Person",
                last_name=str(pid),
                profile_photo_path=(
                    _relative_to_root(pr.profile_path, storage_root)
                    if pr.profile_path else None
                ),
                notes=(
                    "Auto-restored shell record. Replace name and fill "
                    "in details via the admin UI."
                ),
            ))
            counts["people"] += 1
            for photo in pr.photo_paths:
                db.add(models.PersonPhoto(
                    person_id=pid,
                    title="(restored)",
                    description=(
                        "Auto-restored from the on-disk file after the "
                        "2026-04-21 incident. Original title/description lost."
                    ),
                    use_for_face_recognition=True,
                    stored_file_path=_relative_to_root(photo, storage_root),
                    original_file_name=photo.name,
                    mime_type=_guess_mime(photo),
                    file_size_bytes=photo.stat().st_size,
                ))
                counts["person_photos"] += 1

        # Pets
        for pid in sorted(rec.pets):
            pets_rec = rec.pets[pid]
            db.add(models.Pet(
                pet_id=pid,
                family_id=rec.family_id,
                pet_name=f"Pet {pid}",
                animal_type="other",
                notes=(
                    "Auto-restored shell record. Replace name + species "
                    "via the admin UI."
                ),
            ))
            counts["pets"] += 1
            for photo in pets_rec.photo_paths:
                db.add(models.PetPhoto(
                    pet_id=pid,
                    title="(restored)",
                    description="Auto-restored from on-disk file.",
                    stored_file_path=_relative_to_root(photo, storage_root),
                    original_file_name=photo.name,
                    mime_type=_guess_mime(photo),
                    file_size_bytes=photo.stat().st_size,
                ))
                counts["pet_photos"] += 1

        # Residences
        for rid in sorted(rec.residences):
            rr = rec.residences[rid]
            db.add(models.Residence(
                residence_id=rid,
                family_id=rec.family_id,
                label=f"Residence {rid}",
                street_line_1="(unknown)",
                city="(unknown)",
                country="United States",
                is_primary_residence=False,
                notes="Auto-restored shell record. Fill in address via admin UI.",
            ))
            counts["residences"] += 1
            for photo in rr.photo_paths:
                db.add(models.ResidencePhoto(
                    residence_id=rid,
                    title="(restored)",
                    description="Auto-restored from on-disk file.",
                    stored_file_path=_relative_to_root(photo, storage_root),
                    original_file_name=photo.name,
                    mime_type=_guess_mime(photo),
                    file_size_bytes=photo.stat().st_size,
                ))
                counts["residence_photos"] += 1

        # Vehicles
        for vid in sorted(rec.vehicles):
            vr = rec.vehicles[vid]
            db.add(models.Vehicle(
                vehicle_id=vid,
                family_id=rec.family_id,
                make="(unknown)",
                model="(unknown)",
                vehicle_type="car",
                profile_image_path=(
                    _relative_to_root(vr.profile_path, storage_root)
                    if vr.profile_path else None
                ),
                notes="Auto-restored shell record. Fill in details via admin UI.",
            ))
            counts["vehicles"] += 1

        db.commit()

    # Bump sequences past the explicit IDs we just used.
    with engine.begin() as conn:
        for seq, table, col in _SEQUENCES_TO_RESET:
            # ``setval(seq, MAX(id))`` with ``is_called=true`` so the
            # NEXT call to nextval returns MAX(id)+1, exactly what we
            # want. ``coalesce`` handles the empty-table case.
            conn.execute(text(
                f"SELECT setval('{seq}', "
                f"COALESCE((SELECT MAX({col}) FROM {table}), 1), true)"
            ))

    return counts


def _guess_mime(p: Path) -> str:
    suffix = p.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")


# ---------------------------------------------------------------------------
# Schema bootstrap (alembic upgrade head)
# ---------------------------------------------------------------------------


def _ensure_schema() -> None:
    """Run ``alembic upgrade head`` against the resolved DB."""
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    print("Running 'alembic upgrade head'...")
    alembic_command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()
    db_name = _confirm_target(assume_yes=args.yes, dry_run=args.dry_run)

    storage_root = get_settings().storage_root
    families = _scan_filesystem(storage_root)
    if not families:
        print(f"\nNo recoverable family directories under {storage_root}. Nothing to do.")
        return 0

    print(f"\nDiscovered {len(families)} family directory/ies under {storage_root}:")
    for rec in families:
        print(
            f"  family_id={rec.family_id}: "
            f"people={len(rec.people)} pets={len(rec.pets)} "
            f"residences={len(rec.residences)} vehicles={len(rec.vehicles)} "
            f"assistant_avatar={'yes' if rec.assistant_avatar else 'no'}"
        )

    if args.dry_run:
        print("\n--dry-run set; not touching the DB.")
        return 0

    _ensure_schema()

    print(f"\nSeeding shell records into DB={db_name}...")
    grand_total = {k: 0 for k in (
        "family", "assistant", "people", "person_photos",
        "pets", "pet_photos", "residences", "residence_photos", "vehicles",
    )}
    for rec in families:
        print(f"\n-- family_id={rec.family_id} --")
        counts = _seed(rec, storage_root)
        for k, v in counts.items():
            grand_total[k] += v
        for k, v in counts.items():
            if v:
                print(f"  inserted {v:>3} {k}")

    print("\n" + "=" * 64)
    print("Bootstrap complete. Inserted totals:")
    for k, v in grand_total.items():
        print(f"  {k:>20}: {v}")
    print("=" * 64)
    print(
        "\nNext steps:\n"
        "  1. Open the admin UI and replace the placeholder names.\n"
        "  2. Re-link Google OAuth (Gmail + Calendar) per assistant.\n"
        "  3. Re-create relationships, jobs, sensitive records as you go.\n"
        "  4. Run ./scripts/db_backup.sh after the first round of edits!\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
