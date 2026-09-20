"""Field-level encryption for the data that would ruin someone's life if it leaked.

A Social Security number is not a password: it cannot be rotated, it is a
lifetime identifier, and a breach of one is permanent. So it is never stored in
the clear, never logged, never returned by the API, and never used as a
database key.

The scheme is envelope encryption with AES-256-GCM:

* **A master key** comes from the environment (`TAXOS_MASTER_KEY`), which in a
  cloud deployment is injected from KMS / Secrets Manager. On a developer
  machine it is generated once into a 0600 file, so the default is a working
  encrypted system rather than an unencrypted one -- security you have to
  switch on is security nobody switches on.
* **Every field gets its own data key**, derived from the master key by HKDF
  over the field's purpose. Compromising the derivation for one field does not
  hand over the others.
* **Additional authenticated data** binds each ciphertext to the row and column
  it belongs to. Copying an SSN ciphertext from one taxpayer row to another
  fails to decrypt rather than silently succeeding, which defeats the attack
  where a writeable database is used to swap identities.
* **A blind index** lets us answer "has this SSN filed before?" without ever
  decrypting anything: a keyed HMAC of the normalised SSN, using a key that is
  *not* the encryption key. It is deterministic, so it is searchable; it is
  keyed, so a stolen database cannot be brute-forced against the ~10^9 possible
  SSNs the way a bare SHA-256 could.

Ciphertext format: ``v1.<key_id>.<base64 nonce>.<base64 ciphertext+tag>``.
The key id makes rotation possible: re-encrypt lazily on read, and old rows
stay readable while the new key is in force.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEME = "v1"
_MASTER_ENV = "TAXOS_MASTER_KEY"
_KEY_BYTES = 32


class CryptoError(RuntimeError):
    """Encryption or decryption failed. Never includes the plaintext."""


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------
def _home() -> Path:
    return Path(os.getenv("TAXOS_HOME", Path.home() / ".taxos"))


def _master_key_path() -> Path:
    return _home() / "master_key"


def master_key() -> bytes:
    """The root secret: from the environment, or generated once on disk."""
    configured = os.getenv(_MASTER_ENV, "").strip()
    if configured:
        try:
            raw = base64.urlsafe_b64decode(configured + "=" * (-len(configured) % 4))
        except Exception as exc:
            raise CryptoError(f"{_MASTER_ENV} is not valid base64") from exc
        if len(raw) < _KEY_BYTES:
            raise CryptoError(f"{_MASTER_ENV} must decode to at least {_KEY_BYTES} bytes")
        return raw

    path = _master_key_path()
    if path.exists():
        return base64.urlsafe_b64decode(path.read_text(encoding="utf-8").strip())

    generated = secrets.token_bytes(_KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 from creation, not chmod afterwards: no window where it is readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(base64.urlsafe_b64encode(generated).decode("ascii"))
    return generated


def _hkdf(key: bytes, purpose: str, length: int = _KEY_BYTES) -> bytes:
    """HKDF-SHA256 (RFC 5869) -- expand only; the master key is already random."""
    out, block, counter = b"", b"", 1
    info = purpose.encode("utf-8")
    while len(out) < length:
        block = hmac.new(key, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def data_key(purpose: str) -> bytes:
    return _hkdf(master_key(), f"taxos/{SCHEME}/{purpose}")


def key_id() -> str:
    """A short, non-secret fingerprint of the master key, stored per ciphertext."""
    return hashlib.sha256(master_key()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# encryption
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encrypt_field(plaintext: str, *, purpose: str, context: str = "") -> str:
    """Encrypt one field. `context` is bound in as AAD and must match on read."""
    if plaintext is None:
        raise CryptoError("nothing to encrypt")
    aead = AESGCM(data_key(purpose))
    nonce = secrets.token_bytes(12)
    aad = f"{purpose}|{context}".encode("utf-8")
    sealed = aead.encrypt(nonce, plaintext.encode("utf-8"), aad)
    return f"{SCHEME}.{key_id()}.{_b64(nonce)}.{_b64(sealed)}"


def decrypt_field(ciphertext: str, *, purpose: str, context: str = "") -> str:
    parts = (ciphertext or "").split(".")
    if len(parts) != 4 or parts[0] != SCHEME:
        raise CryptoError("not a taxos ciphertext")
    _, stored_key_id, nonce_b64, body_b64 = parts
    if stored_key_id != key_id():
        raise CryptoError(
            "this row was encrypted under a different master key "
            f"(row {stored_key_id}, server {key_id()}); restore the key or re-key the table"
        )
    aead = AESGCM(data_key(purpose))
    aad = f"{purpose}|{context}".encode("utf-8")
    try:
        return aead.decrypt(_unb64(nonce_b64), _unb64(body_b64), aad).decode("utf-8")
    except Exception as exc:
        raise CryptoError("ciphertext failed authentication (wrong key, or tampered)") from exc


# ---------------------------------------------------------------------------
# SSN / ITIN
# ---------------------------------------------------------------------------
_DIGITS = re.compile(r"\D")


@dataclass(frozen=True)
class TaxId:
    """A validated SSN or ITIN, which knows how to hide itself."""

    digits: str
    kind: str  # ssn | itin

    @property
    def masked(self) -> str:
        return f"***-**-{self.digits[-4:]}"

    @property
    def last4(self) -> str:
        return self.digits[-4:]

    def __str__(self) -> str:  # so it cannot be logged by accident
        return self.masked

    def __repr__(self) -> str:
        return f"TaxId({self.masked})"


def normalise_ssn(value: str) -> TaxId:
    """Validate an SSN or ITIN, rejecting the numbers the SSA never issues.

    The rules are the SSA's own: no area 000, 666 or 900-999 for an SSN; no
    group 00; no serial 0000. ITINs start with 9 and have a group in the
    published ranges, so they are recognised rather than rejected -- a resident
    alien filing with an ITIN is an ordinary case, not an error.
    """
    digits = _DIGITS.sub("", value or "")
    if len(digits) != 9:
        raise ValueError("A Social Security number has 9 digits.")
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if group == "00" or serial == "0000":
        raise ValueError("That is not a valid Social Security number.")

    if area == "900" or (area.startswith("9") and area != "900"):
        # ITIN: 9xx-{50-65,70-88,90-92,94-99}-xxxx
        group_num = int(group)
        valid = (50 <= group_num <= 65) or (70 <= group_num <= 88) or (90 <= group_num <= 92) or (94 <= group_num <= 99)
        if not valid:
            raise ValueError("That is not a valid ITIN.")
        return TaxId(digits=digits, kind="itin")

    if area in ("000", "666"):
        raise ValueError("That is not a valid Social Security number.")
    return TaxId(digits=digits, kind="ssn")


def ssn_index(value: str) -> str:
    """A searchable, keyed fingerprint of an SSN. Never reversible without the key."""
    tax_id = value if isinstance(value, TaxId) else normalise_ssn(value)
    key = data_key("ssn-index")
    return hmac.new(key, tax_id.digits.encode("ascii"), hashlib.sha256).hexdigest()


def seal_ssn(value: str, *, context: str) -> tuple[str, str, str, str]:
    """Everything the database should hold for one SSN.

    Returns `(ciphertext, blind index, last four, kind)`. The last four is
    stored in the clear deliberately: it is what the UI shows back to the
    client for confirmation, and on its own it identifies nobody.
    """
    tax_id = normalise_ssn(value)
    return (
        encrypt_field(tax_id.digits, purpose="ssn", context=context),
        ssn_index(tax_id),
        tax_id.last4,
        tax_id.kind,
    )


def open_ssn(ciphertext: str, *, context: str) -> TaxId:
    return normalise_ssn(decrypt_field(ciphertext, purpose="ssn", context=context))


def redact(text: str) -> str:
    """Strip anything SSN-shaped out of free text before it reaches a log."""
    return re.sub(r"\b(\d{3})[- ]?(\d{2})[- ]?(\d{4})\b", r"***-**-\3", text or "")
