"""The ``person_websites`` table — named URLs associated with a person.

Each row is one website (LinkedIn, personal blog, GitHub, school
portal, …) that belongs to a household member. A person can have
zero, one, or many. Keeping these as discrete rows (rather than a
free-text blob on ``people``) lets the admin UI manage them as a
list and lets the AI assistant cite a specific named link when
answering questions like "what's Ben's LinkedIn?".
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class PersonWebsite(Base, TimestampMixin):
    __tablename__ = "person_websites"
    __table_args__ = {
        "comment": (
            "Named websites belonging to a household member. One "
            "row per (name, url) pair — e.g. name='LinkedIn', "
            "url='https://linkedin.com/in/…'. A person can have "
            "many websites; nothing prevents two rows sharing a "
            "name if the household wants that."
        ),
    }

    person_website_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The person this website belongs to.",
    )

    name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment=(
            "Short label for the site as the household refers to "
            "it, e.g. 'LinkedIn', 'Personal site', 'GitHub'."
        ),
    )
    url: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment=(
            "Website URL. Free-form text — store exactly what the "
            "user typed (with or without https://) so we don't "
            "accidentally normalise away paths or subdomains the "
            "household cares about."
        ),
    )

    person: Mapped["Person"] = relationship(  # noqa: F821
        back_populates="websites"
    )
