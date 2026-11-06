"""SQLite operation receipts, NOT a second copy of graph progress.

Checkpoints alone decide where execution resumes. Receipts hold successful model
results so a node replay can reuse a completed side effect. They cannot guarantee
exactly-once provider calls if a process dies before the receipt is committed.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite

from .contracts import CallResult


class OperationStore:
    def __init__(self, path: Path):
        self.path = path

    async def open(self):
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS operations (
                session_id TEXT NOT NULL,
                operation_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                PRIMARY KEY(session_id, operation_key)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
        """)
        await self.conn.commit()

    async def close(self):
        await self.conn.close()

    async def register(self, session_id: str):
        await self.conn.execute(
            "INSERT INTO sessions(id, created_at) VALUES (?, ?)",
            (session_id, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def list_sessions(self) -> list[tuple[str, str]]:
        async with self.conn.execute(
            "SELECT id, created_at FROM sessions ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()

    async def get(self, session_id: str, key: str) -> CallResult | None:
        async with self.conn.execute(
            "SELECT payload, tokens FROM operations WHERE session_id=? AND operation_key=?",
            (session_id, key),
        ) as cur:
            row = await cur.fetchone()
        return CallResult(json.loads(row[0]), row[1]) if row else None

    async def put(self, session_id: str, key: str, result: CallResult):
        await self.conn.execute(
            "INSERT OR IGNORE INTO operations(session_id,operation_key,payload,tokens) VALUES (?,?,?,?)",
            (session_id, key, json.dumps(result.data, ensure_ascii=False), result.tokens),
        )
        await self.conn.commit()

    async def record_failed_usage(self, session_id: str, tokens: int):
        if tokens > 0:
            await self.put(session_id, f"failed:{uuid4().hex}", CallResult(None, tokens))

    async def failed_usage(self, session_id: str) -> dict[str, int]:
        async with self.conn.execute(
            "SELECT operation_key, tokens FROM operations WHERE session_id=? AND operation_key LIKE 'failed:%'",
            (session_id,),
        ) as cur:
            return dict(await cur.fetchall())
