"""Local Web UI over the same LangGraph runtime as the CLI.

Access control is a two-layer split:

- The pass cookie gates the whole /api surface. Passes are issued by the CLI
  (`mockinterview pass new`): long-lived ones for the owner, temp ones with a
  token quota for guests. Only the SHA-256 hash of a token is stored.
- The username cookie is still just a data grouping label: any name entered
  with a valid pass is created on first use.

Model-consuming operations (create / answer / resume) additionally require
temp-pass quota headroom; reads and history stay open after exhaustion.

The server binds to all interfaces by default; --host 127.0.0.1 restricts it.
All interview rules live in the graph; this layer only drives it.
"""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import quote, unquote
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .report import render_report
from .runtime import InterviewApp
from .state import round_label
from .telemetry import StageTracker

STATIC_DIR = Path(__file__).parent / "web_static"
COOKIE = "mi_user"
PASS_COOKIE = "mi_pass"
LONG_PASS_MAX_AGE = 365 * 24 * 3600
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .-]{0,31}$")

TOPICS = [
    "综合",
    "Go 并发",
    "Redis 底层",
    "MySQL 与索引",
    "系统设计",
    "Agent 与 RAG",
    "MCP 与 Context Engineering",
    "分布式与一致性",
    "网络与操作系统",
]


class LoginBody(BaseModel):
    username: str = ""
    pass_token: str = ""


class NewInterviewBody(BaseModel):
    topic: str = ""
    num_questions: int = 0
    resume: str = ""
    jds: list[str] = Field(default_factory=list)


class AnswerBody(BaseModel):
    answer: str = ""
    confirm_empty: bool = False


def build_messages(state: dict, pending: dict | None, events: list[dict]) -> list[dict]:
    """Rebuild the three-role stream from checkpointed facts.

    Interviewer/candidate lines come from Graph State, so old CLI sessions replay
    correctly. System lines are derived from persisted decisions, plus recorded
    Web notifications anchored to the round that produced them.
    """
    options = state.get("options", {})
    questions = state.get("questions", [])
    evaluations = state.get("evaluations", {})
    failures = state.get("score_failures", {})
    total = len(state.get("plan", [])) or options.get("num_questions", 0)
    jds = [jd for jd in options.get("jds", []) or [] if jd.strip()]
    jd_count = len(jds) or (1 if options.get("jd", "").strip() else 0)
    base: list[dict] = []

    def add(role: str, label: str, text: str, anchor: str):
        base.append({"role": role, "label": label, "text": text, "anchor": anchor})

    add(
        "system",
        "系统",
        f"会话已创建 · 方向 {' / '.join(options.get('focus', [])) or '综合'} · 计划 {total} 题"
        f" · 目标 JD {jd_count} 份 · 简历{'已提供' if options.get('resume', '').strip() else '未提供'}",
        "start",
    )
    for qi, question in enumerate(questions, 1):
        qid = question["id"]
        add(
            "system",
            "系统",
            f"第 {qi}/{total} 题 · {question.get('topic', '')}"
            f" · 难度 {question.get('difficulty', '—')}",
            f"q:{qid}:open",
        )
        for ri, round_ in enumerate(question.get("rounds", [])):
            anchor = f"q:{qid}:r{ri}"
            add(
                "interviewer",
                "面试官",
                f"{round_label(question, ri, with_limit=True)}：{round_['ask']}",
                anchor,
            )
            answer = round_.get("answer")
            if answer is not None:
                add("candidate", "候选人", answer or "（已提交空回答）", anchor)
            decision = round_.get("decision")
            if isinstance(decision, dict):
                assessment = round_.get("assessment") or {}
                score = assessment.get("score")
                suffix = f" · 覆盖分 {score}/100" if score is not None else ""
                add("system", "系统", f"快评决策：{decision.get('reason', '')}{suffix}", anchor)
        if question.get("closing"):
            add("interviewer", "面试官", f"收尾：{question['closing']}", f"q:{qid}:close")
        if question.get("status") == "done":
            add(
                "system",
                "系统",
                f"本题结束 · {question.get('closing_reason', '') or '未记录原因'}",
                f"q:{qid}:close",
            )
            evaluation = evaluations.get(qid)
            if evaluation:
                score = evaluation.get("score") or "未计算总分（部分维度证据不足）"
                add("system", "系统", f"单题评分已保存 · {score}", f"q:{qid}:close")
            elif qid in failures:
                add(
                    "system",
                    "系统",
                    f"单题评分失败（{failures[qid]}），可稍后补评",
                    f"q:{qid}:close",
                )
            else:
                add("system", "系统", "单题评分尚未完成，后台任务仍在运行", f"q:{qid}:close")
    if state.get("status") == "done":
        tokens = sum(state.get("usage", {}).values())
        add(
            "system",
            "系统",
            f"面试完成 · 共 {len(questions)} 题 · 已收集 {len(evaluations)} 份评分"
            f" · 已知 token {tokens}",
            "end",
        )
    elif pending:
        add("system", "系统", "等待候选人作答", "end")

    by_anchor: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        by_anchor[event["anchor"]].append(
            {
                "role": event["role"],
                "label": "系统" if event["role"] == "system" else event["role"],
                "text": event["text"],
                "anchor": event["anchor"],
            }
        )
    stream = []
    for index, message in enumerate(base):
        stream.append(message)
        last_of_anchor = index == len(base) - 1 or base[index + 1]["anchor"] != message["anchor"]
        if last_of_anchor:
            stream.extend(by_anchor.pop(message["anchor"], []))
    for anchor in sorted(by_anchor):
        stream.extend(by_anchor[anchor])
    return stream


