"""Accounts and browser sessions.

- passwords: scrypt (n=2^15, r=8, p=1) with a 16-byte per-user salt, constant-time compare
- browser sessions: 32 random bytes in an HttpOnly cookie; only the SHA-256 of the token is stored
- signup is invite-only; the first account becomes admin and can mint invites
- login attempts are rate-limited per (ip, email) in memory
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass

from .store import Store, User

COOKIE_NAME = "pp_session"
SESSION_TTL_S = 7 * 24 * 3600
MIN_PASSWORD_LEN = 10
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW_S = 600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Login:
    user: User
    token: str  # raw cookie value (never stored)


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class Auth:
    def __init__(self, store: Store):
        self.store = store
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------------------- signup
    def signup(self, email: str, password: str, invite: str | None, ip: str | None) -> Login:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError("enter a valid email address")
        if len(password) < MIN_PASSWORD_LEN:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
        first_user = self.store.user_count() == 0
        if not first_user:
            if not invite or not self.store.invite_is_open(invite):
                raise AuthError("a valid invite code is required")
        if self.store.user_by_email(email):
            raise AuthError("that email already has an account")
        salt = secrets.token_bytes(16)
        user = self.store.create_user(email, _hash_password(password, salt), salt, is_admin=first_user)
        if not first_user:
            if not self.store.consume_invite(invite or "", user.id):
                raise AuthError("invite code was just used by someone else")
        self.store.audit(user.id, "signup", f"admin={first_user} ip={ip}")
        return self._issue(user, ip)

    # --------------------------------------------------------------- login
    def login(self, email: str, password: str, ip: str | None) -> Login:
        email = email.strip().lower()
        key = f"{ip}|{email}"
        if self._too_many_attempts(key):
            raise AuthError("too many attempts; wait a few minutes")
        found = self.store.user_by_email(email)
        if not found:
            _hash_password(password, b"x" * 16)  # equalise timing for unknown emails
            self._note_attempt(key)
            raise AuthError("wrong email or password")
        user, pw_hash, salt = found
        if user.disabled or not hmac.compare_digest(_hash_password(password, salt), pw_hash):
            self._note_attempt(key)
            raise AuthError("wrong email or password")
        self.store.audit(user.id, "login", f"ip={ip}")
        return self._issue(user, ip)

    def logout(self, token: str | None) -> None:
        if token:
            self.store.delete_web_session(_token_hash(token))

    def user_for_token(self, token: str | None) -> User | None:
        if not token or len(token) < 20:
            return None
        return self.store.user_for_web_session(_token_hash(token))

    def create_invite(self, admin: User) -> str:
        if not admin.is_admin:
            raise AuthError("admins only")
        code = self.store.create_invite(admin.id)
        self.store.audit(admin.id, "invite.create")
        return code

    # ------------------------------------------------------------ internals
    def _issue(self, user: User, ip: str | None) -> Login:
        token = secrets.token_urlsafe(32)
        self.store.add_web_session(_token_hash(token), user.id, SESSION_TTL_S, ip)
        return Login(user, token)

    def _too_many_attempts(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._attempts.get(key, []) if now - t < ATTEMPT_WINDOW_S]
            self._attempts[key] = recent
            return len(recent) >= MAX_ATTEMPTS

    def _note_attempt(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(time.time())


def cookie_header(token: str, secure: bool, max_age: int = SESSION_TTL_S) -> str:
    parts = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
