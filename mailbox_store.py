"""Local plaintext mailbox and wake claims; routing assertions are not authentication."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
MAX_REQUEST_BYTES = 8192
INBOX_RESPONSE_BYTES = 32768
MAX_PAGE = 100
WAKE_STATES = {"pending", "dispatching", "submitted", "queued", "acknowledged", "refused", "unknown"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def bounded_int(value, name, minimum=1, maximum=MAX_PAGE):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def message_uuid(value):
    try:
        if isinstance(value, str) and len(value) == 36 and str(uuid.UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise ValueError("message_id must be an exact canonical UUID")


def check_address(profile, session):
    if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
        raise ValueError("invalid exact profile name")
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        raise ValueError("invalid exact stored session ID")


def store_paths(db_path, hermes_root):
    """Validate the operator settings shared by every store at the mailbox location."""
    db_path, hermes_root = Path(db_path), Path(hermes_root)
    if not db_path.is_absolute() or not hermes_root.is_absolute():
        raise ValueError("session-mailbox db_path and hermes_root settings must be absolute paths")
    db_path, hermes_root = db_path.resolve(), hermes_root.resolve()
    if not hermes_root.is_dir():
        raise ValueError("session-mailbox hermes_root setting must be an existing Hermes root directory")
    if hermes_root.parent.name == "profiles":
        raise ValueError("session-mailbox hermes_root setting must be the global root, not a profile home")
    if db_path.name.lower() == "state.db" or db_path.is_relative_to(hermes_root / "profiles"):
        raise ValueError("mailbox DB must not be Hermes state.db or profile-local")
    return db_path, hermes_root


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class _Store:
    """One WAL SQLite file holding only this store's tables."""
    TABLES = frozenset()
    SCHEMA = ""

    def _open(self, db_path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite's journal-mode transition can return BUSY immediately despite
        # busy_timeout when two new processes upgrade the same empty database.
        deadline = time.monotonic() + 3
        while True:
            try:
                self._initialize()
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _initialize(self):
        with self._connection(timeout=0.2) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - self.TABLES:
                raise ValueError("refusing to use a database containing unrelated tables")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ValueError("unsupported session-mailbox schema version")
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() != "wal":
                mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("session-mailbox requires SQLite WAL")
            db.executescript("BEGIN IMMEDIATE;" + self.SCHEMA + "PRAGMA user_version=1; COMMIT;")

    @contextmanager
    def _connection(self, write=False, timeout=3):
        db = sqlite3.connect(self.db_path, timeout=timeout, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            db.execute("PRAGMA foreign_keys=ON")
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


class Mailbox(_Store):
    TABLES = frozenset({"messages", "notice_claims", "sqlite_sequence"})
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            sender_profile TEXT NOT NULL, sender_session TEXT NOT NULL,
            recipient_profile TEXT NOT NULL, recipient_session TEXT NOT NULL,
            body TEXT NOT NULL, key TEXT NOT NULL, reply_to TEXT,
            created_at TEXT NOT NULL, acknowledged_at TEXT,
            UNIQUE(sender_profile, sender_session, key)
        );
        CREATE INDEX IF NOT EXISTS recipient_inbox ON messages
            (recipient_profile, recipient_session, acknowledged_at, seq);
        CREATE TABLE IF NOT EXISTS notice_claims (
            message_id TEXT NOT NULL REFERENCES messages(id),
            channel TEXT NOT NULL, claimed_at TEXT NOT NULL,
            PRIMARY KEY(message_id, channel)
        );
    """

    def __init__(self, db_path, hermes_root):
        db_path, self.hermes_root = store_paths(db_path, hermes_root)
        self._open(db_path)

    def _profiles(self):
        homes = {"default": self.hermes_root}
        folder = self.hermes_root / "profiles"
        if folder.is_dir():
            for home in sorted(folder.iterdir()):
                if (PROFILE_RE.fullmatch(home.name) and home.name != "default" and home.is_dir()
                        and not home.is_symlink() and not (folder / ".deleted" / home.name).exists()
                        and any((home / marker).is_file() or (home / marker).is_symlink()
                                for marker in IDENTITY_MARKERS)):
                    homes[home.name] = home
        return homes

    @staticmethod
    def _registry(home):
        path = home / "runtime/active_sessions.json"
        if not path.exists():
            return []
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("active session registry exceeds safe read bound")
        with path.open(encoding="utf-8-sig") as source:
            payload = json.load(source)
        entries = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError("invalid active session registry shape")
        return entries

    @contextmanager
    def _session_db(self, home):
        # mode=ro prevents creating a missing DB; never open transcripts or use Hermes APIs.
        db = sqlite3.connect((home / "state.db").resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    def _address(self, profile, session):
        check_address(profile, session)
        home = self._profiles().get(profile)
        if home is None:
            raise ValueError("unknown existing profile")
        if any(entry.get("session_id") == session for entry in self._registry(home)):
            return profile, session
        if (home / "state.db").is_file():
            with self._session_db(home) as db:
                if db.execute("SELECT 1 FROM sessions WHERE id=?", (session,)).fetchone():
                    return profile, session
        raise ValueError("unknown stored session ID (must exist in registry or sessions metadata)")

    @staticmethod
    def _message(row, include_body=True, include_recipient=True):
        keys = (("id", "sender_profile", "sender_session")
                + (("recipient_profile", "recipient_session") if include_recipient else ())
                + ("reply_to", "created_at", "acknowledged_at"))
        result = {key: row[key] for key in keys}
        result["status"] = "acknowledged" if row["acknowledged_at"] else "queued"
        if include_body:
            result["body"] = row["body"]
        return result

    def send(self, sender_profile, sender_session, recipient_profile, recipient_session, body, key, reply_to=None):
        """Store once; the receipt never echoes the body (read it through inbox)."""
        self._address(sender_profile, sender_session)
        self._address(recipient_profile, recipient_session)
        if not isinstance(body, str) or not body or len(body.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("body must be nonempty UTF-8 text within 8192 bytes")
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) == 127 for c in key):
            raise ValueError("key must have 1..128 characters, with no spaces/control characters")
        request = dict(sender_profile=sender_profile, sender_session=sender_session,
                       recipient_profile=recipient_profile, recipient_session=recipient_session,
                       body=body, key=key, reply_to=reply_to)
        if len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("send request exceeds 8192 UTF-8 bytes including routing metadata")
        if reply_to is not None:
            message_uuid(reply_to)
        with self._connection(write=True) as db:
            previous = db.execute("SELECT * FROM messages WHERE sender_profile=? AND sender_session=? AND key=?",
                                  (sender_profile, sender_session, key)).fetchone()
            if previous is not None:
                if any(previous[field] != request[field] for field in
                       ("recipient_profile", "recipient_session", "body", "reply_to")):
                    raise ValueError("idempotency key conflict: recipient/body/reply_to differ")
                return {**self._message(previous, False), "deduplicated": True}
            if reply_to is not None:
                parent = db.execute("SELECT * FROM messages WHERE id=?", (reply_to,)).fetchone()
                peers = {(sender_profile, sender_session), (recipient_profile, recipient_session)}
                if parent is None or {(parent["sender_profile"], parent["sender_session"]),
                                      (parent["recipient_profile"], parent["recipient_session"])} != peers:
                    raise ValueError("reply_to must name an existing message between these exact peers")
            identifier = str(uuid.uuid4())
            db.execute("""INSERT INTO messages
                (id, sender_profile, sender_session, recipient_profile, recipient_session, body, key, reply_to, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identifier, sender_profile, sender_session, recipient_profile, recipient_session,
                 body, key, reply_to, utc_now()))
            return {**self._message(db.execute("SELECT * FROM messages WHERE id=?", (identifier,)).fetchone(), False),
                    "deduplicated": False}

    def inbox(self, profile, session, limit=20, after=None):
        """Pending messages, oldest first: whole messages within INBOX_RESPONSE_BYTES of JSON."""
        self._address(profile, session)
        bounded_int(limit, "limit")
        with self._connection() as db:
            seq = 0
            if after is not None:
                message_uuid(after)
                cursor = db.execute("SELECT seq FROM messages WHERE id=? AND recipient_profile=? AND recipient_session=?",
                                    (after, profile, session)).fetchone()
                if cursor is None:
                    raise ValueError("inbox cursor is not a message for this recipient")
                seq = cursor["seq"]
            rows = db.execute("""SELECT * FROM messages WHERE recipient_profile=? AND recipient_session=?
                AND acknowledged_at IS NULL AND seq>? ORDER BY seq LIMIT ?""", (profile, session, seq, limit + 1)).fetchall()
        result = {"recipient_profile": profile, "recipient_session": session, "messages": [],
                  "has_more": False, "next_cursor": None, "peer_content_untrusted": True}
        # Envelope upper bound: "false" and a quoted UUID are the longest has_more/cursor forms.
        used = _json_bytes({**result, "next_cursor": "0" * 36})
        for row in rows[:limit]:
            message = self._message(row, include_recipient=False)
            used += _json_bytes(message) + 2  # ", " list separator; over-counts the first
            if used > INBOX_RESPONSE_BYTES:
                if not result["messages"]:
                    raise ValueError("stored message exceeds the inbox response byte budget")
                break
            result["messages"].append(message)
        if len(result["messages"]) < len(rows):
            result["has_more"], result["next_cursor"] = True, result["messages"][-1]["id"]
        return result

    def ack(self, profile, session, message_ids):
        self._address(profile, session)
        if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= MAX_PAGE:
            raise ValueError("message_ids must be a list of 1..100 exact UUIDs")
        identifiers = list(dict.fromkeys(message_uuid(item) for item in message_ids))
        with self._connection(write=True) as db:
            rows = []
            for identifier in identifiers:
                row = db.execute("SELECT id, recipient_profile, recipient_session, acknowledged_at "
                                 "FROM messages WHERE id=?", (identifier,)).fetchone()
                if row is None or (row["recipient_profile"], row["recipient_session"]) != (profile, session):
                    raise ValueError("ACK requires an existing message for the supplied exact recipient")
                rows.append(row)
            now = utc_now()
            for row in rows:
                db.execute("UPDATE messages SET acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=?",
                           (now, row["id"]))
        receipts = [{"id": row["id"], "status": "acknowledged", "acknowledged_at": row["acknowledged_at"] or now}
                    for row in rows]
        return {"messages": receipts, "acknowledged_count": len(receipts)}

    def status(self, message_id):
        message_uuid(message_id)
        with self._connection() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ValueError("unknown message ID")
        return self._message(row, False)

    def claim_notice(self, profile, session, channel, limit=20):
        """Durably claim pending-message notices once per message/channel.

        This is an internal hook API, not an MCP tool. Claiming is at-most-once
        admission, not evidence that a notification arrived or a wake succeeded.
        """
        self._address(profile, session)
        bounded_int(limit, "limit")
        if not isinstance(channel, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", channel):
            raise ValueError("channel must be an exact lowercase notice channel of 1..64 characters")
        with self._connection(write=True) as db:
            rows = db.execute("""SELECT m.* FROM messages AS m
                WHERE m.recipient_profile=? AND m.recipient_session=?
                AND m.acknowledged_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM notice_claims AS n WHERE n.message_id=m.id AND n.channel=?)
                ORDER BY m.seq LIMIT ?""", (profile, session, channel, limit)).fetchall()
            now = utc_now()
            for row in rows:
                db.execute("INSERT INTO notice_claims(message_id, channel, claimed_at) VALUES (?, ?, ?)",
                           (row["id"], channel, now))
            return [{**self._message(row, False), "notice_channel": channel, "notice_claimed_at": now}
                    for row in rows]

    def list_sessions(self, profile=None, include_recent=True, limit=20, offset=0,
                      include_subagents=False, query=None, detailed=False):
        bounded_int(limit, "limit")
        bounded_int(offset, "offset", 0, 10000)
        if not isinstance(include_recent, bool):
            raise ValueError("include_recent must be boolean")
        if not isinstance(include_subagents, bool) or not isinstance(detailed, bool):
            raise ValueError("include_subagents and detailed must be boolean")
        if query is not None and (not isinstance(query, str) or len(query) > 256):
            raise ValueError("query must be a string within 256 characters")
        homes = self._profiles()
        if profile is not None:
            if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile) or profile not in homes:
                raise ValueError("unknown exact profile name")
            homes = {profile: homes[profile]}
        sessions, warnings = {}, []
        for name, home in homes.items():
            try:
                registry = self._registry(home)
            except (OSError, ValueError) as exc:
                warnings.append({"profile": name, "source": "registry", "error": str(exc)[:256]})
                registry = []
            for entry in registry:
                identifier = entry.get("session_id")
                if not isinstance(identifier, str) or not SESSION_RE.fullmatch(identifier):
                    warnings.append({"profile": name, "source": "registry", "error": "invalid stored session ID skipped"})
                    continue
                # `liveness` is the single activity hint; it never proves running, absent or idle.
                sessions[(name, identifier)] = {
                    "profile": name, "session": identifier, "liveness": "unverified_registry_hint",
                    "surface": str(entry.get("surface") or "unknown")[:64],
                    "last_active": entry.get("updated_at", entry.get("started_at")),
                }
            if include_recent and (home / "state.db").is_file():
                try:
                    with self._session_db(home) as db:
                        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
                        # Only schema-known metadata columns, never SELECT * or transcript/message tables.
                        selected = [column for column in ("id", "title", "source", "started_at", "last_activity_at") if column in columns]
                        if "id" not in selected:
                            raise ValueError("sessions metadata table has no id column")
                        order = "COALESCE(last_activity_at, started_at) DESC, id" if "last_activity_at" in selected and "started_at" in selected else ("started_at DESC, id" if "started_at" in selected else "id")
                        where = " WHERE (source IS NULL OR source != 'subagent')" if not include_subagents and "source" in selected else ""
                        rows = db.execute("SELECT " + ",".join(selected) + " FROM sessions" + where + " ORDER BY " + order + " LIMIT ?",
                                          (MAX_PAGE,)).fetchall()
                    for row in rows:
                        identifier = row["id"]
                        if not isinstance(identifier, str) or not SESSION_RE.fullmatch(identifier):
                            continue
                        item = sessions.setdefault((name, identifier), {
                            "profile": name, "session": identifier, "liveness": "not_in_registry",
                        })
                        for column, bound in (("title", 256), ("source", 64)):
                            if column in row.keys() and row[column] is not None:
                                item[column] = str(row[column])[:bound]
                        if "started_at" in row.keys():
                            item["started_at"] = row["started_at"]
                        activity = [item.get("last_active"), item.get("started_at")]
                        if "last_activity_at" in row.keys():
                            activity.append(row["last_activity_at"])
                        item["last_active"] = max((v for v in activity if isinstance(v, (int, float))), default=0)
                except (OSError, ValueError, sqlite3.Error) as exc:
                    warnings.append({"profile": name, "source": "sessions_metadata", "error": str(exc)[:256]})
        ordered = [item for item in sessions.values()
                   if (include_subagents or (item.get("source") != "subagent" and "subagent" not in item.get("surface", "")))
                   and (not query or query.casefold() in " ".join(str(item.get(k, "")) for k in ("profile", "session", "title")).casefold())]
        def recent(item):
            value = item.get("last_active") or item.get("started_at")
            return float(value) if isinstance(value, (int, float)) else 0
        ordered.sort(key=lambda item: (-recent(item), item["profile"], item["session"]))
        page = ordered[offset:offset + limit]
        if not detailed:
            page = [{k: item[k] for k in ("profile", "session", "title") if k in item} for item in page]
        has_more = len(ordered) > offset + limit
        grouped = {}
        for warning in warnings:
            key = (warning["profile"], warning["source"], warning["error"])
            grouped.setdefault(key, {**warning, "count": 0})["count"] += 1
        return {"sessions": page, "has_more": has_more, "next_offset": offset + limit if has_more else None,
                "warnings": list(grouped.values())[:20], "warning_count": len(warnings),
                "warning_group_count": len(grouped),
                "recent_per_profile_limit": MAX_PAGE if include_recent else None}


