"""The ``face_embeddings`` table — InsightFace feature vectors per photo.

Each row is a 512-dim float32 embedding (ArcFace / InsightFace buffalo_l)
extracted from a single ``person_photo`` that the admin flagged for face
recognition. At runtime we load all rows for a family into memory, L2-
normalize them, and use cosine similarity to identify faces from the
webcam stream.

We store the vector as raw ``bytes`` (512 * 4 = 2048 bytes) rather than a
pgvector column so the app still runs on vanilla Postgres without any
extensions installed.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._mixins import TimestampMixin


class FaceEmbedding(Base, TimestampMixin):
    __tablename__ = "face_embeddings"
    __table_args__ = {
        "comment": (
            "One InsightFace embedding per person_photo that was flagged "
            "for face recognition. Cascade-deletes when the photo is "
            "removed, so the enrolled face gallery always stays in sync."
        )
    }

    face_embedding_id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True
    )
    person_photo_id: Mapped[int] = mapped_column(
        ForeignKey("person_photos.person_photo_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="Photo this embedding was extracted from.",
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Person the embedding identifies. Denormalized for fast lookup.",
    )
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Family tenant. Denormalized so recognition queries don't need joins.",
    )
    model_name: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        default="buffalo_l",
        comment="InsightFace model pack used to produce the embedding.",
    )
    embedding_dim: Mapped[int] = mapped_column(
        Integer, nullable=False, default=512
    )
    embedding_bytes: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment="Raw float32 little-endian bytes. 512 dims × 4 = 2048 bytes.",
    )
    bounding_box_json: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
        comment="JSON [x1, y1, x2, y2] of the detected face in the source image.",
    )

    person_photo: Mapped["PersonPhoto"] = relationship()  # noqa: F821
    person: Mapped["Person"] = relationship()  # noqa: F821
