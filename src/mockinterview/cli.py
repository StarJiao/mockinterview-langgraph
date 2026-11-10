"""The CLI owns editing. LangGraph receives only explicitly submitted answers."""

import argparse
import asyncio
from datetime import datetime
import os
from pathlib import Path
import re
import shlex
import sys
from uuid import uuid4

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from .config import Config
from .report import render_report
from .state import MAX_PROBES, round_label


class PauseInterview(Exception):
    """An unsubmitted answer must never be confused with a submitted empty answer."""


def colors_enabled(stream=None, environ=None) -> bool:
    stream = sys.stdout if stream is None else stream
    environ = os.environ if environ is None else environ
    return bool(stream.isatty() and "NO_COLOR" not in environ and environ.get("TERM") != "dumb")


class Console:
    def __init__(self, color: bool | None = None):
        self.color = colors_enabled() if color is None else color

    def say(self, role: str, text: str) -> None:
        # Model text is data, not terminal escape commands.
        text = str(text).replace("\x1b", "")
        codes = {"面试官": "36", "系统": "33", "你": "32"}
        line = f"[{role}] {text}"
        if self.color and role in codes:
            line = f"\x1b[{codes[role]}m{line}\x1b[0m"
        print(line, flush=True)

    def system(self, text: str) -> None:
        self.say("系统", text)

    def separator(self, label: str = "") -> None:
        print(f"\n{'=' * 18} {label} {'=' * 18}", flush=True)


def editor_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    @bindings.add("escape", "enter")
    def submit(event):
        event.app.exit(result=event.current_buffer.text)

    @bindings.add("c-c")
    @bindings.add("c-d")
    def pause(event):
        event.app.exit(exception=PauseInterview())

    return bindings


def make_editor(*, input=None, output=None, color: bool = False) -> PromptSession:
    """Separate input/output injection makes real Chinese editing key paths testable."""
    return PromptSession(
        message=[("class:user", "[你] ")],
        multiline=True,
        key_bindings=editor_bindings(),
        prompt_continuation="     ",
        bottom_toolbar="Enter 换行 · Esc 后 Enter 提交 · Ctrl-C / Ctrl-D 暂停（草稿不保存）",
        style=Style.from_dict({"user": "ansigreen" if color else ""}),
        color_depth=ColorDepth.DEPTH_8_BIT if color else ColorDepth.DEPTH_1_BIT,
        input=input,
        output=output,
        enable_history_search=False,
        enable_open_in_editor=False,
    )


async def read_answer(editor: PromptSession, confirm: PromptSession | None = None) -> str:
    draft = ""
    try:
        while True:
            answer = await editor.prompt_async(default=draft)
            if answer.strip():
                return answer
            if confirm is None:
                confirm = PromptSession()
            choice = await confirm.prompt_async(
                "[系统] 确认提交空回答？输入 y 确认，其余返回编辑："
            )
            if choice.strip().lower() == "y":
                return ""
            draft = answer
    except (EOFError, KeyboardInterrupt) as exc:
        raise PauseInterview() from exc


