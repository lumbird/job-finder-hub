"""Encryption at rest for tokens, keys and the TOTP secret (Fernet, key from `.env`)."""

from __future__ import annotations

from cryptography.fernet import Fernet


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