def pending_anchor(turn) -> str:
    pending = turn.pending
    if not pending:
        return "end"
    for question in turn.state.get("questions", []):
        if question["id"] == pending["question_id"]:
            return f"q:{question['id']}:r{len(question.get('rounds', [])) - 1}"
    return "end"


class WebService:
    """One process-wide InterviewApp; per-session asyncio locks serialize turns."""

    def __init__(self, backend, data_dir, options_base: dict, notify=None):
        self.backend = backend
        self.data_dir = Path(data_dir)
        self.options_base = dict(options_base)
        # Cookies must carry Secure once the site is served over HTTPS;
        # the CLI flips this via --secure-cookie (no-op on plain HTTP).
        self.secure_cookies = bool(self.options_base.get("secure_cookies", False))
        self.notify = notify or (lambda text: print(f"[系统] {str(text).replace(chr(27), '')}"))
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.stage = StageTracker()

    async def open(self):
        self.app = InterviewApp(
            self.data_dir, self.backend, notify=self.notify, stage_tracker=self.stage
        )
        await self.app.__aenter__()
        self.store = self.app.store

    async def close(self):
        running = [task for task in self.tasks.values() if not task.done()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self.app.__aexit__(None, None, None)

    def busy(self, session_id: str) -> bool:
        task = self.tasks.get(session_id)
        return bool(task and not task.done())

    def scoring_pending(self, session_id: str) -> bool:
        """Scores can still land after a turn ends; keep polling until they do."""
        return any(
            key[0] == session_id and not task.done()
            for key, task in self.app.jobs.tasks.items()
        )

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    async def launch(self, session_id: str, factory):
        async with self._guard:
            if self.busy(session_id):
                raise HTTPException(status_code=409, detail="这场面试正在处理上一轮，请稍候")
            self.tasks[session_id] = asyncio.create_task(self._run(session_id, factory))

    async def _run(self, session_id: str, factory):
        async with self._lock(session_id):
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never echo provider exception bodies: they may contain keys.
                await self.store.add_event(
                    session_id, "end", f"本轮执行中断：{type(exc).__name__}；已落盘进度可重试"
                )

    async def wait(self, session_id: str):
        task = self.tasks.get(session_id)
        if task:
            await task


def service_of(request: Request) -> WebService:
    return request.app.state.service


async def current_user(request: Request, service: WebService = Depends(service_of)) -> str:
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="请先填写用户名登录")
    username = unquote(raw)
    if not USERNAME_RE.fullmatch(username) or not await service.store.has_user(username):
        raise HTTPException(status_code=401, detail="登录状态无效，请重新填写用户名")
    return username


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pass_is_valid(pass_row: dict) -> bool:
    if pass_row is None or pass_row["revoked"]:
        return False
    try:
        expires = datetime.fromisoformat(pass_row["expires_at"])
    except ValueError:
        return False
    return expires > _utcnow()


async def fetch_pass(token: str, service: WebService) -> dict | None:
    """Look up a raw token and start its clock if this is its first use.

    Every entry point (gate middleware, login, exchange route) must come
    through here so an unused temp pass gets its full TTL from first contact.
    """
    if not token:
        return None
    pass_row = await service.store.pass_by_token(token)
    if pass_row is None:
        return None
    return await service.store.activate_pass(pass_row)


