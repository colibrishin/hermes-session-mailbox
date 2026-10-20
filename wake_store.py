"""Durable at-most-once automatic dispatch claims, separate from mailbox ACKs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

STATES = {"pending", "dispatching", "submitted", "queued", "acknowledged", "refused", "unknown"}
PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def message_id(value):
    if not isinstance(value, str) or len(value) != 36 or str(uuid.UUID(value)) != value:
        raise ValueError("message_id must be an exact canonical UUID")
    return value


def address(profile, session):
    if not isinstance(profile, str) or not PROFILE.fullmatch(profile):
        raise ValueError("invalid profile")
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        raise ValueError("invalid stored session ID")


class WakeStore:
    def __init__(self, mailbox_db_path, hermes_root):
        source, root = Path(mailbox_db_path), Path(hermes_root)
        if not source.is_absolute() or not root.is_absolute():
            raise ValueError("wake storage requires absolute operator-configured paths")
        source, root = source.resolve(), root.resolve()
        if not root.is_dir() or root.parent.name == "profiles":
            raise ValueError("global Hermes root required")
        if source.name.lower() == "state.db" or source.is_relative_to(root / "profiles"):
            raise ValueError("refusing conversation or profile-local storage")
        self.db_path = source.with_name(source.stem + ".wake" + source.suffix)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 3
        while True:
            try:
                self._initialize()
                break
            except sqlite3.OperationalError as exc:
                if (getattr(exc, "sqlite_errorcode", 0) & 0xff) not in (5, 6) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    @contextmanager
    def _connection(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with self._connection() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"wakes"} or db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ValueError("unrelated or unsupported wake database")
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                    raise RuntimeError("wake database requires WAL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS wakes (
                    message_id TEXT PRIMARY KEY, recipient_profile TEXT NOT NULL,
                    recipient_session TEXT NOT NULL, state TEXT NOT NULL,
                    owner_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '', receipt TEXT
                );
                CREATE INDEX IF NOT EXISTS pending_recipient ON wakes
                    (recipient_profile, recipient_session, state, created_at);
                PRAGMA user_version=1;
                COMMIT;
            """)

    @staticmethod
    def _result(row):
        if row is None:
            return None
        value = dict(row)
        value["receipt"] = json.loads(value["receipt"]) if value["receipt"] else None
        return value

    def request(self, message):
        mid = message_id(message["id"])
        profile, session = message["recipient_profile"], message["recipient_session"]
        address(profile, session)
        stamp = now()
        with self._connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO wakes "
                       "(message_id,recipient_profile,recipient_session,state,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?)", (mid, profile, session,
                       "acknowledged" if message.get("acknowledged_at") else "pending", stamp, stamp))
            row = db.execute("SELECT * FROM wakes WHERE message_id=?", (mid,)).fetchone()
            if (row["recipient_profile"], row["recipient_session"]) != (profile, session):
                raise ValueError("immutable message recipient mismatch")
            return self._result(row)

    def pending(self, profile, session, limit=20):
        address(profile, session)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        with self._connection() as db:
            return [self._result(row) for row in db.execute(
                "SELECT * FROM wakes WHERE recipient_profile=? AND recipient_session=? AND state='pending' "
                "ORDER BY created_at,message_id LIMIT ?", (profile, session, limit))]

    def claim(self, mid, owner_key):
        mid = message_id(mid)
        if not isinstance(owner_key, str) or not 1 <= len(owner_key) <= 384:
            raise ValueError("bounded owner identity required")
        with self._connection(write=True) as db:
            return db.execute("UPDATE wakes SET state='dispatching',owner_key=?,updated_at=? "
                              "WHERE message_id=? AND state='pending'", (owner_key, now(), mid)).rowcount == 1

    def finish(self, mid, state, detail="", receipt=None):
        mid = message_id(mid)
        if state not in STATES - {"pending", "dispatching"}:
            raise ValueError("invalid final wake state")
        if not isinstance(detail, str) or len(detail) > 512:
            raise ValueError("bounded detail required")
        if receipt is not None and not isinstance(receipt, dict):
            raise ValueError("receipt must be an object or None")
        encoded = json.dumps(receipt, ensure_ascii=False) if receipt is not None else None
        if encoded is not None and len(encoded.encode("utf-8")) > 2048:
            raise ValueError("receipt exceeds bound")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM wakes WHERE message_id=?", (mid,)).fetchone()
            if row is None:
                raise ValueError("unknown wake request")
            expected = "pending" if state == "acknowledged" else "dispatching"
            if row["state"] == state or (state == "acknowledged" and row["state"] != "pending"):
                return self._result(row)
            if row["state"] != expected:
                raise ValueError("refusing wake state regression or unclaimed submission")
            db.execute("UPDATE wakes SET state=?,detail=?,receipt=?,updated_at=? WHERE message_id=?",
                       (state, detail, encoded, now(), mid))
            return self._result(db.execute("SELECT * FROM wakes WHERE message_id=?", (mid,)).fetchone())

    def status(self, mid):
        mid = message_id(mid)
        with self._connection() as db:
            return self._result(db.execute("SELECT * FROM wakes WHERE message_id=?", (mid,)).fetchone())
