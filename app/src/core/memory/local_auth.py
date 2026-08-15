"""Password handling for the local-development simulation only."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets


USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,31}$")
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_HASH_VERSION = "v1"


def normalize_username(value: str) -> str:
    username = str(value or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(
            "Username must be 3-32 characters and use letters, numbers, dot, underscore, or hyphen."
        )
    return username.casefold()


def validate_password(value: str) -> str:
    password = str(value or "")
    if len(password) < 8 or len(password) > 128:
        raise ValueError("Password must be between 8 and 128 characters.")
    return password


def hash_password(password: str) -> str:
    secret = validate_password(password).encode("utf-8")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return (
        f"scrypt${_HASH_VERSION}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, version, n, r, p, salt_hex, expected_hex = str(encoded).split("$", 6)
        if algorithm != "scrypt" or version != _HASH_VERSION:
            return False
        actual = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        expected = bytes.fromhex(expected_hex)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)