def pass_max_age(pass_row: dict) -> int:
    """Cookie lifetime = remaining validity; temp passes die with the pass."""
    expires = datetime.fromisoformat(pass_row["expires_at"])
    remaining = int((expires - _utcnow()).total_seconds())
    if pass_row["kind"] == "long":
        return min(remaining, LONG_PASS_MAX_AGE)
    return max(remaining, 0)


def pass_info(pass_row: dict, used_tokens: int | None = None) -> dict:
    info = {
        "kind": pass_row["kind"],
        "label": pass_row["label"],
        "expires_at": pass_row["expires_at"],
        "quota_tokens": pass_row["quota_tokens"],
    }
    if pass_row["kind"] == "temp":
        info["used_tokens"] = used_tokens
    return info


async def require_write_quota(pass_row: dict, service: WebService) -> None:
    """Temp passes need quota headroom for anything that calls the models."""
    if pass_row["kind"] != "temp" or pass_row["quota_tokens"] is None:
        return
    used = await service.store.pass_usage(pass_row["id"])
    if used >= pass_row["quota_tokens"]:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "令牌配额已用完：可继续查看历史与报告，继续面试需要向作者索取新令牌",
                "quota_exhausted": True,
            },
        )


async def require_owned(session_id: str, username: str, service: WebService):
    owner = await service.store.owner(session_id)
    if owner is None or owner != username:
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户")


async def inspect_or_retry(session_id: str, service: WebService):
    try:
        return await service.app.inspect(session_id)
    except ValueError:
        raise HTTPException(
            status_code=409, detail="会话仍在初始化，请稍候再试"
        ) from None


def ensure_backend_match(turn, service: WebService):
    """The CLI already forbids this; a Web service has one backend for all sessions."""
    saved_demo = bool(turn.state.get("options", {}).get("demo", False))
    running_demo = bool(service.options_base.get("demo", False))
    if saved_demo != running_demo:
        saved = "离线演示" if saved_demo else "真实模型"
        running = "离线演示" if running_demo else "真实模型"
        raise HTTPException(
            status_code=409,
            detail=f"这场面试由{saved}创建，当前服务是{running}；请用匹配的启动参数重开实例",
        )


def session_meta(session_id: str, state: dict, pending: dict | None, done: bool) -> dict:
    options = state.get("options", {})
    questions = state.get("questions", [])
    jds = [jd for jd in options.get("jds", []) or [] if jd.strip()]
    return {
        "id": session_id,
        "status": "done" if done else ("等待回答" if pending else "可恢复"),
        "topic": " / ".join(options.get("focus", [])) or "综合",
        "num_questions": len(state.get("plan", [])) or options.get("num_questions", 0),
        "completed": sum(q.get("status") == "done" for q in questions),
        "jd_count": len(jds) or (1 if options.get("jd", "").strip() else 0),
        "has_resume": bool(options.get("resume", "").strip()),
        "demo": bool(options.get("demo", False)),
        "tokens": sum(state.get("usage", {}).values()),
    }


