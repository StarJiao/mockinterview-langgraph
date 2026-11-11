"""SQLite operation receipts, NOT a second copy of graph progress.

Checkpoints alone decide where execution resumes. Receipts hold successful model
results so a node replay can reuse a completed side effect. They cannot guarantee
exactly-once provider calls if a process dies before the receipt is committed.
stage_metrics holds duration and token usage for every real model call; it is
diagnostic evidence for later inspection and never feeds back into the graph.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite

from .contracts import CallResult
from .identity import CLI_USER

TEMP_PASS_DEFAULT_QUOTA = 300_000
"""Real-data baseline: one full 3-question interview with probes ≈ 114k tokens
(≈ 38k per question, stage_metrics 2026-11). The default leaves room for
several sessions or retries; override per pass with `pass new --quota`."""


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
            CREATE TABLE IF NOT EXISTS passes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                token_plain TEXT,
                kind TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                quota_tokens INTEGER,
                ttl_seconds INTEGER,
                first_used_at TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pass_sessions (
                pass_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                since TEXT NOT NULL,
                PRIMARY KEY(pass_id, session_id)
            );
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
        # Passes gained first-use activation. Backfill the TTL for rows issued
        # before this change so their remaining validity can be recomputed.
        async with self.conn.execute("PRAGMA table_info(passes)") as cur:
            pass_columns = {row[1] for row in await cur.fetchall()}
        if "ttl_seconds" not in pass_columns:
            await self.conn.execute("ALTER TABLE passes ADD COLUMN ttl_seconds INTEGER")
        if "first_used_at" not in pass_columns:
            await self.conn.execute("ALTER TABLE passes ADD COLUMN first_used_at TEXT")
        if "token_plain" not in pass_columns:
            await self.conn.execute("ALTER TABLE passes ADD COLUMN token_plain TEXT")
        await self.conn.execute(
            "UPDATE passes SET ttl_seconds = CAST("
            "(julianday(expires_at) - julianday(created_at)) * 86400 AS INTEGER) "
            "WHERE ttl_seconds IS NULL"
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

    # ------------------------------------------------------------------
    # Access passes. Only the SHA-256 hash of a token is stored; the raw
    # token exists exactly once — in the CLI output at issue time.
    # ------------------------------------------------------------------

    async def create_pass(
        self,
        token: str,
        kind: str,
        expires_at: str,
        label: str = "",
        quota_tokens: int | None = None,
        ttl_seconds: int | None = None,
    ) -> int:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        await self.conn.execute(
            "INSERT INTO passes(token_hash, token_plain, kind, label, quota_tokens, "
            "ttl_seconds, first_used_at, created_at, expires_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 0)",
            (
                token_hash,
                token,
                kind,
                label,
                quota_tokens,
                ttl_seconds,
                datetime.now(timezone.utc).isoformat(),
                expires_at,
            ),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT id FROM passes WHERE token_hash=?", (token_hash,)
        ) as cur:
            (pass_id,) = await cur.fetchone()
        return pass_id

    async def get_pass(self, pass_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT id, kind, label, quota_tokens, ttl_seconds, first_used_at, "
            "created_at, expires_at, revoked, token_plain FROM passes WHERE id=?",
            (pass_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._pass_row(row) if row else None

    async def pass_by_token(self, token: str) -> dict | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        async with self.conn.execute(
            "SELECT id, kind, label, quota_tokens, ttl_seconds, first_used_at, "
            "created_at, expires_at, revoked, token_plain FROM passes WHERE token_hash=?",
            (token_hash,),
        ) as cur:
            row = await cur.fetchone()
        return self._pass_row(row) if row else None

    @staticmethod
    def _pass_row(row) -> dict:
        return {
            "id": row[0],
            "kind": row[1],
            "label": row[2],
            "quota_tokens": row[3],
            "ttl_seconds": row[4],
            "first_used_at": row[5],
            "created_at": row[6],
            "expires_at": row[7],
            "revoked": bool(row[8]),
            # Plaintext copy kept so `pass list` can show it; NULL for rows
            # issued before this column existed (hash-only, unrecoverable).
            "token": row[9],
        }

    async def activate_pass(self, pass_row: dict) -> dict:
        """Start a temp pass's clock on its first successful validation.

        Issue time says nothing about when the guest actually opens the link,
        so an unused pass keeps its full TTL: first validation stamps
        first_used_at and moves expires_at to now + ttl. Long passes pass
        through unchanged. The WHERE guard makes the first concurrent request
        win; the result is re-read so callers see the definitive expiry.
        """
        if pass_row["kind"] != "temp" or pass_row["first_used_at"] is not None:
            return pass_row
        ttl = pass_row["ttl_seconds"]
        if not ttl or ttl <= 0:
            return pass_row
        now = datetime.now(timezone.utc)
        await self.conn.execute(
            "UPDATE passes SET first_used_at=?, expires_at=? "
            "WHERE id=? AND first_used_at IS NULL",
            (
                now.isoformat(),
                (now + timedelta(seconds=ttl)).isoformat(),
                pass_row["id"],
            ),
        )
        await self.conn.commit()
        return await self.get_pass(pass_row["id"]) or pass_row

    async def revoke_pass(self, pass_id: int) -> bool:
        cursor = await self.conn.execute(
            "UPDATE passes SET revoked=1 WHERE id=?", (pass_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def list_passes(self) -> list[dict]:
        async with self.conn.execute(
            "SELECT id, kind, label, quota_tokens, ttl_seconds, first_used_at, "
            "created_at, expires_at, revoked, token_plain FROM passes ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for row in rows:
            item = self._pass_row(row)
            item["used_tokens"] = await self.pass_usage(row[0])
            result.append(item)
        return result

    async def link_pass_session(self, pass_id: int, session_id: str) -> None:
        """Attribute a session to a pass from now on. First link wins on `since`.

        Re-links (a session resumed under a different pass) keep the earliest
        timestamp, so per-pass usage sums never count the same call twice.
        """
        await self.conn.execute(
            "INSERT OR IGNORE INTO pass_sessions(pass_id, session_id, since) "
            "VALUES (?, ?, ?)",
            (pass_id, session_id, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def pass_usage(self, pass_id: int) -> int:
        """Real model tokens burned by sessions linked to this pass.

        stage_metrics is the single source of truth: usage sums rows whose
        started_at is at or after the first link time, so scoring jobs that
        finish after the turn are still counted.
        """
        async with self.conn.execute(
            "SELECT COALESCE(SUM(sm.tokens), 0) FROM stage_metrics sm "
            "JOIN pass_sessions ps ON ps.session_id = sm.session_id "
            "AND ps.pass_id=? WHERE sm.started_at >= ps.since",
            (pass_id,),
        ) as cur:
            (total,) = await cur.fetchone()
        return int(total)
