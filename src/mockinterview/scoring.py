"""Process-local background jobs; only successful receipts are durable."""

import asyncio
from copy import deepcopy

from .telemetry import null_stage


class ScoringJobs:
    def __init__(self, backend, store, notify, recorder=None):
        self.backend, self.store, self.notify = backend, store, notify
        self.recorder = recorder
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.errors: dict[tuple[str, str], Exception] = {}

    async def submit(self, session_id: str, question: dict):
        task_key = (session_id, question["id"])
        operation_key = f"eval:{question['id']}"
        if task_key in self.tasks or await self.store.get(session_id, operation_key):
            return
        self.notify(f"后台评分中：{question['topic']}（不阻塞答题）")
        self.tasks[task_key] = asyncio.create_task(
            self._evaluate(session_id, deepcopy(question)), name=operation_key
        )

    async def _evaluate(self, session_id: str, question: dict):
        key = (session_id, question["id"])
        stage_key = f"eval:{question['id']}"
        stage = self.recorder.stage(session_id, stage_key) if self.recorder else null_stage()
        async with stage as metric:
            for attempt in range(2):
                try:
                    result = await self.backend.evaluate(question)
                    metric["tokens"] += int(result.tokens or 0)
                    metric["attempts"] = attempt + 1
                    await self.store.put(session_id, stage_key, result)
                    await self.store.clear_score_failure(session_id, question["id"])
                    self.notify(f"单题评分已保存：{question['topic']}")
                    return
                except Exception as exc:
                    used = int(getattr(exc, "tokens", 0) or 0)
                    metric["tokens"] += used
                    metric["attempts"] = attempt + 1
                    await self.store.record_failed_usage(session_id, used)
                    if attempt == 1:
                        metric["ok"] = False
                        metric["error"] = type(exc).__name__
                        self.errors[key] = exc
                        await self.store.set_score_failure(
                            session_id, question["id"], type(exc).__name__
                        )
                        self.notify(
                            f"单题评分失败：{question['topic']}（{type(exc).__name__}），可单独补评"
                        )

    def retry_failed(self, session_id: str):
        for key in list(self.errors):
            if key[0] == session_id:
                self.errors.pop(key)
                self.tasks.pop(key, None)

    async def wait(self, session_id: str):
        tasks = [task for key, task in self.tasks.items() if key[0] == session_id]
        if tasks:
            await asyncio.gather(*tasks)

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
