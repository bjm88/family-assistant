#!/usr/bin/env python3
"""Catch Avi up on unread email and messages that arrived while it was down.

Same work the assistant runs for itself on startup. Use this when you
want the catch-up without restarting the backend::

    uv run python scripts/recover_inbound.py

The live pollers take the same lock, so running this while Avi is up
will not double-reply. Exit 0 when the pass finishes (including a
disabled flag). Exit 1 when the pass itself crashes.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_PKG_ROOT = REPO_ROOT / "python"
if str(PYTHON_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_PKG_ROOT))


from api import models  # noqa: E402,F401  (ensure model registry is populated)
from api.services.inbound_recovery import recover_missed_inbound  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    # httpx logs full request URLs at INFO, which would include the
    # Telegram bot token. Match the API process and keep those quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        report = recover_missed_inbound()
    except Exception:  # noqa: BLE001 - the script's job is to surface this
        logging.getLogger(__name__).exception("Inbound recovery crashed")
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
