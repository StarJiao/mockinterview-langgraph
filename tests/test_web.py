"""Web UI contract tests use the offline backend and a real SQLite data dir."""

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx

from mockinterview.config import Config
from mockinterview.identity import CLI_USER
from mockinterview.models import DemoBackend, ModelBackend
from mockinterview.persistence import OperationStore
from mockinterview.web import WebService, create_app

OPTIONS_BASE = {
    "token_budget": 200000,
    "demo": True,
    "prompt_version": "original",
    "questioner_model": "demo",
    "evaluator_model": "demo",
}


@pytest.fixture
async def web(tmp_path):
    service = WebService(DemoBackend(), tmp_path, OPTIONS_BASE, notify=lambda _: None)
    application = create_app(service)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            yield client, service, application


async def make_pass(service, kind="long", hours=1, quota=None):
    """Issue a real pass row; returns (raw_token, pass_id)."""
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=hours)).isoformat()
    ttl_seconds = hours * 3600 if kind == "temp" else None
    pass_id = await service.store.create_pass(
        token, kind, expires, quota_tokens=quota, ttl_seconds=ttl_seconds
    )
    return token, pass_id


async def login(client, username="alice", service=None, token=None):
    if token is None:
        assert service is not None, "login needs either a token or the service"
        token, _ = await make_pass(service)
    response = await client.post(
        "/api/login", json={"username": username, "pass_token": token}
    )
    assert response.status_code == 200
    return response.json()


async def new_interview(client, **overrides):
    payload = {"topic": "Go 并发", "num_questions": 1, "resume": "", "jds": []}
    payload.update(overrides)
    response = await client.post("/api/interviews", json=payload)
    return response


async def settle(client, service, session_id):
    await service.wait(session_id)
    response = await client.get(f"/api/interviews/{session_id}")
    assert response.status_code == 200
    return response.json()


async def test_login_requires_username_and_static_pages_are_public(web):
    client, service, _ = web
    assert (await client.get("/api/me")).status_code == 401
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/app.js")).status_code == 200
    assert (await client.post("/api/login", json={"username": "bad/name"})).status_code == 400
    assert (await login(client, service=service))["username"] == "alice"
    me = (await client.get("/api/me")).json()
    assert me["username"] == "alice"
    assert me["pass"]["kind"] == "long"


async def test_legacy_database_sessions_are_backfilled_to_cli_user(tmp_path):
    path = tmp_path / "operations.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO sessions VALUES ('old-one', '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    store = OperationStore(path)
    await store.open()
    assert await store.owner("old-one") == CLI_USER
    assert [row[0] for row in await store.list_sessions(CLI_USER)] == ["old-one"]
    # Opening an old database also adds the metrics table without touching checkpoints.
    assert await store.stage_metrics("old-one") == []
    assert await store.stage_summary("old-one") == {
        "calls": 0,
        "total_ms": 0,
        "total_tokens": 0,
        "failures": 0,
    }
    assert await store.revision("old-one") == "0.0.0"
    await store.close()


async def test_cli_sessions_belong_to_cli_and_web_users_stay_separate(web):
    client, service, application = web
    options = {
        **OPTIONS_BASE,
        "jd": "", "jds": [], "resume": "", "state_snapshot": "",
        "focus": ["综合"], "num_questions": 1, "preset_questions": None,
    }
    await service.app.start(options, session_id="cli-session")
    assert await service.store.owner("cli-session") == CLI_USER

    await login(client, "alice", service=service)
    assert (await client.get("/api/interviews")).json()["sessions"] == []
    assert (await client.get("/api/interviews/cli-session")).status_code == 404

    cli_view = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )
    async with cli_view:
        await login(cli_view, CLI_USER, service=service)
        listed = (await cli_view.get("/api/interviews")).json()["sessions"]
        assert [item["id"] for item in listed] == ["cli-session"]
        assert (await cli_view.get("/api/interviews/cli-session")).status_code == 200

    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )
    async with bob:
        await login(bob, "bob", service=service)
        assert (await bob.get("/api/interviews")).json()["sessions"] == []


