"""Async SQLite storage with WAL mode and serialized writes."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Message, MessageType, ThreadState


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
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                room TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'open',
                msg_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                closed_at REAL
            );
            CREATE TABLE IF NOT EXISTS governance_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_room_seq ON messages(room, seq);
            CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread);
            CREATE INDEX IF NOT EXISTS idx_thread_room ON threads(room);
            CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
            """
        )
        await self._db.commit()

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
                        ON CONFLICT(id) DO NOTHING
                        """,
                        (msg.thread, msg.room, msg.thread, time.time()),
                    )
                    await self.db.execute(
                        "UPDATE threads SET msg_count = msg_count + 1 WHERE id=?", (msg.thread,)
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
            VALUES (?, ?, ?, COALESCE((SELECT state FROM threads WHERE id=?), 'open'),
                    COALESCE((SELECT msg_count FROM threads WHERE id=?), 0),
                    COALESCE((SELECT created_at FROM threads WHERE id=?), ?),
                    (SELECT closed_at FROM threads WHERE id=?))
            """,
            (thread_id, room, title, thread_id, thread_id, thread_id, time.time(), thread_id),
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

    async def get_thread_state(self, thread_id: str) -> str | None:
        cursor = await self.db.execute("SELECT state FROM threads WHERE id=?", (thread_id,))
        row = await cursor.fetchone()
        return str(row[0]) if row else None

    async def update_thread_state(self, thread_id: str, state: str | ThreadState) -> None:
        value = state.value if isinstance(state, ThreadState) else state
        closed_at = time.time() if value == ThreadState.CLOSED.value else None
        await self._execute(
            "UPDATE threads SET state=?, closed_at=COALESCE(?, closed_at) WHERE id=?",
            (value, closed_at, thread_id),
        )

    async def fetch_terminal_threads(self) -> list[tuple[str, str]]:
        cursor = await self.db.execute(
            "SELECT room, id FROM threads WHERE state IN ('closing', 'closed')"
        )
        return [(str(row[0]), str(row[1])) for row in await cursor.fetchall()]

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
