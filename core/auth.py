"""
JWT authentication backed by the `users` table (issue #104).

Identity flow:
  * `authenticate(u, p)` looks up the user, verifies bcrypt, refuses
    disabled accounts, and returns a `CurrentUser`.
  * `create_token(user)` issues a JWT carrying user_id, username, role,
    and the user's current `token_version`.
  * `verify_token(t)` decodes + validates the signature/expiry and the
    presence of required identity claims, returning the payload.
  * `load_current_user(t)` re-loads the row referenced by the token and
    rejects tokens whose `token_version` is stale or whose user has been
    disabled — this is the dependency used by protected endpoints.

Conversation- and memory-level scoping by user_id are out of scope for
#104; they are addressed in #105 / #106.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from dotenv import load_dotenv

from core import users as _users

load_dotenv()

logger = logging.getLogger(__name__)

_secret_key_env = os.getenv("NOVA_SECRET_KEY")
if _secret_key_env is None:
    logger.warning(
        "NOVA_SECRET_KEY is not set; using a temporary secret key. "
        "Sessions will be invalidated on restart."
    )
    SECRET_KEY = secrets.token_hex(32)
else:
    SECRET_KEY = _secret_key_env

TOKEN_EXPIRY_HOURS = 24
JWT_ALGORITHM = "HS256"
_REQUIRED_CLAIMS = ("sub", "username", "role", "tv")


@dataclass(frozen=True)
class CurrentUser:
    """Identity object resolved from a valid JWT for a non-disabled user."""

    id: int
    username: str
    role: str
    token_version: int
    is_restricted: bool


def _open_conn() -> sqlite3.Connection:
    # Late import: core.memory wires up the entire data layer, and importing
    # it at module level pulls in heavier deps than `auth` actually needs.
    from core.memory import DB_PATH
    return sqlite3.connect(DB_PATH)


def _user_from_row(row: sqlite3.Row) -> CurrentUser:
    return CurrentUser(
        id=int(row["id"]),
        username=row["username"],
        role=row["role"],
        token_version=int(row["token_version"]),
        is_restricted=bool(row["is_restricted"]),
    )


def _check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def authenticate(username: str, password: str) -> Optional[CurrentUser]:
    """
    Verify (username, password) against the users table.

    Returns the user on success; None for unknown user, wrong password,
    or a disabled account (`disabled_at IS NOT NULL`).
    """
    if not username or not password:
        return None
    try:
        with _open_conn() as conn:
            row = _users.get_user_by_username(conn, username)
    except sqlite3.DatabaseError:
        return None
    if row is None or row["disabled_at"] is not None:
        return None
    if not _check_password(password, row["password_hash"]):
        return None
    return _user_from_row(row)


def create_token(user: CurrentUser) -> str:
    """Issue a JWT carrying the user's identity, role and token version."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(hours=TOKEN_EXPIRY_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT signed by Nova.

    Returns the payload on success, or None on expiry, signature failure,
    malformed input, or a payload missing any required identity claim.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
    if not all(k in payload for k in _REQUIRED_CLAIMS):
        return None
    return payload


def load_current_user(token: str) -> Optional[CurrentUser]:
    """
    Validate `token` and confirm the underlying user still exists, is not
    disabled, and has the same `token_version` as the token claims.

    A bumped `token_version` (e.g. after a password reset or revocation)
    immediately invalidates every previously-issued token for that user.
    """
    payload = verify_token(token)
    if payload is None:
        return None
    try:
        with _open_conn() as conn:
            row = _users.get_user_by_username(conn, payload["username"])
    except sqlite3.DatabaseError:
        return None
    if row is None or row["disabled_at"] is not None:
        return None
    try:
        if int(row["id"]) != int(payload["sub"]):
            return None
        if int(row["token_version"]) != int(payload["tv"]):
            return None
    except (TypeError, ValueError):
        return None
    return _user_from_row(row)