class WakeStore(_Store):
    """Durable at-most-once automatic dispatch claims, separate from mailbox ACKs."""
    TABLES = frozenset({"wakes"})
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS wakes (
            message_id TEXT PRIMARY KEY, recipient_profile TEXT NOT NULL,
            recipient_session TEXT NOT NULL, state TEXT NOT NULL,
            owner_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', receipt TEXT
        );
        CREATE INDEX IF NOT EXISTS pending_recipient ON wakes
            (recipient_profile, recipient_session, state, created_at);
    """

    def __init__(self, mailbox_db_path, hermes_root):
        source, _root = store_paths(mailbox_db_path, hermes_root)
        self._open(source.with_name(source.stem + ".wake" + source.suffix))

    @staticmethod
    def _result(row):
        if row is None:
            return None
        value = dict(row)
        value["receipt"] = json.loads(value["receipt"]) if value["receipt"] else None
        return value

    def request(self, message):
        mid = message_uuid(message["id"])
        profile, session = message["recipient_profile"], message["recipient_session"]
        check_address(profile, session)
        stamp = utc_now()
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
        check_address(profile, session)
        bounded_int(limit, "limit")
        with self._connection() as db:
            return [self._result(row) for row in db.execute(
                "SELECT * FROM wakes WHERE recipient_profile=? AND recipient_session=? AND state='pending' "
                "ORDER BY created_at,message_id LIMIT ?", (profile, session, limit))]

    def claim(self, mid, owner_key):
        mid = message_uuid(mid)
        if not isinstance(owner_key, str) or not 1 <= len(owner_key) <= 384:
            raise ValueError("bounded owner identity required")
        with self._connection(write=True) as db:
            return db.execute("UPDATE wakes SET state='dispatching',owner_key=?,updated_at=? "
                              "WHERE message_id=? AND state='pending'", (owner_key, utc_now(), mid)).rowcount == 1

    def finish(self, mid, state, detail="", receipt=None):
        mid = message_uuid(mid)
        if state not in WAKE_STATES - {"pending", "dispatching"}:
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
                       (state, detail, encoded, utc_now(), mid))
            return self._result(db.execute("SELECT * FROM wakes WHERE message_id=?", (mid,)).fetchone())

    def status(self, mid):
        mid = message_uuid(mid)
        with self._connection() as db:
            return self._result(db.execute("SELECT * FROM wakes WHERE message_id=?", (mid,)).fetchone())
