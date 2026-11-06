"""Application lifetime: durable graph, disposable score workers, one writer per session."""

import fcntl
import re
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .graph import build_graph
from .nodes import Nodes
from .persistence import OperationStore
from .scoring import ScoringJobs


@dataclass
class Turn:
    session_id: str
    state: dict
    pending: dict | None
    done: bool


class InterviewApp:
    def __init__(self, data_dir, backend, notify=lambda _: None):
        self.data_dir = Path(data_dir)
        self.backend, self.notify = backend, notify
        self._locks = {}

    async def __aenter__(self):
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        try:
            self.checkpointer = await self.stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(self.data_dir / "checkpoints.sqlite"))
            )
            self.store = OperationStore(self.data_dir / "operations.sqlite")
            await self.store.open()
            self.stack.push_async_callback(self.store.close)
            self.jobs = ScoringJobs(self.backend, self.store, self.notify)
            self.stack.push_async_callback(self.jobs.close)
            self.nodes = Nodes(self.backend, self.store, self.jobs, self.notify)
            self.graph = build_graph(self.nodes, self.checkpointer)
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        try:
            await self.stack.aclose()
        finally:
            for file in self._locks.values():
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
                file.close()

    def _config(self, session_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
            raise ValueError("会话 ID 只能包含字母、数字、下划线和连字符，长度不超过 100")
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 10000}

    def _lock(self, session_id):
        self._config(session_id)
        if session_id in self._locks:
            return
        locks = self.data_dir / "locks"
        locks.mkdir(exist_ok=True)
        file = (locks / f"{session_id}.lock").open("a")
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            raise ValueError("这场面试已被另一进程打开，请先退出那个进程") from None
        self._locks[session_id] = file

    async def start(self, options: dict, session_id: str | None = None) -> Turn:
        options = {
            "jd": "",
            "resume": "",
            "state_snapshot": "",
            "focus": ["Go 后端"],
            "num_questions": 3,
            "token_budget": 200000,
            **options,
        }
        if options["num_questions"] <= 0 or options["token_budget"] < 0:
            raise ValueError("题数必须大于零，预算不得为负数（0 表示不限）")
        session_id = session_id or f"mock-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        self._lock(session_id)
        config = self._config(session_id)
        snapshot = await self.graph.aget_state(config)
        ids = {row[0] for row in await self.store.list_sessions()}
        if snapshot.values or session_id in ids:
            raise ValueError(f"会话 {session_id} 已存在，请使用 resume")
        await self.store.register(session_id)
        self.notify(f"会话 ID：{session_id}；中断后用 resume {session_id} 继续")
        await self.graph.ainvoke(
            {
                "session_id": session_id,
                "options": options,
                "plan": [],
                "questions": [],
                "decision": {},
                "evaluations": {},
                "usage": {},
                "status": "running",
            },
            config,
            durability="sync",
        )
        return await self.inspect(session_id)

    async def inspect(self, session_id: str) -> Turn:
        snapshot = await self.graph.aget_state(self._config(session_id))
        if not snapshot.values:
            raise ValueError(f"未找到可恢复的会话：{session_id}")
        state = deepcopy(snapshot.values)
        # Read-only projection of durable receipts; never use it to infer the next node.
        collected = await self.nodes.collected(state)
        for key in ("evaluations", "usage"):
            state[key] = {**state.get(key, {}), **collected[key]}
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        pending = interrupts[0].value if interrupts else None
        return Turn(session_id, state, pending, state.get("status") == "done")

    async def resume(self, session_id: str, answer: str | None = None) -> Turn:
        self._lock(session_id)
        turn = await self.inspect(session_id)
        if turn.done:
            if answer is not None:
                raise ValueError("会话已结束，不能继续提交答案")
            return turn
        if answer is not None and not isinstance(answer, str):
            raise ValueError("答案必须是字符串")
        if answer is not None and turn.pending is None:
            raise ValueError("当前没有等待回答的中断点，请先恢复未完成步骤")
        self.jobs.retry_failed(session_id)
        for q in turn.state["questions"]:
            if q["status"] == "done":
                await self.jobs.submit(session_id, q)
        if turn.pending is not None and answer is None:
            return turn
        value = Command(resume=answer) if answer is not None else None
        await self.graph.ainvoke(value, self._config(session_id), durability="sync")
        return await self.inspect(session_id)

    async def sessions(self) -> list[dict]:
        rows = []
        for session_id, created in await self.store.list_sessions():
            snapshot = await self.graph.aget_state(self._config(session_id))
            if not snapshot.values:
                status = "未生成 checkpoint"
            elif snapshot.values.get("status") == "done":
                status = "done"
            elif any(task.interrupts for task in snapshot.tasks):
                status = "等待回答"
            else:
                status = "可恢复"
            rows.append(
                {"id": session_id, "status": status, "updated_at": snapshot.created_at or created}
            )
        return rows
