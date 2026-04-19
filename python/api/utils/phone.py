"""Phone-number normalisation helpers.

Twilio always delivers phone numbers in strict E.164 (``+14155551234``).
The numbers a user types into a person record are messier — they may
include parentheses, dashes, dots, leading-1 country code, country code
without ``+``, or be missing the country code entirely.

These helpers boil any input down to ``+`` followed by digits so we can
do an apples-to-apples comparison between an inbound Twilio number and
the value sitting in ``people.{mobile,home,work}_phone_number``.

Why not pull in ``phonenumbers``? It would do a more rigorous job (per-
country length validation, plus-code parsing), but the cost is a
~10 MB dependency for what amounts to "strip junk and prepend +1 if
the user clearly typed a US number". We can swap it in later if we
decide we need the full library.
"""

from __future__ import annotations

import re
from typing import Optional


_DIGITS_RE = re.compile(r"\D+")


def normalize_phone(raw: Optional[str], *, default_country_code: str = "1") -> Optional[str]:
    """Return ``raw`` as ``+<digits>`` E.164 — or ``None`` if not parseable.

    Rules
    -----
    * Strip every non-digit (including the leading ``+``); we'll add
      a new one at the end.
    * If the resulting string is empty → ``None``.
    * If it starts with ``00`` (international IDD prefix), drop them.
    * If the length looks like a US local number (10 digits) we prepend
      ``default_country_code`` so ``(415) 555-1234`` becomes
      ``+14155551234``. The default of ``1`` matches the household's
      Twilio number, but is overrideable for non-US users later.
    * If the length is exactly 11 and starts with ``default_country_code``
      we trust it as a US number with a country code typed without the
      ``+``.
    * Otherwise we accept whatever digits we have and prepend ``+``.
      Garbage in → garbage out, but at least it's *normalised* garbage
      so the equality check is deterministic.
    """
    if not raw:
        return None
    digits = _DIGITS_RE.sub("", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None
    if len(digits) == 10:
        digits = default_country_code + digits
    return "+" + digits


def phones_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Compare two phone-number strings after normalisation."""
    na = normalize_phone(a)
    nb = normalize_phone(b)
    if na is None or nb is None:
        return False
    return na == nb
