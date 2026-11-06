"""Process-local background jobs; only successful receipts are durable."""

import asyncio
from copy import deepcopy


class ScoringJobs:
    def __init__(self, backend, store, notify):
        self.backend, self.store, self.notify = backend, store, notify
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
        for attempt in range(2):
            try:
                result = await self.backend.evaluate(question)
                await self.store.put(session_id, f"eval:{question['id']}", result)
                self.notify(f"单题评分已保存：{question['topic']}")
                return
            except Exception as exc:
                await self.store.record_failed_usage(session_id, getattr(exc, "tokens", 0))
                if attempt == 1:
                    self.errors[key] = exc
                    self.notify(f"单题评分失败：{question['topic']}，下次恢复可补评")

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