async def test_topic_and_question_count_are_required_and_jds_are_optional(web):
    client, service, application = web
    await login(client, service=service)
    assert (await new_interview(client, topic="")).status_code == 400
    assert (await new_interview(client, num_questions=0)).status_code == 400
    assert (await new_interview(client, num_questions=11)).status_code == 400
    assert (await new_interview(client, jds=["x"] * 6)).status_code == 400

    created = await new_interview(
        client,
        topic="Redis 底层",
        num_questions=2,
        resume="做过缓存治理",
        jds=["岗位 A：后端", "岗位 B：基础架构", "  "],
    )
    assert created.status_code == 202
    session_id = created.json()["id"]
    await settle(client, service, session_id)
    assert await service.store.owner(session_id) == "alice"
    turn = await service.app.inspect(session_id)
    options = turn.state["options"]
    assert options["jds"] == ["岗位 A：后端", "岗位 B：基础架构"]
    assert options["jd"] == "岗位 A：后端\n\n岗位 B：基础架构"
    assert options["focus"] == ["Redis 底层"]
    assert options["num_questions"] == 2
    assert options["resume"] == "做过缓存治理"
    detail = (await client.get(f"/api/interviews/{session_id}")).json()
    assert detail["session"]["jd_count"] == 2
    assert detail["session"]["has_resume"] is True


async def test_detail_is_available_immediately_after_creation(web):
    client, service, _ = web
    await login(client, service=service)
    created = await new_interview(client)
    assert created.status_code == 202
    session_id = created.json()["id"]
    # Ownership must be visible before the background graph task runs.
    response = await client.get(f"/api/interviews/{session_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["busy"] or detail["initializing"] or detail["pending"]
    await settle(client, service, session_id)


async def test_stream_shows_three_roles_and_resume_until_done(web):
    client, service, application = web
    await login(client, service=service)
    created = await new_interview(client, topic="Go 并发", num_questions=1)
    session_id = created.json()["id"]
    detail = await settle(client, service, session_id)
    assert detail["pending"] is not None
    roles = {message["role"] for message in detail["messages"]}
    assert roles == {"system", "interviewer"}

    blank = await client.post(
        f"/api/interviews/{session_id}/answer", json={"answer": "  "}
    )
    assert blank.status_code == 400
    assert blank.json()["detail"]["need_confirm"] is True

    for _ in range(8):
        detail = await settle(client, service, session_id)
        if detail["done"]:
            break
        assert detail["pending"] is not None
        response = await client.post(
            f"/api/interviews/{session_id}/answer",
            json={"answer": "我先回滚数据，再用日志定位受影响范围。"},
        )
        assert response.status_code == 202
    assert detail["done"] is True
    roles = {message["role"] for message in detail["messages"]}
    assert roles == {"system", "interviewer", "candidate"}
    assert any("候选人已提交本轮回答" in m["text"] for m in detail["messages"])
    assert all(m["label"] == "候选人" for m in detail["messages"] if m["role"] == "candidate")
    assert detail["pending"] is None


async def test_web_refuses_continuing_a_session_with_a_mismatched_backend(web):
    client, service, _ = web
    await login(client, service=service)
    session_id = (await new_interview(client)).json()["id"]
    await settle(client, service, session_id)

    service.options_base["demo"] = False
    mismatched = await client.post(
        f"/api/interviews/{session_id}/answer", json={"answer": "继续作答"}
    )
    assert mismatched.status_code == 409
    assert "离线演示" in mismatched.json()["detail"]
    assert (await client.post(f"/api/interviews/{session_id}/resume")).status_code == 409
    assert (await client.get(f"/api/interviews/{session_id}/report")).status_code == 200

    service.options_base["demo"] = True
    matched = await client.post(
        f"/api/interviews/{session_id}/answer", json={"answer": "继续作答"}
    )
    assert matched.status_code == 202
    await settle(client, service, session_id)


async def test_completed_interview_cannot_continue_but_report_is_readable(web):
    client, service, application = web
    await login(client, service=service)
    session_id = (await new_interview(client, topic="系统设计", num_questions=1)).json()["id"]
    for _ in range(8):
        detail = await settle(client, service, session_id)
        if detail["done"]:
            break
        await client.post(
            f"/api/interviews/{session_id}/answer", json={"answer": "不会，跳过本题。"}
        )
    assert detail["done"] is True
    assert (
        await client.post(
            f"/api/interviews/{session_id}/answer", json={"answer": "再答一次"}
        )
    ).status_code == 409
    assert (await client.post(f"/api/interviews/{session_id}/resume")).status_code == 409
    report = await client.get(f"/api/interviews/{session_id}/report")
    assert report.status_code == 200
    assert report.json()["done"] is True
    assert "模拟面试报告" in report.json()["markdown"]


async def test_sessions_are_isolated_between_usernames(web):
    alice, service, application = web
    await login(alice, "alice", service=service)
    session_id = (await new_interview(alice, topic="Go 并发", num_questions=1)).json()["id"]
    await settle(alice, service, session_id)

    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )
    async with bob:
        await login(bob, "bob", service=service)
        assert (await bob.get("/api/interviews")).json()["sessions"] == []
        for path, method in [
            (f"/api/interviews/{session_id}", "get"),
            (f"/api/interviews/{session_id}/report", "get"),
            (f"/api/interviews/{session_id}/answer", "post"),
            (f"/api/interviews/{session_id}/resume", "post"),
        ]:
            call = getattr(bob, method)
            kwargs = {"json": {"answer": "x"}} if method == "post" else {}
            response = await call(path, **kwargs)
            assert response.status_code == 404, (path, response.status_code)


async def test_status_heartbeat_returns_a_change_marker_without_full_detail(web):
    client, service, _ = web
    await login(client, service=service)
    session_id = (await new_interview(client)).json()["id"]
    await settle(client, service, session_id)

    status = (await client.get(f"/api/interviews/{session_id}/status")).json()
    detail = (await client.get(f"/api/interviews/{session_id}")).json()
    assert status["revision"] == detail["revision"]
    assert {"busy", "scoring_pending", "revision", "stage", "stage_ms"} <= set(status)
    assert "messages" not in status and "pending" not in status
    assert status["busy"] is False

    await client.post(
        f"/api/interviews/{session_id}/answer", json={"answer": "先回滚，再定位受影响范围"}
    )
    after = (await client.get(f"/api/interviews/{session_id}/status")).json()
    assert after["revision"] != status["revision"]
    await settle(client, service, session_id)
    await service.app.jobs.wait(session_id)


async def test_stage_timings_are_recorded_for_planning_and_questions(web):
    client, service, _ = web
    await login(client, service=service)
    session_id = (await new_interview(client)).json()["id"]
    await settle(client, service, session_id)
    timings = (await client.get(f"/api/interviews/{session_id}/timings")).json()
    stages = timings["stages"]
    labels = {item["label"] for item in stages}
    assert "生成考点计划" in labels
    assert "生成下一题" in labels
    assert all(item["ms"] >= 0 and item["duration_ms"] >= 0 for item in stages)
    assert all({"tokens", "ok", "started_at", "key", "attempts"} <= set(item) for item in stages)
    assert timings["summary"]["calls"] == len(stages)
    assert timings["summary"]["total_tokens"] == sum(item["tokens"] for item in stages)
    assert {"current", "recent"} <= set(timings["live"])
    detail = (await client.get(f"/api/interviews/{session_id}")).json()
    assert len(detail["metrics"]) == len(stages)
    assert detail["metrics_summary"] == timings["summary"]
    assert (await client.get("/api/interviews/not-mine/timings")).status_code == 404


def test_web_defaults_to_all_interfaces():
    from mockinterview.cli import build_parser

    args = build_parser().parse_args(["web"])
    assert args.host == "0.0.0.0"
    assert args.port == 8000
    assert args.secure_cookie is False


async def test_secure_cookie_flag_marks_cookies_when_enabled(tmp_path):
    service = WebService(
        DemoBackend(), tmp_path, {**OPTIONS_BASE, "secure_cookies": True},
        notify=lambda _: None,
    )
    application = create_app(service)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            token, _ = await make_pass(service)
            response = await client.post(
                "/api/login", json={"username": "alice", "pass_token": token}
            )
            assert response.status_code == 200
            set_cookie = response.headers.get("set-cookie", "")
            assert "Secure" in set_cookie and "mi_pass=" in set_cookie


class CapturingModel:
    def __init__(self):
        self.messages = []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        payload = {"topics": [{"topic": "考点一"}, {"topic": "考点二"}]}
        return SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            usage_metadata={"total_tokens": 1},
        )


async def test_multi_jd_reaches_plan_prompt_without_breaking_legacy_options():
    first = CapturingModel()
    backend = ModelBackend(Config(), questioner=first, evaluator=first)
    await backend.plan({"num_questions": 2, "jds": ["JD 一", "JD 二"], "focus": ["综合"]})
    user_message = first.messages[0][1].content
    assert "【目标 JD 1】\nJD 一" in user_message
    assert "【目标 JD 2】\nJD 二" in user_message

    second = CapturingModel()
    backend = ModelBackend(Config(), questioner=second, evaluator=second)
    await backend.plan({"num_questions": 2, "jd": "单个 JD 文本", "focus": ["综合"]})
    legacy_message = second.messages[0][1].content
    assert "单个 JD 文本" in legacy_message
    assert "【目标 JD 1】" not in legacy_message


async def test_stage_metrics_are_durable_and_survive_a_reopened_store(web):
    client, service, _ = web
    await login(client, service=service)
    session_id = (await new_interview(client)).json()["id"]
    await settle(client, service, session_id)

    saved = await service.store.stage_metrics(session_id)
    assert saved
    assert all(item["tokens"] >= 0 and item["duration_ms"] >= 0 for item in saved)
    assert all(item["label"] and item["started_at"] and item["finished_at"] for item in saved)

    # The rows are in SQLite, not only in the process-local StageTracker.
    with sqlite3.connect(service.data_dir / "operations.sqlite") as conn:
        rows = conn.execute(
            "SELECT stage_key, duration_ms, tokens, ok FROM stage_metrics "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    assert len(rows) == len(saved)
    assert rows[0][0] == saved[0]["key"]

    reopened = OperationStore(service.data_dir / "operations.sqlite")
    await reopened.open()
    try:
        assert await reopened.stage_metrics(session_id) == saved
        assert await reopened.stage_summary(session_id) == await service.store.stage_summary(
            session_id
        )
    finally:
        await reopened.close()

    # A stage write on its own changes the heartbeat marker.
    before = (await client.get(f"/api/interviews/{session_id}/status")).json()["revision"]
    await service.store.add_stage_metric(
        session_id,
        {
            "stage_key": "manual:check",
            "label": "模型调用",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "duration_ms": 1000,
            "tokens": 7,
            "ok": True,
            "error": "",
            "attempts": 1,
        },
    )
    after = (await client.get(f"/api/interviews/{session_id}/status")).json()["revision"]
    assert after != before


async def test_api_requires_a_valid_pass_and_exchange_route_sets_the_cookie(web):
    client, service, application = web
    assert (await client.get("/api/interviews")).status_code == 401
    assert (await client.get("/api/interviews")).json()["detail"] == (
        "需要访问令牌：请通过带 ?pass= 的链接进入，或在登录框填入令牌"
    )
    no_token = await client.post("/api/login", json={"username": "alice"})
    assert no_token.status_code == 401
    bad = await client.post("/api/login", json={"username": "alice", "pass_token": "nope"})
    assert bad.status_code == 401
    assert "重新获取" in bad.json()["detail"]

    token, _ = await make_pass(service)
    fresh = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )
    async with fresh:
        junk = await fresh.get("/pass/junk-token", follow_redirects=False)
        assert junk.status_code == 302
        assert junk.headers["location"] == "/?pass_error=1"
        exchanged = await fresh.get(f"/pass/{token}", follow_redirects=False)
        assert exchanged.status_code == 302
        assert exchanged.headers["location"] == f"/?pass={token}"
        # The friend lands on the login form with the token prefilled; entering
        # only a username creates the user on first login.
        assert (await fresh.post("/api/login", json={"username": "carol", "pass_token": token})).status_code == 200
        me = (await fresh.get("/api/me")).json()
        assert me["username"] == "carol"
        assert me["pass"]["kind"] == "long"
        assert (await fresh.get("/api/interviews")).status_code == 200
        assert (await fresh.post("/api/logout", json={})).status_code == 200
        assert (await fresh.get("/api/interviews")).status_code == 401


