"""Encryption at rest for users' API keys.

Keys are sealed with Fernet (AES-128-CBC + HMAC-SHA256) under one master key
from `PHONEPILOT_MASTER_KEY`. The database only ever stores ciphertext plus a
non-secret hint ("pck_…56cc"); plaintext lives in memory just long enough to
build an HTTP client.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

ENV_MASTER_KEY = "PHONEPILOT_MASTER_KEY"


class SecretBox:
    def __init__(self, master_key: str | None = None):
        key = master_key or os.environ.get(ENV_MASTER_KEY)
        if not key:
            raise ValueError(
                f"{ENV_MASTER_KEY} is not set. Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def seal(self, plaintext: str) -> bytes:
        return self._f.encrypt(plaintext.encode("utf-8"))

    def open(self, ciphertext: bytes) -> str:
        try:
            return self._f.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("stored secret cannot be decrypted with the current master key") from exc

    @staticmethod
    def generate_master_key() -> str:
        return Fernet.generate_key().decode()


def hint(secret: str) -> str:
    """Non-secret display form: first 5 and last 4 characters."""
    s = secret.strip()
    if len(s) <= 12:
        return "•" * len(s)
    return f"{s[:5]}…{s[-4:]}"
