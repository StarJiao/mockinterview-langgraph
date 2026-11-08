"""SQLite operation receipts, NOT a second copy of graph progress.

Checkpoints alone decide where execution resumes. Receipts hold successful model
results so a node replay can reuse a completed side effect. They cannot guarantee
exactly-once provider calls if a process dies before the receipt is committed.
stage_metrics holds duration and token usage for every real model call; it is
diagnostic evidence for later inspection and never feeds back into the graph.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite

from .contracts import CallResult
from .identity import CLI_USER


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
            CREATE TABLE IF NOT EXISTS score_failures (
                session_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                error_type TEXT NOT NULL,
                PRIMARY KEY(session_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_events (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                anchor TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, seq)
            );
            CREATE TABLE IF NOT EXISTS stage_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                stage_key TEXT NOT NULL,
                label TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                tokens INTEGER NOT NULL,
                ok INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_stage_metrics_session
                ON stage_metrics(session_id, id);
        """)
        # Older databases predate the Web UI. Add the ownership column and give
        # every pre-existing session the CLI user; checkpoints stay untouched.
        async with self.conn.execute("PRAGMA table_info(sessions)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if "username" not in columns:
            await self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN username TEXT NOT NULL DEFAULT ''"
            )
        await self.conn.execute(
            "UPDATE sessions SET username=? WHERE username='' OR username IS NULL",
            (CLI_USER,),
        )
        await self.conn.commit()

    async def close(self):
        await self.conn.close()

    async def register(self, session_id: str, username: str = CLI_USER):
        await self.conn.execute(
            "INSERT INTO sessions(id, created_at, username) VALUES (?, ?, ?)",
            (session_id, datetime.now(timezone.utc).isoformat(), username),
        )
        await self.conn.commit()

    async def list_sessions(self, username: str | None = None) -> list[tuple[str, str]]:
        if username is None:
            query, params = "SELECT id, created_at FROM sessions ORDER BY created_at DESC", ()
        else:
            query = "SELECT id, created_at FROM sessions WHERE username=? ORDER BY created_at DESC"
            params = (username,)
        async with self.conn.execute(query, params) as cur:
            return await cur.fetchall()

    async def owner(self, session_id: str) -> str | None:
        async with self.conn.execute(
            "SELECT username FROM sessions WHERE id=?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def ensure_user(self, username: str) -> None:
        """Register a Web username; ownership is set when a session is created.

        This is a data grouping convenience, not authentication: the username
        is supplied by the caller, and legacy sessions belong to CLI_USER.
        """
        await self.conn.execute(
            "INSERT OR IGNORE INTO users(username, created_at) VALUES (?, ?)",
            (username, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def has_user(self, username: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM users WHERE username=?", (username,)
        ) as cur:
            return await cur.fetchone() is not None

    async def add_event(self, session_id: str, anchor: str, text: str, role: str = "system"):
        async with self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_events WHERE session_id=?",
            (session_id,),
        ) as cur:
            (seq,) = await cur.fetchone()
        await self.conn.execute(
            "INSERT INTO session_events(session_id, seq, anchor, role, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, seq, anchor, role, text, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def events(self, session_id: str) -> list[dict]:
        async with self.conn.execute(
            "SELECT anchor, role, text, created_at FROM session_events "
            "WHERE session_id=? ORDER BY seq",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"anchor": row[0], "role": row[1], "text": row[2], "created_at": row[3]}
            for row in rows
        ]

    async def add_stage_metric(self, session_id: str, metric: dict):
        """Persist one real model call. Replays reuse receipts and add no row."""
        await self.conn.execute(
            "INSERT INTO stage_metrics(session_id, stage_key, label, started_at, "
            "finished_at, duration_ms, tokens, ok, error, attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                metric["stage_key"],
                metric["label"],
                metric["started_at"],
                metric["finished_at"],
                int(metric["duration_ms"]),
                int(metric["tokens"]),
                1 if metric["ok"] else 0,
                metric["error"],
                int(metric["attempts"]),
            ),
        )
        await self.conn.commit()

    async def stage_metrics(self, session_id: str) -> list[dict]:
        async with self.conn.execute(
            "SELECT id, stage_key, label, started_at, finished_at, duration_ms, "
            "tokens, ok, error, attempts FROM stage_metrics "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "key": row[1],
                "label": row[2],
                "started_at": row[3],
                "finished_at": row[4],
                "duration_ms": row[5],
                "ms": row[5],  # kept for the older /timings field name
                "tokens": row[6],
                "ok": bool(row[7]),
                "error": row[8],
                "attempts": row[9],
            }
            for row in rows
        ]

    async def stage_summary(self, session_id: str) -> dict:
        async with self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration_ms), 0), COALESCE(SUM(tokens), 0), "
            "COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), 0) "
            "FROM stage_metrics WHERE session_id=?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return {
            "calls": row[0],
            "total_ms": row[1],
            "total_tokens": row[2],
            "failures": row[3],
        }

    async def revision(self, session_id: str) -> str:
        """Cheap change marker for polling: three indexed counts, no checkpoint read."""
        async with self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM operations WHERE session_id=?), "
            "(SELECT COUNT(*) FROM session_events WHERE session_id=?), "
            "(SELECT COUNT(*) FROM stage_metrics WHERE session_id=?)",
            (session_id, session_id, session_id),
        ) as cur:
            row = await cur.fetchone()
        return f"{row[0]}.{row[1]}.{row[2]}"

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

    async def set_score_failure(self, session_id: str, question_id: str, error_type: str):
        await self.conn.execute(
            "INSERT OR REPLACE INTO score_failures VALUES (?, ?, ?)",
            (session_id, question_id, error_type),
        )
        await self.conn.commit()

    async def clear_score_failure(self, session_id: str, question_id: str):
        await self.conn.execute(
            "DELETE FROM score_failures WHERE session_id=? AND question_id=?",
            (session_id, question_id),
        )
        await self.conn.commit()

    async def score_failures(self, session_id: str) -> dict[str, str]:
        async with self.conn.execute(
            "SELECT question_id, error_type FROM score_failures WHERE session_id=?",
            (session_id,),
        ) as cur:
            return dict(await cur.fetchall())
