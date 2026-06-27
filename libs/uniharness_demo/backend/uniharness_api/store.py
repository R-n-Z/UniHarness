"""SQLite-backed conversation and session stores.

Previously in-memory dicts; now persists across server restarts via
the shared ``uniharness.db`` SQLite database.

Module-level singletons ``store`` and ``session_store`` are Async-accessible.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from uniharness_api.database import acquire_write_lock, get_db, release_write_lock

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain objects (unchanged public API)
# ---------------------------------------------------------------------------


class WarmSession:
    """A pre-conversation warm session (VM user + home dir).

    Exists independently of any conversation.  Created when the user opens the
    welcome screen and claimed when they send their first message.
    """

    __slots__ = ("id", "mode", "session_name", "working_dir", "created_at")

    def __init__(
        self,
        session_id: str,
        mode: str,
        session_name: str | None = None,
        working_dir: str | None = None,
    ) -> None:
        self.id = session_id
        self.mode = mode
        self.session_name = session_name
        self.working_dir = working_dir
        self.created_at = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "mode": self.mode,
            "session_name": self.session_name,
            "working_dir": self.working_dir,
            "created_at": self.created_at.isoformat(),
        }


class Conversation:
    """A single conversation with its messages."""

    __slots__ = (
        "id",
        "title",
        "model_id",
        "mode",
        "session_name",
        "working_dir",
        "messages",
        "created_at",
        "updated_at",
    )

    def __init__(
        self,
        conversation_id: str,
        title: str,
        model_id: str | None = None,
        mode: str | None = None,
        session_name: str | None = None,
        working_dir: str | None = None,
    ) -> None:
        self.id = conversation_id
        self.title = title
        self.model_id = model_id
        self.mode = mode or "chat"
        self.session_name = session_name
        self.working_dir = working_dir
        self.messages: list[dict[str, Any]] = []
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)

    def add_message(
        self,
        role: str,
        content: str,
        blocks: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Add a message to the conversation (in-memory)."""
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if blocks:
            msg["blocks"] = blocks
        if attachments:
            msg["attachments"] = attachments
        self.messages.append(msg)
        self.updated_at = datetime.now(UTC)
        return msg

    def to_summary(self) -> dict[str, Any]:
        """Return a summary dict (no messages)."""
        return {
            "id": self.id,
            "title": self.title,
            "model_id": self.model_id,
            "mode": self.mode,
            "session_name": self.session_name,
            "working_dir": self.working_dir,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def to_detail(self) -> dict[str, Any]:
        """Return a full detail dict (with messages)."""
        return {
            "id": self.id,
            "title": self.title,
            "model_id": self.model_id,
            "mode": self.mode,
            "session_name": self.session_name,
            "working_dir": self.working_dir,
            "messages": self.messages,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_conv(row: aiosqlite.Row) -> Conversation:
    """Build a Conversation from a database row (without messages)."""
    return Conversation(
        conversation_id=row["id"],
        title=row["title"] or "New conversation",
        model_id=row["model_id"],
        mode=row["mode"],
        session_name=row["session_name"],
        working_dir=row["working_dir"],
    )


def _row_to_session(row: aiosqlite.Row) -> WarmSession:
    """Build a WarmSession from a database row."""
    return WarmSession(
        session_id=row["id"],
        mode=row["mode"] or "cowork",
        session_name=row["session_name"],
        working_dir=row["working_dir"],
    )


# ---------------------------------------------------------------------------
# SessionStore (SQLite-backed)
# ---------------------------------------------------------------------------


class SessionStore:
    """Persistent store for warm sessions (SQLite-backed)."""

    # -- Create ----------------------------------------------------------

    async def create(
        self,
        mode: str,
        session_name: str | None = None,
        working_dir: str | None = None,
    ) -> WarmSession:
        session_id = str(uuid.uuid4())
        session = WarmSession(session_id, mode, session_name=session_name, working_dir=working_dir)
        async with get_db() as db:
            await acquire_write_lock()
            try:
                await db.execute(
                    "INSERT INTO sessions (id, mode, session_name, working_dir) VALUES (?, ?, ?, ?)",
                    (session.id, session.mode, session.session_name, session.working_dir),
                )
                await db.commit()
            finally:
                release_write_lock()
        return session

    # -- Read ------------------------------------------------------------

    async def get(self, session_id: str) -> WarmSession | None:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
            return _row_to_session(row) if row else None

    # -- Claim -----------------------------------------------------------

    async def claim(self, session_id: str) -> WarmSession | None:
        """Remove and return a session (claimed by a conversation)."""
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            await acquire_write_lock()
            try:
                await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                await db.commit()
            finally:
                release_write_lock()
            return _row_to_session(row)

    # -- Delete ----------------------------------------------------------

    async def delete(self, session_id: str) -> bool:
        async with get_db() as db:
            await acquire_write_lock()
            try:
                cursor = await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                await db.commit()
                return cursor.rowcount > 0
            finally:
                release_write_lock()

    # -- Expired ---------------------------------------------------------

    async def expired(self, max_age_seconds: float = 600) -> list[WarmSession]:
        """Return sessions older than max_age_seconds."""
        cutoff = (datetime.now(UTC).timestamp() - max_age_seconds)
        cutoff_str = datetime.fromtimestamp(cutoff, tz=UTC).isoformat()
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE created_at < ?",
                (cutoff_str,),
            )
            rows = await cursor.fetchall()
            return [_row_to_session(r) for r in rows]

    # -- List all --------------------------------------------------------

    async def list_all(self) -> list[WarmSession]:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM sessions ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [_row_to_session(r) for r in rows]


# ---------------------------------------------------------------------------
# ConversationStore (SQLite-backed)
# ---------------------------------------------------------------------------


class ConversationStore:
    """Persistent store for conversations (SQLite-backed)."""

    # -- Create ----------------------------------------------------------

    async def create(
        self,
        title: str | None = None,
        model_id: str | None = None,
        mode: str | None = None,
        working_dir: str | None = None,
    ) -> Conversation:
        conversation_id = str(uuid.uuid4())
        conv = Conversation(conversation_id, title or "New conversation", model_id=model_id, mode=mode, working_dir=working_dir)
        async with get_db() as db:
            await acquire_write_lock()
            try:
                await db.execute(
                    """INSERT INTO conversations (id, title, model_id, mode, working_dir, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (conv.id, conv.title, conv.model_id, conv.mode, conv.working_dir,
                     conv.created_at.isoformat(), conv.updated_at.isoformat()),
                )
                await db.commit()
            finally:
                release_write_lock()
        return conv

    # -- Read ------------------------------------------------------------

    async def get(self, conversation_id: str) -> Conversation | None:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            conv = _row_to_conv(row)
            conv.created_at = datetime.fromisoformat(row["created_at"])
            conv.updated_at = datetime.fromisoformat(row["updated_at"])
            # Load messages
            conv.messages = await self._load_messages(db, conversation_id)
            return conv

    # -- List all --------------------------------------------------------

    async def list_all(self) -> list[Conversation]:
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC")
            rows = await cursor.fetchall()
            result: list[Conversation] = []
            for row in rows:
                conv = _row_to_conv(row)
                conv.created_at = datetime.fromisoformat(row["created_at"])
                conv.updated_at = datetime.fromisoformat(row["updated_at"])
                conv.messages = await self._load_messages(db, row["id"])
                result.append(conv)
            return result

    # -- Delete ----------------------------------------------------------

    async def delete(self, conversation_id: str) -> bool:
        async with get_db() as db:
            await acquire_write_lock()
            try:
                cursor = await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
                await db.commit()
                return cursor.rowcount > 0
            finally:
                release_write_lock()

    # -- Update title ----------------------------------------------------

    async def update_title(self, conversation_id: str, title: str) -> Conversation | None:
        now = datetime.now(UTC).isoformat()
        async with get_db() as db:
            await acquire_write_lock()
            try:
                cursor = await db.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (title, now, conversation_id),
                )
                await db.commit()
                if cursor.rowcount == 0:
                    return None
            finally:
                release_write_lock()
        return await self.get(conversation_id)

    # -- Update model_id -------------------------------------------------

    async def update_model_id(self, conversation_id: str, model_id: str | None) -> Conversation | None:
        now = datetime.now(UTC).isoformat()
        async with get_db() as db:
            await acquire_write_lock()
            try:
                cursor = await db.execute(
                    "UPDATE conversations SET model_id = ?, updated_at = ? WHERE id = ?",
                    (model_id, now, conversation_id),
                )
                await db.commit()
                if cursor.rowcount == 0:
                    return None
            finally:
                release_write_lock()
        return await self.get(conversation_id)

    # -- Add message -----------------------------------------------------

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        blocks: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        """Add a message to a conversation.  Persists to SQLite."""
        now = datetime.now(UTC)
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": now.isoformat(),
        }
        if blocks:
            msg["blocks"] = blocks
        if attachments:
            msg["attachments"] = attachments

        async with get_db() as db:
            # Check conversation exists
            cur = await db.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
            if not await cur.fetchone():
                return None

            await acquire_write_lock()
            try:
                await db.execute(
                    """INSERT INTO messages (conversation_id, role, content, blocks, attachments)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        conversation_id,
                        role,
                        content,
                        json.dumps(blocks) if blocks else None,
                        json.dumps(attachments) if attachments else None,
                    ),
                )
                await db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now.isoformat(), conversation_id),
                )
                await db.commit()
            finally:
                release_write_lock()
        return msg

    # -- Get messages for agent ------------------------------------------

    async def get_messages_for_agent(self, conversation_id: str) -> list[dict[str, Any]]:
        """Get messages formatted for the agent (role + content + attachments)."""
        async with get_db() as db:
            msgs = await self._load_messages(db, conversation_id)
            result: list[dict[str, Any]] = []
            for m in msgs:
                msg: dict[str, Any] = {"role": m["role"], "content": m["content"]}
                if m.get("attachments"):
                    msg["attachments"] = m["attachments"]
                result.append(msg)
            return result

    # -- Internal --------------------------------------------------------

    @staticmethod
    async def _load_messages(db: aiosqlite.Connection, conversation_id: str) -> list[dict[str, Any]]:
        """Load messages for a conversation from the database."""
        cursor = await db.execute(
            "SELECT role, content, blocks, attachments, created_at FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            msg: dict[str, Any] = {
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["created_at"],
            }
            if row["blocks"]:
                try:
                    msg["blocks"] = json.loads(row["blocks"])
                except json.JSONDecodeError:
                    msg["blocks"] = []
            if row["attachments"]:
                try:
                    msg["attachments"] = json.loads(row["attachments"])
                except json.JSONDecodeError:
                    msg["attachments"] = []
            result.append(msg)
        return result


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

store = ConversationStore()
session_store = SessionStore()