class ScriptAnswers:
    """A deliberate test/replay mode; exhausted input pauses instead of submitting blanks."""

    def __init__(self, path: str | Path):
        content = Path(path).read_text(encoding="utf-8")
        self.answers = (
            iter(re.split(r"(?m)^===ANSWER===[ \t]*\r?$", content)) if content else iter(())
        )

    async def read(self) -> str:
        try:
            # Separator-adjacent newlines are formatting, not answer content.
            return next(self.answers).strip("\r\n")
        except StopIteration as exc:
            raise PauseInterview() from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地模拟面试 · LangGraph + SQLite")

    def common(target, suppress=False):
        def default(value):
            return argparse.SUPPRESS if suppress else value

        target.add_argument(
            "--env", default=default(".env"), help="dotenv 路径；shell 环境变量优先"
        )
        target.add_argument("--data-dir", default=default("data"), help="本地 SQLite 数据目录")
        target.add_argument("--output-dir", default=default("outputs"), help="Markdown 报告目录")
        target.add_argument(
            "--demo", action="store_true", default=default(False), help="离线演示，不调用模型"
        )

    common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)
    new = subparsers.add_parser("new", help="开始新面试")
    common(new, True)
    new.add_argument("--jd", help="岗位描述文件")
    new.add_argument("--resume-file", help="简历文件")
    new.add_argument("--state", help="薄弱点/学习状态文件")
    new.add_argument("--focus", action="append", default=[], help="考查方向；可重复或逗号分隔")
    new.add_argument("--questions", type=int)
    new.add_argument("--preset", help="预定题目 JSON 文件；题数由文件决定")
    new.add_argument("--prompt-version", choices=("original", "improved", "improved_v2"), default=None)
    new.add_argument("--budget", type=int, help="软 token 预算；允许必要收尾和评分")
    new.add_argument("--answers", help="脚本模式：以独立行 ===ANSWER=== 分隔答案，耗尽即暂停")
    resume = subparsers.add_parser("resume", help="继续已有面试")
    common(resume, True)
    resume.add_argument("session_id")
    resume.add_argument("--answers", help="脚本答案文件，耗尽即暂停")
    resume.add_argument("--prompt-version", choices=("original", "improved", "improved_v2"), default=None)
    sessions = subparsers.add_parser("sessions", help="列出本地会话")
    common(sessions, True)
    report = subparsers.add_parser("report", help="从已保存的事实导出报告（不调用模型）")
    common(report, True)
    report.add_argument("session_id")
    score = subparsers.add_parser("score", help="对已结束面试的缺分题目单独补评")
    common(score, True)
    score.add_argument("session_id")
    web = subparsers.add_parser("web", help="启动本机 Web 界面（凭访问令牌进入）")
    common(web, True)
    web.add_argument(
        "--host", default="0.0.0.0", help="监听地址；默认所有网卡，可用 127.0.0.1 收回"
    )
    web.add_argument("--port", type=int, default=8000, help="监听端口")
    web.add_argument(
        "--secure-cookie",
        action="store_true",
        default=False,
        help="HTTPS 站点使用：给 cookie 加 Secure 标记（HTTP 下加了会登录不上）",
    )
    passes = subparsers.add_parser("pass", help="签发/管理 Web 访问令牌")
    common(passes, True)
    pass_sub = passes.add_subparsers(dest="pass_command", required=True)
    pnew = pass_sub.add_parser("new", help="签发令牌")
    common(pnew, True)
    pnew.add_argument("--days", type=int, help="长期令牌有效期（天），不限配额")
    pnew.add_argument("--hours", type=int, help="临时令牌有效期（小时），带 token 配额")
    pnew.add_argument("--quota", type=int, help="临时令牌 token 配额，默认 300000")
    pnew.add_argument("--count", type=int, default=1, help="一次签发数量，默认 1")
    pnew.add_argument("--label", default="", help="备注（给谁用的）")
    pnew.add_argument("--base-url", default="", help="拼接分享链接，例如 http://1.2.3.4")
    plist = pass_sub.add_parser("list", help="列出全部令牌及用量")
    common(plist, True)
    prevoke = pass_sub.add_parser("revoke", help="立即作废一枚令牌")
    common(prevoke, True)
    prevoke.add_argument("pass_id", type=int)
    return parser


def show_plan(console: Console, state: dict) -> None:
    plan = state.get("plan", [])
    if plan:
        console.system(
            "本场考查方向："
            + " → ".join(f"{index}. {item.get('topic', '')}" for index, item in enumerate(plan, 1))
        )


def show_history(console: Console, state: dict) -> set[str]:
    shown_closings = set()
    for index, question in enumerate(state.get("questions", []), 1):
        answered = [
            (i, rd)
            for i, rd in enumerate(question.get("rounds", []))
            if rd.get("answer") is not None
        ]
        if answered:
            console.separator(f"历史 · 第 {index} 题 · {question.get('topic', '')}")
            for round_index, round_ in answered:
                label = round_label(question, round_index, with_limit=True)
                console.say("面试官", f"{label}：{round_['ask']}")
                console.say("你", round_["answer"] or "（已提交空回答）")
        if question.get("closing"):
            console.say("面试官", question["closing"])
            shown_closings.add(question["id"])
    return shown_closings


