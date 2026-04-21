"""Move per-person work email into a new ``jobs`` resource.

Each person can now have zero, one, or many jobs. A job captures:

* ``company_name`` — employer / company as the household refers to it
* ``company_website`` — free-form URL
* ``role_title`` — the person's role at the company
* ``work_email`` — work mailbox / Google Calendar id (formerly
  ``people.work_email``)
* ``description`` — free-form notes

Migration steps
---------------
1. Create ``jobs`` (with ``person_id`` FK + indexes).
2. **Backfill**: for every person with a non-NULL ``work_email``,
   insert one row into ``jobs`` carrying just that work email so the
   AI assistant's calendar resolver and email-inbox lookup keep
   matching the same addresses they did before. Other job fields
   stay NULL — the admin can fill in the company name / role on the
   person's profile when they get to it.
3. Drop the ``ix_people_work_email_lower`` expression index (added
   in 0017).
4. Drop ``people.work_email`` column.

The downgrade reverses everything: re-adds the column + index, and
backfills it from the FIRST job (by job_id) that has a work_email
for each person — there's no perfectly faithful inverse for "many
jobs → one column", but picking the lowest job_id is deterministic
and matches what an admin would have manually set on the old
``people.work_email`` column before the upgrade.

Revision ID: 0026_jobs
Revises: 0025_monitoring_tasks
Create Date: 2026-04-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0026_jobs"
down_revision: Union[str, None] = "0025_monitoring_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column(
            "job_id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            nullable=False,
            comment="The person who holds (or held) this job.",
        ),
        sa.Column(
            "company_name",
            sa.String(length=200),
            nullable=True,
            comment=(
                "Employer / company name as the household refers to "
                "it (e.g. 'Acme Corp'). Optional so an admin can "
                "record a work email without yet filling in the "
                "company details."
            ),
        ),
        sa.Column(
            "company_website",
            sa.String(length=500),
            nullable=True,
            comment=(
                "Employer's primary website URL. Free-form text — "
                "store exactly what the user typed (with or without "
                "https://) so we don't accidentally normalise away "
                "tracking paths or subdomains the household cares "
                "about."
            ),
        ),
        sa.Column(
            "role_title",
            sa.String(length=160),
            nullable=True,
            comment=(
                "Person's role / job title at this company, e.g. "
                "'Senior Engineer', 'Pediatric Nurse', 'Owner'."
            ),
        ),
        sa.Column(
            "work_email",
            sa.String(length=255),
            nullable=True,
            comment=(
                "Work / employer email address for this job. Used by "
                "the AI assistant as a Google Calendar id when "
                "checking availability or listing events for this "
                "person — work calendars are typically only shared "
                "as free/busy, while personal calendars are "
                "full-detail. Optional. A person with multiple jobs "
                "has multiple work calendars merged into the "
                "freebusy lookup."
            ),
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-form notes about the job — team, scope, work "
                "schedule, anything the household assistant should "
                "know to answer questions like 'is Ben in a "
                "meeting?' or 'when does Mom usually leave for "
                "work?'."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        comment=(
            "Employment / role history for a household member. One "
            "row per job (past or present). The optional work_email "
            "doubles as a Google Calendar id for the AI assistant's "
            "calendar tools — work calendars are usually shared as "
            "free/busy only, while the person's personal calendar "
            "(people.email_address) is full-detail. A person can "
            "have multiple concurrent jobs (consulting + day job) "
            "or a chain of past employers."
        ),
    )
    op.create_index("ix_jobs_person_id", "jobs", ["person_id"])
    # Same lower(work_email) functional index the email-inbox poller
    # relied on when work_email lived on `people` — keeps the
    # case-insensitive sender lookup fast across many jobs.
    op.create_index(
        "ix_jobs_work_email_lower",
        "jobs",
        [sa.text("lower(work_email)")],
        unique=False,
    )

    # ---- Backfill from people.work_email ----------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO jobs (person_id, work_email, created_at, updated_at)
            SELECT person_id, work_email, NOW(), NOW()
              FROM people
             WHERE work_email IS NOT NULL
               AND TRIM(work_email) <> ''
            """
        )
    )

    # ---- Drop the now-orphaned column + its index -------------------------
    op.drop_index("ix_people_work_email_lower", table_name="people")
    op.drop_column("people", "work_email")


def downgrade() -> None:
    # Re-add the column + index first so the backfill below has
    # somewhere to write.
    op.add_column(
        "people",
        sa.Column(
            "work_email",
            sa.String(length=255),
            nullable=True,
            comment=(
                "Work / employer email address. Used by the AI "
                "assistant as a SECOND Google Calendar id when "
                "checking availability or listing events for this "
                "person — work calendars are typically only shared "
                "as free/busy, while personal calendars are "
                "full-detail. Optional."
            ),
        ),
    )
    op.create_index(
        "ix_people_work_email_lower",
        "people",
        [sa.text("lower(work_email)")],
        unique=False,
    )

    # For each person, copy the work_email from the lowest-id job
    # that has one set. There's no faithful inverse for many → one,
    # but picking job_id ASC is deterministic and matches what was
    # likely the original value (the upgrade backfill inserted in
    # person_id order, so the lowest job_id is the row that came
    # from the column).
    op.execute(
        sa.text(
            """
            UPDATE people p
               SET work_email = sub.work_email
              FROM (
                  SELECT DISTINCT ON (person_id)
                         person_id, work_email
                    FROM jobs
                   WHERE work_email IS NOT NULL
                     AND TRIM(work_email) <> ''
                ORDER BY person_id, job_id
              ) AS sub
             WHERE p.person_id = sub.person_id
            """
        )
    )

    op.drop_index("ix_jobs_work_email_lower", table_name="jobs")
    op.drop_index("ix_jobs_person_id", table_name="jobs")
    op.drop_table("jobs")
