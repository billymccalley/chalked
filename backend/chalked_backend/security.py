from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone


PBKDF2_ROUNDS = 210_000
SESSION_DAYS = 30


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expires_iso(days: int = SESSION_DAYS) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def make_code() -> str:
    return secrets.token_urlsafe(5).replace("-", "").replace("_", "").upper()[:7]


def make_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False
