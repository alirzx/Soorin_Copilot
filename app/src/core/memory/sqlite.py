"""Local-development SQLite adapters for chat and compact thread state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from src.core.identity import RequestIdentity, normalize_identifier
from src.core.memory.persistence import (
    LOCAL_SCHEMA_VERSION,
    MAX_CHAT_CONTENT_CHARS,
    MAX_CHAT_READ_LIMIT,
    MAX_CONVERSATION_TITLE_CHARS,
    MAX_THREAD_STATE_BYTES,
    THREAD_STATE_SCHEMA_VERSION,
    ThreadMemoryState,
    LocalChatMessage,
    LocalConversation,
    LocalPersistenceConflictError,
    LocalPersistenceError,
    LocalPersistenceOwnershipError,
    LocalPersistenceSchemaError,
    LocalRequestCommit,
    LocalUser,
    utc_now,
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_users (
    user_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES local_users(user_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_local_conversations_owner
ON local_conversations(user_id, updated_at DESC, conversation_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_local_conversations_session
ON local_conversations(session_id);

CREATE TABLE IF NOT EXISTS local_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES local_conversations(conversation_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'interrupted', 'failed')),
    position INTEGER NOT NULL CHECK (position > 0),
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, request_id, role),
    UNIQUE (conversation_id, position)
);

CREATE INDEX IF NOT EXISTS idx_local_messages_order
ON local_messages(conversation_id, position);

CREATE TABLE IF NOT EXISTS local_request_commits (
    user_id TEXT NOT NULL REFERENCES local_users(user_id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL
        REFERENCES local_conversations(conversation_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('started', 'interrupted', 'completed', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, conversation_id, request_id)
);

CREATE TABLE IF NOT EXISTS local_thread_states (
    thread_key TEXT PRIMARY KEY,
    user_id TEXT,
    conversation_id TEXT,
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    schema_version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _identifier(value: str, field_name: str) -> str:
    try:
        normalized = normalize_identifier(value, field_name=field_name)
    except ValueError as exc:
        raise LocalPersistenceError(f"Invalid {field_name}.") from exc
    if normalized is None:
        raise LocalPersistenceError(f"Invalid {field_name}.")
    return normalized


def _content(value: str, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise LocalPersistenceError(f"{field_name} must be text.")
    if not value.strip():
        raise LocalPersistenceError(f"{field_name} must not be blank.")
    if len(value) > maximum:
        raise LocalPersistenceError(f"{field_name} exceeds the local limit.")
    return value


def _limit(value: int) -> int:
    if not isinstance(value, int) or not 1 <= value <= MAX_CHAT_READ_LIMIT:
        raise LocalPersistenceError(
            f"limit must be between 1 and {MAX_CHAT_READ_LIMIT}."
        )
    return value


class LocalSQLiteDatabase:
    """Own schema initialization while opening one short-lived connection per use."""

    def __init__(self, path: str | Path) -> None:
        text = str(path).strip()
        if not text:
            raise LocalPersistenceError("Local SQLite path is required.")
        self.path = Path(text).expanduser()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise LocalPersistenceError("Local SQLite database is unavailable.") from exc
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                try:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS schema_metadata ("
                        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    row = connection.execute(
                        "SELECT value FROM schema_metadata WHERE key = ?",
                        ("local_schema_version",),
                    ).fetchone()
                    if row is not None and row["value"] in {"1", "2"}:
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            if row["value"] == "1":
                                self._upgrade_v1_to_v2(connection)
                            self._upgrade_v2_to_v3(connection)
                            connection.commit()
                        except Exception:
                            connection.rollback()
                            raise
                    elif row is not None and row["value"] != str(LOCAL_SCHEMA_VERSION):
                        raise LocalPersistenceSchemaError(
                            "Local SQLite schema version is incompatible."
                        )

                    connection.executescript(_SCHEMA_SQL)
                    if row is None:
                        connection.execute(
                            "INSERT INTO schema_metadata(key, value) VALUES (?, ?)",
                            ("local_schema_version", str(LOCAL_SCHEMA_VERSION)),
                        )
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
        except LocalPersistenceError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise LocalPersistenceError(
                "Local SQLite schema initialization failed."
            ) from exc

    @staticmethod
    def _upgrade_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Add stable conversation sessions without deleting Gate 3 records."""
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(local_conversations)")
        }
        if "session_id" not in columns:
            connection.execute("ALTER TABLE local_conversations ADD COLUMN session_id TEXT")
        rows = connection.execute(
            "SELECT conversation_id FROM local_conversations WHERE session_id IS NULL OR session_id = ''"
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE local_conversations SET session_id = ? WHERE conversation_id = ?",
                (uuid4().hex, row["conversation_id"]),
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_local_conversations_session "
            "ON local_conversations(session_id)"
        )
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = ?",
            ("2", "local_schema_version"),
        )

    @staticmethod
    def _upgrade_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Upgrade routing-only JSON to the bounded Gate 5 memory contract."""
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "local_thread_states" in tables:
            rows = connection.execute(
                "SELECT thread_key, state_json, schema_version FROM local_thread_states"
            ).fetchall()
            defaults = {
                "recent_turn_references": [],
                "recent_episodes": [],
                "summary_updated_at": "",
                "summary_source_request_id": "",
                "summary_size_tokens": 0,
            }
            for row in rows:
                if row["schema_version"] == THREAD_STATE_SCHEMA_VERSION:
                    continue
                try:
                    payload = json.loads(row["state_json"])
                except (TypeError, ValueError) as exc:
                    raise LocalPersistenceSchemaError(
                        "Existing thread state is malformed."
                    ) from exc
                if not isinstance(payload, dict):
                    raise LocalPersistenceSchemaError(
                        "Existing thread state is malformed."
                    )
                for key, value in defaults.items():
                    payload.setdefault(key, value)
                connection.execute(
                    "UPDATE local_thread_states SET schema_version = ?, state_json = ? "
                    "WHERE thread_key = ?",
                    (
                        THREAD_STATE_SCHEMA_VERSION,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        row["thread_key"],
                    ),
                )
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = ?",
            (str(LOCAL_SCHEMA_VERSION), "local_schema_version"),
        )

    def foreign_keys_enabled(self) -> bool:
        with self.connect() as connection:
            row = connection.execute("PRAGMA foreign_keys").fetchone()
            return bool(row and row[0] == 1)


def _conversation(row: sqlite3.Row) -> LocalConversation:
    return LocalConversation(
        conversation_id=row["conversation_id"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _message(row: sqlite3.Row) -> LocalChatMessage:
    return LocalChatMessage(
        message_id=row["message_id"],
        conversation_id=row["conversation_id"],
        request_id=row["request_id"],
        role=row["role"],
        content=row["content"],
        status=row["status"],
        position=row["position"],
        created_at=row["created_at"],
    )


def _commit(row: sqlite3.Row) -> LocalRequestCommit:
    return LocalRequestCommit(
        user_id=row["user_id"],
        conversation_id=row["conversation_id"],
        request_id=row["request_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _user(row: sqlite3.Row) -> LocalUser:
    return LocalUser(user_id=row["user_id"], created_at=row["created_at"])


class SQLiteChatRepository:
    """Owner-scoped local transcript adapter for future UI simulation."""

    def __init__(self, database: LocalSQLiteDatabase) -> None:
        self.database = database

    def create_user(self, *, user_id: str | None = None) -> LocalUser:
        user = _identifier(user_id, "user_id") if user_id is not None else f"local-user-{uuid4().hex}"
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO local_users(user_id, created_at) VALUES (?, ?)",
                    (user, now),
                )
                row = connection.execute(
                    "SELECT * FROM local_users WHERE user_id = ?", (user,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:
            raise LocalPersistenceError("Local user creation failed.")
        return _user(row)

    def list_users(self, *, limit: int = 50) -> tuple[LocalUser, ...]:
        bounded = _limit(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM local_users ORDER BY created_at DESC, user_id ASC LIMIT ?",
                (bounded,),
            ).fetchall()
        return tuple(_user(row) for row in rows)

    def get_user(self, *, user_id: str) -> LocalUser | None:
        user = _identifier(user_id, "user_id")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_users WHERE user_id = ?", (user,)
            ).fetchone()
        return _user(row) if row is not None else None

    @staticmethod
    def _assert_owner(
        connection: sqlite3.Connection,
        *,
        user_id: str,
        conversation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM local_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise LocalPersistenceError("Local conversation was not found.")
        if row["user_id"] != user_id:
            raise LocalPersistenceOwnershipError(
                "Local conversation ownership validation failed."
            )
        return row

    def create_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
        session_id: str | None = None,
        title: str = "",
    ) -> LocalConversation:
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        session = _identifier(session_id, "session_id") if session_id is not None else uuid4().hex
        if not isinstance(title, str) or len(title) > MAX_CONVERSATION_TITLE_CHARS:
            raise LocalPersistenceError("Invalid local conversation title.")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO local_users(user_id, created_at) VALUES (?, ?)",
                    (user, now),
                )
                existing = connection.execute(
                    "SELECT * FROM local_conversations WHERE conversation_id = ?",
                    (conversation,),
                ).fetchone()
                if existing is not None and existing["user_id"] != user:
                    raise LocalPersistenceOwnershipError(
                        "Local conversation ownership validation failed."
                    )
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO local_conversations(
                            conversation_id, user_id, session_id, title, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (conversation, user, session, title, now, now),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            row = connection.execute(
                "SELECT * FROM local_conversations WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        if row is None:
            raise LocalPersistenceError("Local conversation creation failed.")
        return _conversation(row)

    def list_conversations(
        self,
        *,
        user_id: str,
        limit: int = 50,
    ) -> tuple[LocalConversation, ...]:
        user = _identifier(user_id, "user_id")
        bounded = _limit(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM local_conversations
                WHERE user_id = ?
                ORDER BY updated_at DESC, conversation_id ASC
                LIMIT ?
                """,
                (user, bounded),
            ).fetchall()
        return tuple(_conversation(row) for row in rows)

    def get_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> LocalConversation | None:
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_conversations WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        if row is None:
            return None
        if row["user_id"] != user:
            raise LocalPersistenceOwnershipError(
                "Local conversation ownership validation failed."
            )
        return _conversation(row)

    @staticmethod
    def _insert_message(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        request_id: str,
        role: str,
        content: str,
        status: str,
    ) -> LocalChatMessage:
        existing = connection.execute(
            """
            SELECT * FROM local_messages
            WHERE conversation_id = ? AND request_id = ? AND role = ?
            """,
            (conversation_id, request_id, role),
        ).fetchone()
        if existing is not None:
            if existing["content"] != content:
                raise LocalPersistenceConflictError(
                    "Local request id was reused with different message content."
                )
            return _message(existing)
        row = connection.execute(
            """
            SELECT COALESCE(MAX(position), 0) + 1 AS next_position
            FROM local_messages WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        position = int(row["next_position"])
        message = LocalChatMessage(
            message_id=uuid4().hex,
            conversation_id=conversation_id,
            request_id=request_id,
            role=role,
            content=content,
            status=status,
            position=position,
            created_at=utc_now(),
        )
        connection.execute(
            """
            INSERT INTO local_messages(
                message_id, conversation_id, request_id, role,
                content, status, position, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.conversation_id,
                message.request_id,
                message.role,
                message.content,
                message.status,
                message.position,
                message.created_at,
            ),
        )
        return message

    def append(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
        role: str,
        content: str,
        status: str = "completed",
    ) -> LocalChatMessage:
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        request = _identifier(request_id, "request_id")
        if role not in {"user", "assistant"}:
            raise LocalPersistenceError("Invalid local message role.")
        if status not in {"completed", "interrupted", "failed"}:
            raise LocalPersistenceError("Invalid local message status.")
        body = _content(
            content,
            field_name="message content",
            maximum=MAX_CHAT_CONTENT_CHARS,
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_owner(
                    connection,
                    user_id=user,
                    conversation_id=conversation,
                )
                message = self._insert_message(
                    connection,
                    conversation_id=conversation,
                    request_id=request,
                    role=role,
                    content=body,
                    status=status,
                )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO local_request_commits(
                        user_id, conversation_id, request_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, conversation_id, request_id)
                    DO UPDATE SET
                        status = CASE
                            WHEN local_request_commits.status = 'completed'
                            THEN 'completed'
                            ELSE excluded.status
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (user, conversation, request, status, now, now),
                )
                connection.execute(
                    "UPDATE local_conversations SET updated_at = ? WHERE conversation_id = ?",
                    (now, conversation),
                )
                connection.commit()
                return message
            except Exception:
                connection.rollback()
                raise

    def recent(
        self,
        *,
        user_id: str,
        conversation_id: str,
        limit: int = 50,
    ) -> tuple[LocalChatMessage, ...]:
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        bounded = _limit(limit)
        with self.database.connect() as connection:
            self._assert_owner(
                connection,
                user_id=user,
                conversation_id=conversation,
            )
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM local_messages
                    WHERE conversation_id = ?
                    ORDER BY position DESC
                    LIMIT ?
                )
                ORDER BY position ASC
                """,
                (conversation, bounded),
            ).fetchall()
        return tuple(_message(row) for row in rows)

    def delete_conversation(self, *, user_id: str, conversation_id: str) -> bool:
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT user_id FROM local_conversations WHERE conversation_id = ?",
                    (conversation,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                if row["user_id"] != user:
                    raise LocalPersistenceOwnershipError(
                        "Local conversation ownership validation failed."
                    )
                connection.execute(
                    "DELETE FROM local_conversations WHERE conversation_id = ?",
                    (conversation,),
                )
                connection.execute(
                    "DELETE FROM local_thread_states WHERE conversation_id = ? AND user_id = ?",
                    (conversation, user),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def begin_request(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
    ) -> LocalRequestCommit:
        self.create_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        request = _identifier(request_id, "request_id")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_owner(
                    connection,
                    user_id=user,
                    conversation_id=conversation,
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO local_request_commits(
                        user_id, conversation_id, request_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'started', ?, ?)
                    """,
                    (user, conversation, request, now, now),
                )
                row = connection.execute(
                    """
                    SELECT * FROM local_request_commits
                    WHERE user_id = ? AND conversation_id = ? AND request_id = ?
                    """,
                    (user, conversation, request),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:
            raise LocalPersistenceError("Local request initialization failed.")
        return _commit(row)

    def commit_turn(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
        user_content: str,
        assistant_content: str,
    ) -> tuple[LocalChatMessage, LocalChatMessage]:
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        request = _identifier(request_id, "request_id")
        user_body = _content(
            user_content,
            field_name="user content",
            maximum=MAX_CHAT_CONTENT_CHARS,
        )
        assistant_body = _content(
            assistant_content,
            field_name="assistant content",
            maximum=MAX_CHAT_CONTENT_CHARS,
        )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_owner(
                    connection,
                    user_id=user,
                    conversation_id=conversation,
                )
                user_message = self._insert_message(
                    connection,
                    conversation_id=conversation,
                    request_id=request,
                    role="user",
                    content=user_body,
                    status="completed",
                )
                assistant_message = self._insert_message(
                    connection,
                    conversation_id=conversation,
                    request_id=request,
                    role="assistant",
                    content=assistant_body,
                    status="completed",
                )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO local_request_commits(
                        user_id, conversation_id, request_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?)
                    ON CONFLICT(user_id, conversation_id, request_id)
                    DO UPDATE SET status = 'completed', updated_at = excluded.updated_at
                    """,
                    (user, conversation, request, now, now),
                )
                connection.execute(
                    "UPDATE local_conversations SET updated_at = ? WHERE conversation_id = ?",
                    (now, conversation),
                )
                connection.commit()
                return user_message, assistant_message
            except Exception:
                connection.rollback()
                raise

    def mark_request_status(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
        status: str,
    ) -> LocalRequestCommit:
        if status not in {"interrupted", "failed"}:
            raise LocalPersistenceError("Invalid local request terminal status.")
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        request = _identifier(request_id, "request_id")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_owner(
                    connection,
                    user_id=user,
                    conversation_id=conversation,
                )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE local_request_commits
                    SET status = CASE WHEN status = 'completed' THEN status ELSE ? END,
                        updated_at = ?
                    WHERE user_id = ? AND conversation_id = ? AND request_id = ?
                    """,
                    (status, now, user, conversation, request),
                )
                row = connection.execute(
                    """
                    SELECT * FROM local_request_commits
                    WHERE user_id = ? AND conversation_id = ? AND request_id = ?
                    """,
                    (user, conversation, request),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:
            raise LocalPersistenceError("Local request record was not found.")
        return _commit(row)

    def request_status(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
    ) -> LocalRequestCommit | None:
        user = _identifier(user_id, "user_id")
        conversation = _identifier(conversation_id, "conversation_id")
        request = _identifier(request_id, "request_id")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT c.user_id AS owner_id, r.*
                FROM local_request_commits r
                JOIN local_conversations c
                  ON c.conversation_id = r.conversation_id
                WHERE r.conversation_id = ? AND r.request_id = ?
                """,
                (conversation, request),
            ).fetchone()
        if row is None:
            return None
        if row["owner_id"] != user:
            raise LocalPersistenceOwnershipError(
                "Local conversation ownership validation failed."
            )
        return _commit(row)


class SQLiteThreadStateStore:
    """Optimistically versioned compact continuity keyed by thread_key."""

    def __init__(self, database: LocalSQLiteDatabase) -> None:
        self.database = database

    @staticmethod
    def _assert_identity(row: sqlite3.Row, identity: RequestIdentity) -> None:
        if (
            row["user_id"] != identity.user_id
            or row["conversation_id"] != identity.conversation_id
        ):
            raise LocalPersistenceOwnershipError(
                "Local thread ownership validation failed."
            )

    def load(self, *, identity: RequestIdentity) -> ThreadMemoryState | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_thread_states WHERE thread_key = ?",
                (identity.thread_key,),
            ).fetchone()
        if row is None:
            return None
        self._assert_identity(row, identity)
        encoded = row["state_json"]
        if len(encoded.encode("utf-8")) > MAX_THREAD_STATE_BYTES:
            raise LocalPersistenceSchemaError("Persisted thread state is oversized.")
        try:
            payload = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise LocalPersistenceSchemaError(
                "Persisted thread state is malformed."
            ) from exc
        try:
            return ThreadMemoryState.from_payload(
                payload,
                thread_key=row["thread_key"],
                user_id=row["user_id"],
                conversation_id=row["conversation_id"],
                session_id=row["session_id"],
                updated_at=row["updated_at"],
                revision=row["revision"],
                schema_version=row["schema_version"],
            )
        except ValueError as exc:
            raise LocalPersistenceSchemaError(
                "Persisted thread state is malformed."
            ) from exc

    def save(
        self,
        *,
        identity: RequestIdentity,
        state: ThreadMemoryState,
        expected_revision: int,
    ) -> ThreadMemoryState:
        if (
            state.thread_key != identity.thread_key
            or state.user_id != identity.user_id
            or state.conversation_id != identity.conversation_id
            or state.session_id != identity.session_id
        ):
            raise LocalPersistenceOwnershipError(
                "Local thread-state identity does not match the request."
            )
        if expected_revision < 0:
            raise LocalPersistenceConflictError("Invalid expected thread revision.")
        if state.schema_version != THREAD_STATE_SCHEMA_VERSION:
            raise LocalPersistenceSchemaError(
                "Persisted thread-state schema version is incompatible."
            )
        payload = json.dumps(
            state.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_THREAD_STATE_BYTES:
            raise LocalPersistenceSchemaError("Persisted thread state is oversized.")
        now = utc_now()
        new_revision = expected_revision + 1
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM local_thread_states WHERE thread_key = ?",
                    (identity.thread_key,),
                ).fetchone()
                if row is None:
                    if expected_revision != 0:
                        raise LocalPersistenceConflictError(
                            "Thread state revision is stale."
                        )
                    connection.execute(
                        """
                        INSERT INTO local_thread_states(
                            thread_key, user_id, conversation_id, session_id,
                            revision, schema_version, state_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identity.thread_key,
                            identity.user_id,
                            identity.conversation_id,
                            identity.session_id,
                            new_revision,
                            state.schema_version,
                            payload,
                            now,
                        ),
                    )
                else:
                    self._assert_identity(row, identity)
                    if row["revision"] != expected_revision:
                        raise LocalPersistenceConflictError(
                            "Thread state revision is stale."
                        )
                    cursor = connection.execute(
                        """
                        UPDATE local_thread_states
                        SET session_id = ?, revision = ?, schema_version = ?,
                            state_json = ?, updated_at = ?
                        WHERE thread_key = ? AND revision = ?
                        """,
                        (
                            identity.session_id,
                            new_revision,
                            state.schema_version,
                            payload,
                            now,
                            identity.thread_key,
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise LocalPersistenceConflictError(
                            "Thread state revision is stale."
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return replace(
            state,
            session_id=identity.session_id,
            revision=new_revision,
            updated_at=now,
        )

    def delete(self, *, identity: RequestIdentity) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM local_thread_states WHERE thread_key = ?",
                    (identity.thread_key,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                self._assert_identity(row, identity)
                connection.execute(
                    "DELETE FROM local_thread_states WHERE thread_key = ?",
                    (identity.thread_key,),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise
