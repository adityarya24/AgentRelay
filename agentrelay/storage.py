"""Async SQLite storage with WAL mode and serialized writes."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    Identity,
    Message,
    MessageType,
    PolicyRule,
    TelegramBotRegistration,
    ThreadState,
    utc_timestamp,
)


class Storage:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser())
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._seq_cache: dict[str, int] = {}

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                room TEXT NOT NULL,
                content TEXT NOT NULL,
                thread TEXT,
                type TEXT NOT NULL DEFAULT 'text',
                target TEXT,
                mentions TEXT NOT NULL DEFAULT '[]',
                ts REAL NOT NULL,
                seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS governance_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identities (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                transport TEXT NOT NULL,
                capabilities TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                trust_level TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                secret_ref TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS policies (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                room TEXT NOT NULL DEFAULT '*',
                effect TEXT NOT NULL,
                reason TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_bots (
                identity_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                token_ref TEXT NOT NULL,
                bot_to_bot_enabled INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                verified_at REAL,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_room_seq ON messages(room, seq);
            CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread);
            CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
            CREATE INDEX IF NOT EXISTS idx_identity_kind ON identities(kind);
            CREATE INDEX IF NOT EXISTS idx_identity_transport ON identities(transport);
            CREATE INDEX IF NOT EXISTS idx_policy_route ON policies(source, target, room);
            CREATE INDEX IF NOT EXISTS idx_telegram_bot_username ON telegram_bots(username);
            """
        )
        await self._ensure_thread_schema()
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_thread_room ON threads(room)")
        await self._db.commit()

    async def _ensure_thread_schema(self) -> None:
        cursor = await self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='threads'"
        )
        exists = await cursor.fetchone()
        if not exists:
            await self.db.execute(
                """
                CREATE TABLE threads (
                    id TEXT NOT NULL,
                    room TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'open',
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    closed_at REAL,
                    PRIMARY KEY (id, room)
                )
                """
            )
            return

        cursor = await self.db.execute("PRAGMA table_info(threads)")
        rows = await cursor.fetchall()
        pk_cols = [row[1] for row in sorted((r for r in rows if r[5]), key=lambda r: r[5])]
        if pk_cols == ["id", "room"]:
            return

        await self.db.executescript(
            """
            CREATE TABLE threads_new (
                id TEXT NOT NULL,
                room TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'open',
                msg_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                closed_at REAL,
                PRIMARY KEY (id, room)
            );
            INSERT OR REPLACE INTO threads_new
                (id, room, title, state, msg_count, created_at, closed_at)
            SELECT id, room, title, state, msg_count, created_at, closed_at
            FROM threads;
            DROP TABLE threads;
            ALTER TABLE threads_new RENAME TO threads;
            """
        )

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("storage not initialized")
        return self._db

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        async with self._write_lock:
            cursor = await self.db.execute(sql, params)
            await self.db.commit()
            return cursor

    async def store_message(self, msg: Message) -> None:
        async with self._write_lock:
            cached = self._seq_cache.get(msg.room)
            if cached is None:
                cursor = await self.db.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE room=?", (msg.room,)
                )
                row = await cursor.fetchone()
                seq = int(row[0] if row else 1)
            else:
                seq = cached
            self._seq_cache[msg.room] = seq + 1
            msg.seq = seq
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        msg.id,
                        msg.sender,
                        msg.room,
                        msg.content,
                        msg.thread,
                        msg.type.value if isinstance(msg.type, MessageType) else msg.type,
                        msg.target,
                        json.dumps(msg.mentions),
                        msg.ts,
                        msg.seq,
                    ),
                )
                if msg.thread:
                    await self.db.execute(
                        """
                        INSERT INTO threads (id, room, title, state, msg_count, created_at)
                        VALUES (?, ?, ?, 'open', 0, ?)
                        ON CONFLICT(id, room) DO NOTHING
                        """,
                        (msg.thread, msg.room, msg.thread, time.time()),
                    )
                    await self.db.execute(
                        "UPDATE threads SET msg_count = msg_count + 1 WHERE id=? AND room=?",
                        (msg.thread, msg.room),
                    )
                await self.db.commit()
            except Exception:
                self._seq_cache[msg.room] = seq
                raise

    async def fetch_history(self, room: str, limit: int = 50, after_seq: int = 0) -> list[Message]:
        cursor = await self.db.execute(
            "SELECT * FROM messages WHERE room=? AND seq>? ORDER BY seq ASC LIMIT ?",
            (room, after_seq, limit),
        )
        rows = await cursor.fetchall()
        return [
            Message(
                id=row[0],
                sender=row[1],
                room=row[2],
                content=row[3],
                thread=row[4],
                type=row[5],
                target=row[6],
                mentions=json.loads(row[7]),
                ts=row[8],
                seq=row[9],
            )
            for row in rows
        ]

    async def store_thread(self, thread_id: str, room: str, title: str) -> None:
        await self._execute(
            """
            INSERT OR REPLACE INTO threads (id, room, title, state, msg_count, created_at, closed_at)
            VALUES (?, ?, ?, COALESCE((SELECT state FROM threads WHERE id=? AND room=?), 'open'),
                    COALESCE((SELECT msg_count FROM threads WHERE id=? AND room=?), 0),
                    COALESCE((SELECT created_at FROM threads WHERE id=? AND room=?), ?),
                    (SELECT closed_at FROM threads WHERE id=? AND room=?))
            """,
            (
                thread_id,
                room,
                title,
                thread_id,
                room,
                thread_id,
                room,
                thread_id,
                room,
                time.time(),
                thread_id,
                room,
            ),
        )

    async def fetch_threads(self, room: str) -> list[dict]:
        cursor = await self.db.execute(
            "SELECT id, room, title, state, msg_count, created_at, closed_at FROM threads WHERE room=?",
            (room,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "room": row[1],
                "title": row[2],
                "state": row[3],
                "msg_count": row[4],
                "created_at": row[5],
                "closed_at": row[6],
            }
            for row in rows
        ]

    async def get_thread_state(self, thread_id: str, room: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT state FROM threads WHERE id=? AND room=?", (thread_id, room)
        )
        row = await cursor.fetchone()
        return str(row[0]) if row else None

    async def update_thread_state(
        self, thread_id: str, state: str | ThreadState, room: str | None = None
    ) -> None:
        value = state.value if isinstance(state, ThreadState) else state
        closed_at = time.time() if value == ThreadState.CLOSED.value else None
        if room is None:
            await self._execute(
                "UPDATE threads SET state=?, closed_at=COALESCE(?, closed_at) WHERE id=?",
                (value, closed_at, thread_id),
            )
            return
        await self._execute(
            "UPDATE threads SET state=?, closed_at=COALESCE(?, closed_at) WHERE id=? AND room=?",
            (value, closed_at, thread_id, room),
        )

    async def fetch_terminal_threads(self) -> list[tuple[str, str]]:
        cursor = await self.db.execute(
            "SELECT room, id FROM threads WHERE state IN ('closing', 'closed')"
        )
        return [(str(row[0]), str(row[1])) for row in await cursor.fetchall()]

    @staticmethod
    def _identity_from_row(row: tuple) -> Identity:
        return Identity(
            id=row[0],
            display_name=row[1],
            kind=row[2],
            transport=row[3],
            capabilities=json.loads(row[4]),
            enabled=bool(row[5]),
            trust_level=row[6],
            metadata=json.loads(row[7]),
            secret_ref=row[8],
            created_at=row[9],
            updated_at=row[10],
        )

    async def upsert_identity(self, identity: Identity) -> Identity:
        now = utc_timestamp()
        existing = await self.get_identity(identity.id)
        created_at = existing.created_at if existing else identity.created_at
        saved = identity.model_copy(update={"created_at": created_at, "updated_at": now})
        await self._execute(
            """
            INSERT INTO identities (
                id, display_name, kind, transport, capabilities, enabled, trust_level,
                metadata, secret_ref, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                kind=excluded.kind,
                transport=excluded.transport,
                capabilities=excluded.capabilities,
                enabled=excluded.enabled,
                trust_level=excluded.trust_level,
                metadata=excluded.metadata,
                secret_ref=excluded.secret_ref,
                updated_at=excluded.updated_at
            """,
            (
                saved.id,
                saved.display_name,
                saved.kind.value,
                saved.transport.value,
                json.dumps(saved.capabilities),
                int(saved.enabled),
                saved.trust_level.value,
                json.dumps(saved.metadata),
                saved.secret_ref,
                saved.created_at,
                saved.updated_at,
            ),
        )
        return saved

    async def get_identity(self, identity_id: str) -> Identity | None:
        cursor = await self.db.execute(
            """
            SELECT id, display_name, kind, transport, capabilities, enabled, trust_level,
                   metadata, secret_ref, created_at, updated_at
            FROM identities
            WHERE id=?
            """,
            (identity_id,),
        )
        row = await cursor.fetchone()
        return self._identity_from_row(row) if row else None

    async def list_identities(self, include_disabled: bool = False) -> list[Identity]:
        sql = """
            SELECT id, display_name, kind, transport, capabilities, enabled, trust_level,
                   metadata, secret_ref, created_at, updated_at
            FROM identities
        """
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        cursor = await self.db.execute(sql)
        return [self._identity_from_row(row) for row in await cursor.fetchall()]

    async def set_identity_enabled(self, identity_id: str, enabled: bool) -> Identity | None:
        await self._execute(
            "UPDATE identities SET enabled=?, updated_at=? WHERE id=?",
            (int(enabled), utc_timestamp(), identity_id),
        )
        return await self.get_identity(identity_id)

    @staticmethod
    def _policy_from_row(row: tuple) -> PolicyRule:
        return PolicyRule(
            id=row[0],
            source=row[1],
            target=row[2],
            room=row[3],
            effect=row[4],
            reason=row[5],
            enabled=bool(row[6]),
            created_at=row[7],
            updated_at=row[8],
        )

    async def upsert_policy(self, rule: PolicyRule) -> PolicyRule:
        now = utc_timestamp()
        existing = await self.get_policy(rule.id)
        created_at = existing.created_at if existing else rule.created_at
        saved = rule.model_copy(update={"created_at": created_at, "updated_at": now})
        await self._execute(
            """
            INSERT INTO policies (
                id, source, target, room, effect, reason, enabled, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source=excluded.source,
                target=excluded.target,
                room=excluded.room,
                effect=excluded.effect,
                reason=excluded.reason,
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (
                saved.id,
                saved.source,
                saved.target,
                saved.room,
                saved.effect.value,
                saved.reason,
                int(saved.enabled),
                saved.created_at,
                saved.updated_at,
            ),
        )
        return saved

    async def get_policy(self, rule_id: str) -> PolicyRule | None:
        cursor = await self.db.execute(
            """
            SELECT id, source, target, room, effect, reason, enabled, created_at, updated_at
            FROM policies
            WHERE id=?
            """,
            (rule_id,),
        )
        row = await cursor.fetchone()
        return self._policy_from_row(row) if row else None

    async def list_policies(self, include_disabled: bool = False) -> list[PolicyRule]:
        sql = """
            SELECT id, source, target, room, effect, reason, enabled, created_at, updated_at
            FROM policies
        """
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY source, target, room, effect, id"
        cursor = await self.db.execute(sql)
        return [self._policy_from_row(row) for row in await cursor.fetchall()]

    async def set_policy_enabled(self, rule_id: str, enabled: bool) -> PolicyRule | None:
        await self._execute(
            "UPDATE policies SET enabled=?, updated_at=? WHERE id=?",
            (int(enabled), utc_timestamp(), rule_id),
        )
        return await self.get_policy(rule_id)

    @staticmethod
    def _telegram_bot_from_row(row: tuple) -> TelegramBotRegistration:
        return TelegramBotRegistration(
            identity_id=row[0],
            username=row[1],
            token_ref=row[2],
            bot_to_bot_enabled=bool(row[3]),
            enabled=bool(row[4]),
            verified_at=row[5],
            last_error=row[6],
            created_at=row[7],
            updated_at=row[8],
        )

    async def upsert_telegram_bot(
        self, bot: TelegramBotRegistration
    ) -> TelegramBotRegistration:
        now = utc_timestamp()
        existing = await self.get_telegram_bot(bot.identity_id)
        created_at = existing.created_at if existing else bot.created_at
        saved = bot.model_copy(update={"created_at": created_at, "updated_at": now})
        await self._execute(
            """
            INSERT INTO telegram_bots (
                identity_id, username, token_ref, bot_to_bot_enabled, enabled,
                verified_at, last_error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_id) DO UPDATE SET
                username=excluded.username,
                token_ref=excluded.token_ref,
                bot_to_bot_enabled=excluded.bot_to_bot_enabled,
                enabled=excluded.enabled,
                verified_at=excluded.verified_at,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                saved.identity_id,
                saved.username,
                saved.token_ref,
                int(saved.bot_to_bot_enabled),
                int(saved.enabled),
                saved.verified_at,
                saved.last_error,
                saved.created_at,
                saved.updated_at,
            ),
        )
        return saved

    async def get_telegram_bot(self, identity_id: str) -> TelegramBotRegistration | None:
        cursor = await self.db.execute(
            """
            SELECT identity_id, username, token_ref, bot_to_bot_enabled, enabled,
                   verified_at, last_error, created_at, updated_at
            FROM telegram_bots
            WHERE identity_id=?
            """,
            (identity_id,),
        )
        row = await cursor.fetchone()
        return self._telegram_bot_from_row(row) if row else None

    async def get_telegram_bot_by_username(
        self, username: str
    ) -> TelegramBotRegistration | None:
        cursor = await self.db.execute(
            """
            SELECT identity_id, username, token_ref, bot_to_bot_enabled, enabled,
                   verified_at, last_error, created_at, updated_at
            FROM telegram_bots
            WHERE lower(username)=lower(?)
            """,
            (username.removeprefix("@"),),
        )
        row = await cursor.fetchone()
        return self._telegram_bot_from_row(row) if row else None

    async def list_telegram_bots(
        self, include_disabled: bool = False
    ) -> list[TelegramBotRegistration]:
        sql = """
            SELECT identity_id, username, token_ref, bot_to_bot_enabled, enabled,
                   verified_at, last_error, created_at, updated_at
            FROM telegram_bots
        """
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY identity_id"
        cursor = await self.db.execute(sql)
        return [self._telegram_bot_from_row(row) for row in await cursor.fetchall()]

    async def store_governance(self, state: dict) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO governance_state (id, data, updated_at) VALUES (1, ?, ?)",
            (json.dumps(state), time.time()),
        )

    async def load_governance(self) -> dict | None:
        cursor = await self.db.execute("SELECT data FROM governance_state WHERE id=1")
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def cleanup(self, ttl_hours: int = 24) -> int:
        cutoff = time.time() - ttl_hours * 3600
        cursor = await self._execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        return int(cursor.rowcount or 0)

    async def close(self) -> None:
        if self._db is None:
            return
        async with self._write_lock:
            await self._db.close()
        self._db = None