def save_report(state: dict, session_id: str, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # IDs are validated by the runtime, and still never become path components unchecked.
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    path = directory / f"report-{safe_id}.md"
    path.write_text(render_report({**state, "session_id": session_id}), encoding="utf-8")
    return path.resolve()


def resume_command(args, session_id: str) -> str:
    pieces = [
        "uv",
        "run",
        "mockinterview",
        "resume",
        session_id,
        "--data-dir",
        str(args.data_dir),
        "--output-dir",
        str(args.output_dir),
    ]
    if args.demo:
        pieces.append("--demo")
    elif args.env != ".env":
        pieces.extend(["--env", args.env])
    return shlex.join(pieces)


async def pass_command(args, console: Console) -> int:
    """Issue/list/revoke web access passes; needs no model backend or .env."""
    import secrets

    from datetime import timedelta, timezone

    from .persistence import OperationStore, TEMP_PASS_DEFAULT_QUOTA

    store = OperationStore(Path(args.data_dir) / "operations.sqlite")
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    await store.open()
    try:
        if args.pass_command == "new":
            if bool(args.days) == bool(args.hours):
                raise ValueError("长期令牌用 --days，临时令牌用 --hours，二选一")
            if args.days:
                if args.days <= 0:
                    raise ValueError("--days 必须大于 0")
                kind, delta, quota = "long", timedelta(days=args.days), None
            else:
                if args.hours <= 0:
                    raise ValueError("--hours 必须大于 0")
                kind, delta = "temp", timedelta(hours=args.hours)
                quota = args.quota if args.quota is not None else TEMP_PASS_DEFAULT_QUOTA
                if quota <= 0:
                    raise ValueError("--quota 必须大于 0")
            count = max(1, args.count)
            expires = (datetime.now(timezone.utc) + delta).isoformat()
            ttl_seconds = int(delta.total_seconds()) if kind == "temp" else None
            console.system(
                f"签发 {count} 枚{'长期' if kind == 'long' else '临时'}令牌"
                + (
                    f" · 自首次使用起 {args.hours} 小时内有效"
                    if kind == "temp"
                    else f" · 有效期至 {expires}"
                )
                + (f" · 配额 {quota} token" if quota else " · 不限配额")
            )
            for _ in range(count):
                token = secrets.token_urlsafe(24)
                await store.create_pass(
                    token,
                    kind,
                    expires,
                    label=args.label,
                    quota_tokens=quota,
                    ttl_seconds=ttl_seconds,
                )
                suffix = f"?pass={token}"
                url = f"{args.base_url.rstrip('/')}/{suffix}" if args.base_url else suffix
                print(f"{token}\t{url}", flush=True)
            if kind == "long":
                console.system("长期令牌不限配额，请只给自己使用。")
            else:
                console.system("临时令牌到点自动失效；可随时用 pass revoke 立即作废。")
            return 0
        if args.pass_command == "list":
            rows = await store.list_passes()
            console.system("id\t类型\t用量/配额\t有效期至\t状态\t备注")
            now_iso = datetime.now(timezone.utc).isoformat()
            for row in rows:
                quota = row["quota_tokens"]
                usage = f"{row['used_tokens']}/{quota if quota is not None else '∞'}"
                if row["revoked"]:
                    state = "已作废"
                elif row["kind"] == "temp" and row["first_used_at"] is None:
                    state = "未激活（首次使用起计时）"
                elif row["expires_at"] <= now_iso:
                    state = "已过期"
                else:
                    state = "有效"
                print(
                    f"{row['id']}\t{row['kind']}\t{usage}\t"
                    f"{row['expires_at'][:19].replace('T', ' ')}\t{state}\t{row['label']}"
                )
            return 0
        # revoke
        if not await store.revoke_pass(args.pass_id):
            raise ValueError(f"不存在 id 为 {args.pass_id} 的令牌")
        console.system(f"令牌 {args.pass_id} 已作废；持有者的下一次请求即被拒绝。")
        return 0
    finally:
        await store.close()


async def run(args) -> int:
    # Deferred imports allow --help and editor tests without provider/runtime initialization.
    from .models import DemoBackend, ModelBackend
    from .runtime import InterviewApp

    console = Console()
    session_id = getattr(args, "session_id", None)
    if args.command == "pass":
        return await pass_command(args, console)
    config = Config.load(args.env)
    if args.command == "web":
        from .web import serve

        if not args.demo:
            missing = []
            if not config.questioner_api_key:
                missing.append("DEEPSEEK_API_KEY / QUESTIONER_API_KEY")
            if not config.evaluator_api_key:
                missing.append("DASHSCOPE_API_KEY / EVALUATOR_API_KEY")
            if missing:
                raise ValueError("缺少模型密钥：" + "、".join(missing) + "；可指定 --env 或使用 --demo")
        backend = DemoBackend() if args.demo else ModelBackend(
            config, console.system, prompt_version="original"
        )
        options_base = {
            "token_budget": config.token_budget,
            "demo": args.demo,
            "prompt_version": "original",
            "questioner_model": "demo" if args.demo else config.questioner_model,
            "evaluator_model": "demo" if args.demo else config.evaluator_model,
            "secure_cookies": args.secure_cookie,
        }
        shown_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        console.system(
            f"Web 界面：http://{shown_host}:{args.port}（监听 {args.host}:{args.port}）；"
            "所有 API 凭访问令牌进入（pass new 签发），用户名只用于区分数据。"
        )
        await serve(
            backend, Path(args.data_dir), options_base, args.host, args.port, notify=console.system
        )
        return 0
    read_only = args.command in {"sessions", "report"}
    if args.command in {"resume", "score"}:
        # Read first without constructing a provider client. A saved demo must
        # remain offline even when the user omits --demo on the resume command.
        async with InterviewApp(Path(args.data_dir), DemoBackend()) as probe:
            saved = await probe.inspect(session_id)
        if args.command == "score" and not saved.done:
            raise ValueError("面试尚未结束，请先使用 resume 完成答题")
        is_demo = bool(saved.state.get("options", {}).get("demo", False))
        if args.demo and not is_demo:
            raise ValueError("真实面试不能用 --demo 恢复，避免混入演示评分")
        args.demo = is_demo
        saved_version = saved.state.get("options", {}).get("prompt_version", "original")
        if getattr(args, "prompt_version", None) is not None and args.prompt_version != saved_version:
            raise ValueError("恢复时提示词版本必须与会话创建时一致；请新建会话做版本对比")
        args.prompt_version = saved_version
        read_only = saved.done if args.command == "resume" else not any(
            q["status"] == "done" and q["id"] not in saved.state["evaluations"]
            for q in saved.state["questions"]
        )
    if args.command == "new":
        args.prompt_version = args.prompt_version or "original"
    if not args.demo and not read_only:
        missing = []
        if not config.questioner_api_key:
            missing.append("DEEPSEEK_API_KEY / QUESTIONER_API_KEY")
        if not config.evaluator_api_key:
            missing.append("DASHSCOPE_API_KEY / EVALUATOR_API_KEY")
        if missing:
            raise ValueError("缺少模型密钥：" + "、".join(missing) + "；可指定 --env 或使用 --demo")
    backend = DemoBackend() if args.demo or read_only else ModelBackend(
        config, console.system, prompt_version=args.prompt_version or "original"
    )
    answers = ScriptAnswers(args.answers) if getattr(args, "answers", None) else None
    editor = None
    if args.command in {"new", "resume"} and not read_only and answers is None and not sys.stdin.isatty():
        raise ValueError("交互编辑需要终端；自动测试请使用 --answers 文件（EOF 不会当作空回答）")

    try:
        with patch_stdout(raw=True):
            async with InterviewApp(Path(args.data_dir), backend, notify=console.system) as app:
                if args.command == "sessions":
                    for session in await app.sessions():
                        print(
                            f"{session['id']}\t{session['status']}\t{session.get('updated_at', '')}"
                        )
                    return 0
                if args.command == "report":
                    turn = await app.inspect(session_id)
                    path = save_report(turn.state, session_id, args.output_dir)
                    console.system(f"报告已写入：{path}")
                    return 0
                if args.command == "score":
                    turn = await app.rescore(session_id) if not read_only else await app.inspect(session_id)
                    path = save_report(turn.state, session_id, args.output_dir)
                    missing = sum(
                        q["status"] == "done" and q["id"] not in turn.state["evaluations"]
                        for q in turn.state["questions"]
                    )
                    console.system(f"补评完成；仍有 {missing} 题未评分。报告：{path}")
                    return 0 if missing == 0 else 1
                if args.command == "new":
                    if (args.questions is not None and args.questions <= 0) or (args.budget is not None and args.budget <= 0):
                        raise ValueError("题目数和 token 预算必须大于 0")

                    def read(path):
                        return Path(path).read_text(encoding="utf-8") if path else ""

                    from .preset import load_preset

                    preset = load_preset(args.preset) if args.preset else None
                    if preset and args.questions is not None:
                        raise ValueError("使用 --preset 时不要同时指定 --questions；题数由文件决定")
                    options = {
                        "jd": read(args.jd),
                        "resume": read(args.resume_file),
                        "state_snapshot": read(args.state),
                        "focus": [
                            item.strip()
                            for value in args.focus
                            for item in value.split(",")
                            if item.strip()
                        ]
                        or ["综合"],
                        "num_questions": len(preset) if preset else (args.questions or 3),
                        "token_budget": args.budget or config.token_budget,
                        "demo": args.demo,
                        "prompt_version": args.prompt_version,
                        "preset_questions": preset,
                        "questioner_model": "demo" if args.demo else config.questioner_model,
                        "evaluator_model": "demo" if args.demo else config.evaluator_model,
                    }
                    session_id = f"mock-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
                    console.system(f"会话：{session_id}")
                    turn = await app.start(options, session_id=session_id)
                    shown_closings = set()
                else:
                    turn = await app.resume(session_id)
                    console.system(f"恢复会话：{session_id}；以下仅重放问答，不重放旧系统消息。")
                    shown_closings = show_history(console, turn.state)
                show_plan(console, turn.state)
                last_question = None
                while not turn.done:
                    for question in turn.state.get("questions", []):
                        if question.get("closing") and question["id"] not in shown_closings:
                            console.say("面试官", question["closing"])
                            shown_closings.add(question["id"])
                    pending = turn.pending
                    if not pending:
                        raise RuntimeError("流程未完成且没有等待回答的中断；请恢复会话重试")
                    if pending["question_id"] != last_question:
                        console.separator(
                            f"第 {pending['index'] + 1}/{pending['total']} 题 · {pending['topic']}"
                        )
                        last_question = pending["question_id"]
                    label = (
                        "题意澄清"
                        if pending.get("kind") == "clarification"
                        else "主问"
                        if pending["hop"] == 0
                        else f"追问 {pending['hop']}/{MAX_PROBES}"
                    )
                    console.say("面试官", f"{label}：{pending['ask']}")
                    if answers:
                        answer = await answers.read()
                        console.say("你", answer or "（脚本已明确提交空回答）")
                    else:
                        if editor is None:
                            editor = make_editor(color=console.color)
                        answer = await read_answer(editor)
                    turn = await app.resume(session_id, answer=answer)
                for question in turn.state.get("questions", []):
                    if question.get("closing") and question["id"] not in shown_closings:
                        console.say("面试官", question["closing"])
                path = save_report(turn.state, session_id, args.output_dir)
                tokens = sum(turn.state.get("usage", {}).values())
                console.system(
                    f"面试完成；{len(turn.state.get('questions', []))} 题，token {tokens}。"
                )
                console.system(f"报告：{path}")
                missing = sum(
                    q["status"] == "done" and q["id"] not in turn.state["evaluations"]
                    for q in turn.state["questions"]
                )
                if missing:
                    console.system(f"有 {missing} 题评分失败；可运行 uv run mockinterview score {session_id} 补评。")
                return 0
    except (PauseInterview, KeyboardInterrupt, EOFError):
        console.system("已暂停。已提交内容可恢复，尚未提交的编辑草稿未保存。")
        if session_id:
            console.system(f"继续：{resume_command(args, session_id)}")
        return 0
    except Exception:
        if session_id:
            console.system(f"本次执行未完成；已落盘进度可重试：{resume_command(args, session_id)}")
        raise


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[系统] 已中断；可用 sessions 查找会话后 resume。", file=sys.stderr)
        code = 130
    except Exception as exc:
        # Provider errors may embed request details. Never echo arbitrary exception bodies/keys.
        if isinstance(exc, (ValueError, FileNotFoundError, PermissionError)):
            message = str(exc)
        else:
            message = f"{type(exc).__name__}；请检查网络、模型配置或已保存的会话状态"
        print(f"[系统] 执行失败：{message}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
