"""Primitivas locais de autenticação, senha e sessão para o Prospector."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


PASSWORD_ITERATIONS = 310_000
SESSION_DAYS = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 10:
        raise ValueError("A senha precisa ter pelo menos 10 caracteres.")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_session(connection, user_id: int) -> tuple[str, str]:
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    created = utc_now()
    expires = created + timedelta(days=SESSION_DAYS)
    connection.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, user_id, created.isoformat(), expires.isoformat()),
    )
    return token, expires.isoformat()


def get_session_user(connection, token: str | None) -> dict[str, Any] | None:
    if not token or len(token) > 256:
        return None
    token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    row = connection.execute(
        """SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id
           WHERE s.token_hash=?""",
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) <= utc_now():
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            connection.commit()
            return None
    except ValueError:
        return None
    return dict(row)


def monthly_window(started_at: str, now: datetime | None = None) -> tuple[str, int]:
    """Calcula início e número de períodos de 30 dias decorridos."""
    now = now or utc_now()
    try:
        start = datetime.fromisoformat(started_at)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        start = now
    if now < start:
        return now.isoformat(), 0
    periods = int((now - start).total_seconds() // (30 * 24 * 60 * 60))
    next_start = start + timedelta(days=30 * periods)
    return next_start.isoformat(), periods
