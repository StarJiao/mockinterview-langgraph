"""Stage timing: durable call metrics plus a process-local live view.

Every real model call writes one row to operations.sqlite (stage_metrics). The
live tracker only answers "what is running right now" for the waiting UI; it is
not business state and is never written to a checkpoint.
"""

import time
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

STAGE_LABELS = (
    ("plan", "生成考点计划"),
    ("question:", "生成下一题"),
    ("coverage:", "快评决策"),
    ("probe:", "生成追问"),
    ("closing:", "本题收尾"),
    ("eval:", "单题评分"),
)


def stage_label(key: str) -> str:
    for prefix, name in STAGE_LABELS:
        if key.startswith(prefix):
            return name
    return "模型调用"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StageTracker:
    """Process-local live progress; durable records live in OperationStore."""

    def __init__(self):
        self._active: dict[tuple[str, str], float] = {}
        self._recent: dict[str, list[dict]] = defaultdict(list)

    @staticmethod
    def label(key: str) -> str:
        return stage_label(key)

    def start(self, session_id: str, key: str):
        self._active[(session_id, key)] = time.monotonic()

    def end(self, session_id: str, key: str):
        started = self._active.pop((session_id, key), None)
        if started is None:
            return
        rows = self._recent[session_id]
        rows.append(
            {
                "key": key,
                "label": stage_label(key),
                "ms": int((time.monotonic() - started) * 1000),
            }
        )
        del rows[:-20]

    def current(self, session_id: str) -> dict | None:
        now = time.monotonic()
        running = [
            (started, key)
            for (sid, key), started in self._active.items()
            if sid == session_id
        ]
        if not running:
            return None
        started, key = min(running)
        return {"key": key, "label": stage_label(key), "ms": int((now - started) * 1000)}

    def recent(self, session_id: str) -> list[dict]:
        return list(self._recent.get(session_id, []))


class StageRecorder:
    """Time one real model call and persist the row, success or failure.

    Callers mutate the yielded dict to report tokens, attempts and failure. Only
    the exception class name is stored: provider messages may contain keys.
    Diagnostics must never break an interview, so a failed metric write is
    swallowed rather than replacing the original model result or exception.
    """

    def __init__(self, store, tracker=None):
        self.store, self.tracker = store, tracker

    @asynccontextmanager
    async def stage(self, session_id: str, key: str):
        started = time.monotonic()
        started_at = _utcnow()
        metric = {"tokens": 0, "ok": True, "error": "", "attempts": 1}
        if self.tracker:
            self.tracker.start(session_id, key)
        try:
            yield metric
        except Exception as exc:
            metric["tokens"] = int(getattr(exc, "tokens", 0) or 0)
            metric["ok"] = False
            metric["error"] = type(exc).__name__
            raise
        finally:
            with suppress(Exception):
                await self.store.add_stage_metric(
                    session_id,
                    {
                        "stage_key": key,
                        "label": stage_label(key),
                        "started_at": started_at,
                        "finished_at": _utcnow(),
                        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                        **metric,
                    },
                )
            if self.tracker:
                self.tracker.end(session_id, key)


@asynccontextmanager
async def null_stage():
    """Used when a runtime has no recorder (direct unit construction)."""
    yield {"tokens": 0, "ok": True, "error": "", "attempts": 1}
