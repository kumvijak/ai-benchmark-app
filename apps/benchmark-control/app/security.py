from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return f"pbkdf2_sha256${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, digest_b64 = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
    expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return hmac.compare_digest(actual, expected)


def create_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class SecretBox:
    app_secret: str

    def _fernet(self) -> Fernet:
        key = base64.urlsafe_b64encode(hashlib.sha256(self.app_secret.encode("utf-8")).digest())
        return Fernet(key)

    def encrypt(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self._fernet().decrypt(value.encode("ascii")).decode("utf-8")


def redact(text: str | None) -> str:
    if not text:
        return ""
    needles = ["PRIVATE KEY", "password", "passphrase", "Authorization:", "Bearer "]
    result = re.sub(r'\bhf_[A-Za-z0-9_]+\b', '[REDACTED]', text)
    for needle in needles:
        result = result.replace(needle, "[REDACTED]")
    return result[-8000:]

