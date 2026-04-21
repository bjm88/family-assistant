"""The ``jobs`` table — a person's employment / role history.

Each row is one job a person holds (or has held). A person can have
zero, one, or many jobs — past or present. Capturing a separate row
per job lets the family knowledge base distinguish "where do they
work?" from "what's their work email?" and keeps both in lockstep
when someone changes employer.

The ``work_email`` column on this table is what the AI assistant
treats as a SECOND Google Calendar id for the person — work
calendars are typically only shared as free/busy while personal
calendars (``people.email_address``) are full-detail. Before this
table existed, ``work_email`` lived directly on ``people``; that
column was dropped in migration 0026 and the data backfilled into
``jobs`` so the calendar resolver and email-inbox lookup keep
working without behaviour changes.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = {
        "comment": (
            "Employment / role history for a household member. One "
            "row per job (past or present). The optional work_email "
            "doubles as a Google Calendar id for the AI assistant's "
            "calendar tools — work calendars are usually shared as "
            "free/busy only, while the person's personal calendar "
            "(people.email_address) is full-detail. A person can "
            "have multiple concurrent jobs (consulting + day job) "
            "or a chain of past employers."
        ),
    }

    job_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The person who holds (or held) this job.",
    )

    company_name: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
        comment=(
            "Employer / company name as the household refers to it "
            "(e.g. 'Acme Corp'). Optional so an admin can record a "
            "work email without yet filling in the company details."
        ),
    )
    company_website: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Employer's primary website URL. Free-form text — store "
            "exactly what the user typed (with or without https://) "
            "so we don't accidentally normalise away tracking paths "
            "or subdomains the household cares about."
        ),
    )
    role_title: Mapped[Optional[str]] = mapped_column(
        String(160),
        nullable=True,
        comment=(
            "Person's role / job title at this company, e.g. "
            "'Senior Engineer', 'Pediatric Nurse', 'Owner'."
        ),
    )
    work_email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment=(
            "Work / employer email address for this job. Used by the "
            "AI assistant as a Google Calendar id when checking "
            "availability or listing events for this person — work "
            "calendars are typically only shared as free/busy, while "
            "personal calendars are full-detail. Optional. A person "
            "with multiple jobs has multiple work calendars merged "
            "into the freebusy lookup."
        ),
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Free-form notes about the job — team, scope, work "
            "schedule, anything the household assistant should know "
            "to answer questions like 'is Ben in a meeting?' or "
            "'when does Mom usually leave for work?'."
        ),
    )

    person: Mapped["Person"] = relationship(  # noqa: F821
        back_populates="jobs"
    )
