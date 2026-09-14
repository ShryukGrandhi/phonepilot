"""SQLite persistence for the multi-user service.

One file, WAL mode, a process-wide lock around writes. Every table that holds
user data carries `user_id`, and every query that reads user data takes the
user id as a parameter: there is no code path that lists phones, runs or keys
across users except the admin invite table.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash BLOB NOT NULL, pw_salt BLOB NOT NULL,
  created REAL NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY, created_by TEXT, created REAL NOT NULL, used_by TEXT, used REAL
);
CREATE TABLE IF NOT EXISTS web_sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, ip TEXT
);
CREATE INDEX IF NOT EXISTS web_sessions_user ON web_sessions(user_id);
CREATE TABLE IF NOT EXISTS user_keys (
  user_id TEXT NOT NULL, provider TEXT NOT NULL, enc BLOB NOT NULL, hint TEXT NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY (user_id, provider)
);
CREATE TABLE IF NOT EXISTS phones (
  session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created REAL NOT NULL, expires_at REAL,
  ended REAL, ready_at REAL, transport TEXT NOT NULL DEFAULT 'http'
);
CREATE INDEX IF NOT EXISTS phones_user ON phones(user_id);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL, task TEXT NOT NULL, dir TEXT NOT NULL,
  started REAL NOT NULL, ended REAL, success INTEGER, summary TEXT, result TEXT, steps INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_user ON runs(user_id, started);
CREATE TABLE IF NOT EXISTS audit (
  t REAL NOT NULL, user_id TEXT, action TEXT NOT NULL, detail TEXT
);
"""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    is_admin: bool
    disabled: bool
    created: float


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _x(self, sql: str, *args: Any) -> None:
        with self._lock:
            self._conn.execute(sql, args)

    # --------------------------------------------------------------- users
    def user_count(self) -> int:
        return int(self._q("SELECT COUNT(*) AS n FROM users")[0]["n"])

    def create_user(self, email: str, pw_hash: bytes, pw_salt: bytes, is_admin: bool = False) -> User:
        uid = "u_" + secrets.token_urlsafe(12)
        now = time.time()
        self._x("INSERT INTO users(id,email,pw_hash,pw_salt,created,is_admin) VALUES(?,?,?,?,?,?)",
                uid, email.lower(), pw_hash, pw_salt, now, int(is_admin))
        return User(uid, email.lower(), is_admin, False, now)

    def user_by_email(self, email: str) -> tuple[User, bytes, bytes] | None:
        rows = self._q("SELECT * FROM users WHERE email=?", email.lower())
        if not rows:
            return None
        r = rows[0]
        return User(r["id"], r["email"], bool(r["is_admin"]), bool(r["disabled"]), r["created"]), r["pw_hash"], r["pw_salt"]

    def user_by_id(self, uid: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE id=?", uid)
        if not rows:
            return None
        r = rows[0]
        return User(r["id"], r["email"], bool(r["is_admin"]), bool(r["disabled"]), r["created"])

    # ------------------------------------------------------------- invites
    def create_invite(self, created_by: str | None) -> str:
        code = secrets.token_urlsafe(9)
        self._x("INSERT INTO invites(code,created_by,created) VALUES(?,?,?)", code, created_by, time.time())
        return code

    def consume_invite(self, code: str, used_by: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE invites SET used_by=?, used=? WHERE code=? AND used_by IS NULL", (used_by, time.time(), code)
            )
            return cur.rowcount == 1

    def invite_is_open(self, code: str) -> bool:
        return bool(self._q("SELECT 1 FROM invites WHERE code=? AND used_by IS NULL", code))

    # -------------------------------------------------------- web sessions
    def add_web_session(self, token_hash: str, user_id: str, ttl_s: float, ip: str | None) -> None:
        now = time.time()
        self._x("INSERT INTO web_sessions(token_hash,user_id,created,expires,ip) VALUES(?,?,?,?,?)",
                token_hash, user_id, now, now + ttl_s, ip)

    def user_for_web_session(self, token_hash: str) -> User | None:
        rows = self._q("SELECT user_id, expires FROM web_sessions WHERE token_hash=?", token_hash)
        if not rows or rows[0]["expires"] < time.time():
            return None
        user = self.user_by_id(rows[0]["user_id"])
        return None if user is None or user.disabled else user

    def delete_web_session(self, token_hash: str) -> None:
        self._x("DELETE FROM web_sessions WHERE token_hash=?", token_hash)

    def delete_user_web_sessions(self, user_id: str) -> None:
        self._x("DELETE FROM web_sessions WHERE user_id=?", user_id)

    # ---------------------------------------------------------------- keys
    def set_key(self, user_id: str, provider: str, enc: bytes, hint: str) -> None:
        self._x("INSERT INTO user_keys(user_id,provider,enc,hint,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,provider) DO UPDATE SET enc=excluded.enc, hint=excluded.hint, updated=excluded.updated",
                user_id, provider, enc, hint, time.time())

    def get_key(self, user_id: str, provider: str) -> bytes | None:
        rows = self._q("SELECT enc FROM user_keys WHERE user_id=? AND provider=?", user_id, provider)
        return rows[0]["enc"] if rows else None

    def key_hints(self, user_id: str) -> dict[str, str]:
        return {r["provider"]: r["hint"] for r in self._q("SELECT provider, hint FROM user_keys WHERE user_id=?", user_id)}

    def delete_key(self, user_id: str, provider: str) -> None:
        self._x("DELETE FROM user_keys WHERE user_id=? AND provider=?", user_id, provider)

    # -------------------------------------------------------------- phones
    def add_phone(self, session_id: str, user_id: str, expires_at: float | None, transport: str) -> None:
        self._x("INSERT OR REPLACE INTO phones(session_id,user_id,created,expires_at,transport) VALUES(?,?,?,?,?)",
                session_id, user_id, time.time(), expires_at, transport)

    def phone_owner(self, session_id: str) -> str | None:
        rows = self._q("SELECT user_id FROM phones WHERE session_id=?", session_id)
        return rows[0]["user_id"] if rows else None

    def mark_phone_ready(self, session_id: str, expires_at: float | None) -> None:
        self._x("UPDATE phones SET ready_at=?, expires_at=? WHERE session_id=?", time.time(), expires_at, session_id)

    def end_phone(self, session_id: str) -> None:
        self._x("UPDATE phones SET ended=? WHERE session_id=? AND ended IS NULL", time.time(), session_id)

    def open_phones(self, user_id: str) -> list[str]:
        return [r["session_id"] for r in self._q("SELECT session_id FROM phones WHERE user_id=? AND ended IS NULL", user_id)]

    def phone_minutes_today(self, user_id: str) -> float:
        day_start = time.time() - 86400
        total = 0.0
        for r in self._q("SELECT ready_at, ended, expires_at FROM phones WHERE user_id=? AND ready_at IS NOT NULL "
                         "AND (ended IS NULL OR ended > ?)", user_id, day_start):
            end = r["ended"] or min(time.time(), r["expires_at"] or time.time())
            total += max(0.0, end - max(r["ready_at"], day_start)) / 60.0
        return total

    # ---------------------------------------------------------------- runs
    def add_run(self, user_id: str, session_id: str, task: str, run_dir: str) -> str:
        rid = "r_" + secrets.token_urlsafe(10)
        self._x("INSERT INTO runs(id,user_id,session_id,task,dir,started) VALUES(?,?,?,?,?,?)",
                rid, user_id, session_id, task, run_dir, time.time())
        return rid

    def finish_run(self, rid: str, success: bool | None, summary: str, result: str | None, steps: int) -> None:
        self._x("UPDATE runs SET ended=?, success=?, summary=?, result=?, steps=? WHERE id=?",
                time.time(), None if success is None else int(success), summary, result, steps, rid)

    def runs_for_user(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM runs WHERE user_id=? ORDER BY started DESC LIMIT ?", user_id, limit)]

    def run_dir_owned(self, user_id: str, run_dir_name: str) -> bool:
        return bool(self._q("SELECT 1 FROM runs WHERE user_id=? AND dir=?", user_id, run_dir_name))

    # --------------------------------------------------------------- audit
    def audit(self, user_id: str | None, action: str, detail: str = "") -> None:
        self._x("INSERT INTO audit(t,user_id,action,detail) VALUES(?,?,?,?)", time.time(), user_id, action, detail[:500])

    def audit_tail(self, n: int = 50) -> Iterator[dict[str, Any]]:
        for r in self._q("SELECT * FROM audit ORDER BY t DESC LIMIT ?", n):
            yield dict(r)