async def test_expired_and_revoked_passes_are_rejected(web):
    client, service, _ = web
    expired, _ = await make_pass(service, hours=-1)
    response = await client.post(
        "/api/login", json={"username": "alice", "pass_token": expired}
    )
    assert response.status_code == 401
    assert "无效或已过期" in response.json()["detail"]

    token, pass_id = await make_pass(service)
    await login(client, "alice", token=token)
    assert (await client.get("/api/interviews")).status_code == 200
    assert await service.store.revoke_pass(pass_id) is True
    assert (await client.get("/api/interviews")).status_code == 401
    assert "无效或已过期" in (await client.get("/api/interviews")).json()["detail"]


async def test_temp_pass_quota_blocks_model_work_but_reads_stay_open(web):
    client, service, _ = web
    token, pass_id = await make_pass(service, kind="temp", hours=1, quota=10)
    await login(client, "guest", token=token)
    created = await new_interview(client)
    assert created.status_code == 202
    session_id = created.json()["id"]
    await settle(client, service, session_id)

    # Simulate the real model spend this pass caused (started after the link).
    await service.store.link_pass_session(pass_id, session_id)
    await service.store.add_stage_metric(
        session_id,
        {
            "stage_key": "manual:quota",
            "label": "模型调用",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 500,
            "tokens": 100,
            "ok": True,
            "error": "",
            "attempts": 1,
        },
    )
    assert await service.store.pass_usage(pass_id) >= 100

    blocked = await new_interview(client)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["quota_exhausted"] is True
    answer = await client.post(
        f"/api/interviews/{session_id}/answer", json={"answer": "还想继续作答"}
    )
    assert answer.status_code == 403
    assert answer.json()["detail"]["quota_exhausted"] is True
    assert (await client.post(f"/api/interviews/{session_id}/resume")).status_code == 403

    # Reads, history and reports stay open after exhaustion.
    assert (await client.get(f"/api/interviews/{session_id}")).status_code == 200
    assert (await client.get("/api/interviews")).status_code == 200
    assert (await client.get(f"/api/interviews/{session_id}/report")).status_code == 200

    # Switching usernames under the same pass still works (grouping label only).
    assert (await client.post("/api/login", json={"username": "guest2", "pass_token": token})).status_code == 200
    assert (await client.get("/api/interviews")).json()["sessions"] == []

    # A long pass never hits the quota guard.
    long_token, _ = await make_pass(service, kind="long")
    await login(client, "owner", token=long_token)
    assert (await new_interview(client)).status_code == 202


async def test_temp_pass_clock_starts_on_first_use_not_at_issue_time(web):
    client, service, _ = web
    token, _ = await make_pass(service, kind="temp", hours=1)
    # Simulate a pass issued long ago that was never opened: under the old
    # "expiry fixed at issue" rule this login would be rejected.
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await service.store.conn.execute(
        "UPDATE passes SET created_at=?, expires_at=? WHERE token_hash IN "
        "(SELECT token_hash FROM passes WHERE id = (SELECT MAX(id) FROM passes))",
        (stale, stale),
    )
    await service.store.conn.commit()
    rows = await service.store.list_passes()
    issued_id = rows[-1]["id"]

    # First use: the gate activates the pass and grants a full fresh hour.
    await login(client, "late-guest", token=token)
    me = (await client.get("/api/me")).json()
    assert me["pass"]["kind"] == "temp"
    activated = datetime.fromisoformat(me["pass"]["expires_at"])
    assert activated > datetime.now(timezone.utc) + timedelta(minutes=55)

    stored = await service.store.get_pass(issued_id)
    assert stored["first_used_at"] is not None
    assert stored["expires_at"] == me["pass"]["expires_at"]

    # Subsequent validation does not extend the clock again.
    again = (await client.get("/api/interviews")).status_code == 200
    assert again is True
    stored_after = await service.store.get_pass(issued_id)
    assert stored_after["expires_at"] == stored["expires_at"]

    # Long passes are untouched by activation: expiry stays as issued.
    long_token, _ = await make_pass(service, kind="long", hours=1)
    issued = (await service.store.list_passes())[-1]
    long_expires_before = issued["expires_at"]
    await login(client, "owner", token=long_token)
    assert (await service.store.list_passes())[-1]["expires_at"] == long_expires_before