def create_app(service: WebService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await service.open()
        yield
        await service.close()

    app = FastAPI(title="Mock Interview", lifespan=lifespan)
    app.state.service = service

    @app.middleware("http")
    async def pass_gate(request: Request, call_next):
        """One gate for the whole /api surface; the pass cookie is the door.

        /api/login carries the token in its body, static pages must load so the
        login form can be shown at all — everything else needs a valid pass.
        The validated row is stashed on request.state for quota checks.
        """
        path = request.url.path
        if path.startswith("/api/") and path != "/api/login":
            raw = request.cookies.get(PASS_COOKIE)
            pass_row = await fetch_pass(raw, service)
            if not pass_is_valid(pass_row):
                detail = (
                    "访问令牌无效或已过期，请联系作者重新获取"
                    if raw
                    else "需要访问令牌：请通过带 ?pass= 的链接进入，或在登录框填入令牌"
                )
                return JSONResponse({"detail": detail}, status_code=401)
            request.state.pass_row = pass_row
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/app.css")
    async def stylesheet():
        return FileResponse(STATIC_DIR / "app.css", media_type="text/css")

    @app.get("/app.js")
    async def script():
        return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")

    @app.get("/api/topics")
    async def topics():
        return {"topics": TOPICS}

    @app.post("/api/login")
    async def login(body: LoginBody, service: WebService = Depends(service_of)):
        username = body.username.strip()
        if not USERNAME_RE.fullmatch(username):
            raise HTTPException(
                status_code=400,
                detail="用户名需为 1–32 个中英文、数字、空格、点或连字符，且不能以符号开头",
            )
        token = body.pass_token.strip()
        pass_row = await fetch_pass(token, service)
        if not pass_is_valid(pass_row):
            raise HTTPException(
                status_code=401, detail="令牌无效或已过期，请联系作者重新获取"
            )
        await service.store.ensure_user(username)
        response = JSONResponse({"username": username})
        response.set_cookie(
            COOKIE, quote(username, safe=""), httponly=True, samesite="lax", path="/",
            secure=service.secure_cookies,
        )
        response.set_cookie(
            PASS_COOKIE, token, httponly=True, samesite="lax", path="/",
            max_age=pass_max_age(pass_row), secure=service.secure_cookies,
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        response.delete_cookie(PASS_COOKIE, path="/")
        return response

    @app.get("/pass/{token}")
    async def exchange_pass(token: str, service: WebService = Depends(service_of)):
        """Validate the URL token up front, then hand it back to the login form.

        The pass cookie alone cannot complete login (the username cookie is
        separate), and the form cannot read an httponly cookie — so the token
        rides one more hop via ?pass= for the frontend to prefill.
        """
        token = token.strip()
        pass_row = await fetch_pass(token, service)
        if not pass_is_valid(pass_row):
            return RedirectResponse("/?pass_error=1", status_code=302)
        response = RedirectResponse(f"/?pass={token}", status_code=302)
        response.set_cookie(
            PASS_COOKIE, token, httponly=True, samesite="lax", path="/",
            max_age=pass_max_age(pass_row), secure=service.secure_cookies,
        )
        return response

    @app.get("/api/me")
    async def me(
        request: Request,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        pass_row = request.state.pass_row
        used = (
            await service.store.pass_usage(pass_row["id"])
            if pass_row["kind"] == "temp"
            else None
        )
        return {"username": username, "pass": pass_info(pass_row, used)}

    @app.get("/api/interviews")
    async def list_interviews(
        username: str = Depends(current_user), service: WebService = Depends(service_of)
    ):
        sessions = await service.app.sessions(username)
        return {"sessions": sessions}

    @app.post("/api/interviews", status_code=202)
    async def new_interview(
        body: NewInterviewBody,
        request: Request,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        await require_write_quota(request.state.pass_row, service)
        topic = body.topic.strip()
        if not topic or len(topic) > 60:
            raise HTTPException(status_code=400, detail="主题为必填项，且不超过 60 字")
        if not 1 <= body.num_questions <= 10:
            raise HTTPException(status_code=400, detail="题目数量为必填项，范围 1–10")
        if len(body.resume) > 10000:
            raise HTTPException(status_code=400, detail="简历过长（上限 10000 字）")
        jds = [jd.strip() for jd in body.jds if jd.strip()]
        if len(jds) > 5 or any(len(jd) > 4000 for jd in jds):
            raise HTTPException(status_code=400, detail="目标 JD 最多 5 份，每份不超过 4000 字")
        options = {
            **service.options_base,
            "jd": "\n\n".join(jds),
            "jds": jds,
            "resume": body.resume,
            "state_snapshot": "",
            "focus": [item.strip() for item in topic.split(",") if item.strip()] or [topic],
            "num_questions": body.num_questions,
            "preset_questions": None,
        }
        session_id = f"mock-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
        await service.store.register(session_id, username)
        await service.store.link_pass_session(request.state.pass_row["id"], session_id)
        await service.store.add_event(
            session_id,
            "start",
            f"由 {username} 新建面试 · 主题 {topic} · {body.num_questions} 题",
        )
        await service.launch(
            session_id,
            lambda: service.app.start(
                options, session_id=session_id, username=username, registered=True
            ),
        )
        return {"id": session_id, "busy": True}

    @app.get("/api/interviews/{session_id}")
    async def detail(
        session_id: str,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        await require_owned(session_id, username, service)
        busy = service.busy(session_id)
        try:
            turn = await service.app.inspect(session_id)
        except ValueError:
            if not busy:
                raise HTTPException(status_code=404, detail="会话尚无可用进度") from None
            return {
                "session": {"id": session_id, "status": "初始化中"},
                "messages": [],
                "pending": None,
                "done": False,
                "busy": True,
                "initializing": True,
                "revision": await service.store.revision(session_id),
                "metrics": await service.store.stage_metrics(session_id),
                "metrics_summary": await service.store.stage_summary(session_id),
            }
        events = await service.store.events(session_id)
        return {
            "session": session_meta(session_id, turn.state, turn.pending, turn.done),
            "messages": build_messages(turn.state, turn.pending, events),
            "pending": turn.pending,
            "done": turn.done,
            "busy": busy,
            "initializing": False,
            "scoring_pending": service.scoring_pending(session_id),
            "revision": await service.store.revision(session_id),
            "metrics": await service.store.stage_metrics(session_id),
            "metrics_summary": await service.store.stage_summary(session_id),
        }

    @app.get("/api/interviews/{session_id}/status")
    async def status(
        session_id: str,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        """Heartbeat: three indexed counts plus process-local live stage timing."""
        await require_owned(session_id, username, service)
        current = service.stage.current(session_id)
        recent = service.stage.recent(session_id)
        return {
            "busy": service.busy(session_id),
            "scoring_pending": service.scoring_pending(session_id),
            "revision": await service.store.revision(session_id),
            "stage": current["label"] if current else None,
            "stage_ms": current["ms"] if current else 0,
            "last_stage": recent[-1] if recent else None,
        }

    @app.get("/api/interviews/{session_id}/timings")
    async def timings(
        session_id: str,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        """Durable per-call metrics plus the process-local live view."""
        await require_owned(session_id, username, service)
        return {
            "stages": await service.store.stage_metrics(session_id),
            "summary": await service.store.stage_summary(session_id),
            "live": {
                "current": service.stage.current(session_id),
                "recent": service.stage.recent(session_id),
            },
        }

    @app.post("/api/interviews/{session_id}/answer", status_code=202)
    async def answer(
        session_id: str,
        body: AnswerBody,
        request: Request,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        await require_owned(session_id, username, service)
        await require_write_quota(request.state.pass_row, service)
        turn = await inspect_or_retry(session_id, service)
        ensure_backend_match(turn, service)
        if turn.done:
            raise HTTPException(status_code=409, detail="面试已完成，只能查看报告，不能继续作答")
        if service.busy(session_id):
            raise HTTPException(status_code=409, detail="这场面试正在处理上一轮，请稍候")
        if not body.answer.strip() and not body.confirm_empty:
            raise HTTPException(
                status_code=400,
                detail={"message": "空白答案需要再次确认提交", "need_confirm": True},
            )
        if len(body.answer) > 20000:
            raise HTTPException(status_code=400, detail="回答过长（上限 20000 字）")
        await service.store.link_pass_session(request.state.pass_row["id"], session_id)
        await service.store.add_event(
            session_id, pending_anchor(turn), "候选人已提交本轮回答，等待生成下一步"
        )
        await service.launch(
            session_id, lambda: service.app.resume(session_id, answer=body.answer)
        )
        return {"id": session_id, "busy": True}

    @app.post("/api/interviews/{session_id}/resume", status_code=202)
    async def resume(
        session_id: str,
        request: Request,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        await require_owned(session_id, username, service)
        await require_write_quota(request.state.pass_row, service)
        turn = await inspect_or_retry(session_id, service)
        ensure_backend_match(turn, service)
        if turn.done:
            raise HTTPException(status_code=409, detail="面试已完成，只能查看报告，不能继续")
        if service.busy(session_id):
            raise HTTPException(status_code=409, detail="这场面试正在处理上一轮，请稍候")
        await service.store.link_pass_session(request.state.pass_row["id"], session_id)
        await service.store.add_event(session_id, "end", "已恢复未完成步骤，继续推进流程")
        await service.launch(session_id, lambda: service.app.resume(session_id))
        return {"id": session_id, "busy": True}

    @app.get("/api/interviews/{session_id}/report")
    async def report(
        session_id: str,
        username: str = Depends(current_user),
        service: WebService = Depends(service_of),
    ):
        await require_owned(session_id, username, service)
        turn = await inspect_or_retry(session_id, service)
        return {"markdown": render_report(turn.state), "done": turn.done}

    return app


async def serve(backend, data_dir, options_base: dict, host: str, port: int, notify=None):
    import uvicorn

    service = WebService(backend, Path(data_dir), options_base, notify=notify)
    application = create_app(service)
    server = uvicorn.Server(
        uvicorn.Config(application, host=host, port=port, log_level="info")
    )
    await server.serve()
