"""Short-lived public links to a file Avi is sending by WhatsApp or text.

Twilio can't be handed a file: it is given a URL and fetches the file
itself, from the public internet, with no login. So this is the one
route that serves household files without a session.

What keeps that safe is the link itself (see
:mod:`api.services.outbound_files`): it is signed by this server, it
names exactly one file as prepared for one channel, and it expires a
few minutes after the reply is sent. A link that has been altered,
has expired, or points at a record that no longer exists gets the
same 404 as an address that never existed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response

from ..db import SessionLocal
from ..services import outbound_files

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outbound-files", tags=["outbound-files"])


@router.get("/{token}/{filename}")
def get_outbound_file(token: str, filename: str) -> Response:
    # ``filename`` is only there so the URL ends in a sensible name;
    # the token alone decides what is served.
    with SessionLocal() as db:
        try:
            prepared = outbound_files.open_token(db, token)
        except outbound_files.FileUnavailable as exc:
            logger.info("Outbound file link refused: %s", exc)
            raise HTTPException(status_code=404, detail="Not found") from exc
    logger.info(
        "Outbound file link served name=%s bytes=%d",
        prepared.filename,
        len(prepared.data),
    )
    return Response(
        content=prepared.data,
        media_type=prepared.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Robots-Tag": "noindex",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{prepared.filename}"',
        },
    )
