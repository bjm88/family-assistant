"""Application-layer symmetric encryption for sensitive columns.

We use Fernet (AES-128-CBC + HMAC-SHA256) so ciphertext is self-describing and
tamper-evident. The key lives in ``FA_ENCRYPTION_KEY`` (never in the database).

Design notes
------------
* Encrypted columns are stored as ``bytea`` ciphertext. They are never logged,
  never returned from the API in raw form, and never used as query predicates.
* For columns that need to be *searchable* or *partially displayable* (SSN,
  credit card, VIN, license plate, policy #) we pair the ciphertext column
  with a plain ``*_last_four`` or ``*_masked`` text column. The dynamic-SQL
  LLM can freely filter on the plain helper column without ever touching
  the ciphertext.
* A Postgres SQL function ``fa_decrypt_placeholder`` is installed by the
  initial migration purely as documentation so the LLM catalog shows that
  decryption is *not* available inside SQL — only via the application.
"""

from __future__ import annotations

from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = get_settings().FA_ENCRYPTION_KEY.strip()
    if not key or key == "REPLACE_ME_WITH_A_FERNET_KEY":
        raise RuntimeError(
            "FA_ENCRYPTION_KEY is not configured. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
            "and place it in your .env file."
        )
    _fernet = Fernet(key.encode("utf-8"))
    return _fernet


def encrypt_str(plaintext: Optional[str]) -> Optional[bytes]:
    """Encrypt a user-provided string. ``None``/empty input passes through."""
    if plaintext is None or plaintext == "":
        return None
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_str(ciphertext: Optional[bytes]) -> Optional[str]:
    """Decrypt a ciphertext blob back to plaintext. ``None`` passes through."""
    if ciphertext is None:
        return None
    try:
        return _get_fernet().decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Failed to decrypt column — the FA_ENCRYPTION_KEY does not match "
            "the key used when this row was written."
        ) from exc


def last_four(value: Optional[str]) -> Optional[str]:
    """Return the last four characters of a sensitive identifier, for display."""
    if value is None:
        return None
    digits = "".join(ch for ch in value if ch.isalnum())
    if len(digits) < 4:
        return digits or None
    return digits[-4:]
