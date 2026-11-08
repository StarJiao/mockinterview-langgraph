import asyncio
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mockinterview.cli import (
    Console,
    PauseInterview,
    ScriptAnswers,
    build_parser,
    colors_enabled,
    make_editor,
    read_answer,
    show_history,
    show_plan,
)
from mockinterview.config import Config
from mockinterview.report import render_report


async def edit(keys):
    with create_pipe_input() as pipe:
        editor = make_editor(input=pipe, output=DummyOutput())
        pipe.send_text(keys)
        return await asyncio.wait_for(editor.prompt_async(), 2)


async def test_chinese_deletes_one_character_not_one_byte():
    assert await edit("中文\x7f\x7f终端\x1b\r") == "终端"


async def test_multiline_can_move_back_and_edit_previous_line():
    keys = "甲乙\r丙丁\x1b[A\x05\x7f正\x1b[B\x05\x1b\r"
    assert await edit(keys) == "甲正\n丙丁"


async def test_pasted_multiline_is_not_submitted_early():
    keys = "\x1b[200~粘贴第一行\n粘贴第二行\x1b[201~\x1b\r"
    assert await edit(keys) == "粘贴第一行\n粘贴第二行"


async def test_enter_only_inserts_newline():
    with create_pipe_input() as pipe:
        editor = make_editor(input=pipe, output=DummyOutput())
        task = asyncio.create_task(editor.prompt_async())
        pipe.send_text("中文\r")
        await asyncio.sleep(0.05)
        assert not task.done()
        pipe.send_text("后续\x1b\r")
        assert await asyncio.wait_for(task, 2) == "中文\n后续"


@pytest.mark.parametrize("keys", ["草稿\x03", "草稿\x04", "\x04"])
async def test_ctrl_c_and_ctrl_d_pause_even_with_draft(keys):
    with pytest.raises(PauseInterview):
        await edit(keys)


async def test_closed_input_pauses_instead_of_submitting_empty():
    with create_pipe_input() as pipe:
        editor = make_editor(input=pipe, output=DummyOutput())
        task = asyncio.create_task(read_answer(editor))
        await asyncio.sleep(0.02)
        pipe.close()
        with pytest.raises(PauseInterview):
            await asyncio.wait_for(task, 2)


class Prompts:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.defaults = []

    async def prompt_async(self, *args, **kwargs):
        self.defaults.append(kwargs.get("default"))
        return next(self.answers)


async def test_blank_requires_explicit_confirmation():
    editor = Prompts(" \n ", "修改后的回答")
    confirm = Prompts("n")
    assert await read_answer(editor, confirm) == "修改后的回答"
    assert editor.defaults == ["", " \n "]
    assert await read_answer(Prompts(""), Prompts("y")) == ""


async def test_script_answers_preserve_multiline_and_pause_at_eof(tmp_path):
    path = tmp_path / "answers.txt"
    path.write_text("第一行\n第二行\n===ANSWER===\n\n===ANSWER===\n最后", encoding="utf-8")
    reader = ScriptAnswers(path)
    assert await reader.read() == "第一行\n第二行"
    assert await reader.read() == ""
    assert await reader.read() == "最后"
    with pytest.raises(PauseInterview):
        await reader.read()
    path.write_text("", encoding="utf-8")
    with pytest.raises(PauseInterview):
        await ScriptAnswers(path).read()


class TTY(StringIO):
    def isatty(self):
        return True


def test_colors_obey_tty_no_color_and_dumb_term():
    assert colors_enabled(TTY(), {})
    assert not colors_enabled(StringIO(), {})
    assert not colors_enabled(TTY(), {"NO_COLOR": ""})
    assert not colors_enabled(TTY(), {"TERM": "dumb"})


def test_globals_can_be_before_or_after_subcommand():
    parser = build_parser()
    assert parser.parse_args(["--demo", "new"]).demo
    assert parser.parse_args(["new", "--demo"]).demo
    assert parser.parse_args(["--data-dir", "x", "resume", "abc"]).data_dir == "x"
    assert parser.parse_args(["resume", "abc", "--data-dir", "y"]).data_dir == "y"


def test_env_can_reuse_provider_names_without_leaking_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "DEEPSEEK_API_KEY=secret-one\nDASHSCOPE_API_KEY=secret-two\n"
        "QUESTIONER_MODEL=old\nSESSION_TOKEN_BUDGET=12345\n",
        encoding="utf-8",
    )
    config = Config.load(path, {"QUESTIONER_MODEL": "new"})
    assert config.questioner_model == "new"
    assert config.questioner_api_key == "secret-one"
    assert config.evaluator_api_key == "secret-two"
    assert config.token_budget == 12345
    assert "secret" not in repr(config)


def test_replay_excludes_unanswered_round_and_system_messages(capsys):
    state = {
        "plan": [{"topic": "缓存", "evidence": "不应提前展示", "reference_points": ["秘密锚点"]}],
        "questions": [
            {
                "id": "q1",
                "topic": "缓存",
                "rounds": [{"ask": "主问题", "answer": ""}, {"ask": "待回答追问", "answer": None}],
                "closing": "",
            }
        ],
    }
    console = Console(color=False)
    show_plan(console, state)
    show_history(console, state)
    text = capsys.readouterr().out
    assert "主问题" in text and "已提交空回答" in text
    assert "待回答追问" not in text and "秘密锚点" not in text and "不应提前展示" not in text


def test_report_distinguishes_empty_from_unsubmitted_and_matches_scores_by_id():
    state = {
        "session_id": "sample",
        "status": "done",
        "usage": {"plan": 10, "q1": 20},
        "options": {"focus": ["Go"], "token_budget": 1000},
        "questions": [
            {
                "id": "q1",
                "status": "done",
                "topic": "缓存",
                "rounds": [{"ask": "问题一", "answer": ""}],
                "closing": "收尾",
            },
            {
                "id": "q2",
                "status": "running",
                "topic": "并发",
                "rounds": [{"ask": "问题二", "answer": None}],
            },
        ],
        "evaluations": {"q1": {"score": "12/20", "judge_model": "fake", "judge_text": "证据"}},
    }
    report = render_report(state)
    assert "30 / 1000" in report
    assert "已提交空回答" in report and "未提交回答" in report
    assert "12/20" in report and "尚未评分" in report
    assert report.index("12/20") < report.index("并发")


def test_history_and_report_label_clarification_without_incrementing_probes(capsys):
    state = {
        "questions": [
            {
                "id": "q1",
                "topic": "迁移",
                "rounds": [
                    {"kind": "main", "ask": "主问", "answer": "秒级还是很久？"},
                    {"kind": "clarification", "ask": "补充时间假设", "answer": "先回滚"},
                    {"kind": "probe", "ask": "如何防复发？", "answer": "协调写入归属"},
                ],
            }
        ],
    }
    show_history(Console(color=False), state)
    output = capsys.readouterr().out
    assert "题意澄清：补充时间假设" in output
    assert "追问 1/3：如何防复发" in output
    assert "追问 2" not in output
    report = render_report(state)
    assert "面试官 · 题意澄清" in report
    assert "面试官 · 追问 1" in report
    assert "面试官 · 追问 2" not in report


def test_report_displays_unassessed_dimensions_without_fabricated_total():
    import json

    dimensions = [
        {
            "name": "迁移与场景应用",
            "score": None,
            "evidence": ["原文：无", "评分理由：没有问迁移场景", "改进建议：可继续考察"],
            "assessments": [
                {
                    "kind": "not_asked",
                    "round_index": 1,
                    "candidate_quote": "",
                    "explanation": "没有提问机会",
                }
            ],
        }
    ]
    state = {
        "questions": [{"id": "q1", "rounds": [{"ask": "原因？", "answer": "集中过期"}]}],
        "evaluations": {
            "q1": {
                "score": None,
                "assessed_dimensions": 3,
                "judge_text": json.dumps({"dimensions": dimensions}, ensure_ascii=False),
            }
        },
    }
    report = render_report(state)
    assert "未计算总分（已评估 3/4 个维度" in report
    assert "迁移与场景应用：未评估（证据不足）" in report
    assert "依据分类：未被问到 · 回合 1" in report
    assert "None" not in report and "/20" not in report and "None/5" not in report


async def test_demo_resume_without_flag_stays_offline(tmp_path, monkeypatch):
    from mockinterview.cli import run
    from mockinterview.models import DemoBackend
    from mockinterview.runtime import InterviewApp

    async with InterviewApp(tmp_path / "data", DemoBackend()) as app:
        await app.start({"num_questions": 1, "demo": True}, "offline-resume")

    def forbidden(*args, **kwargs):
        raise AssertionError("演示恢复不应创建真实模型客户端")

    monkeypatch.setattr("mockinterview.models.ModelBackend", forbidden)
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "resume",
            "offline-resume",
            "--data-dir",
            str(tmp_path / "data"),
            "--answers",
            str(empty),
            "--env",
            str(tmp_path / "missing.env"),
        ]
    )
    assert await run(args) == 0
    assert args.demo is True


async def test_omitted_jd_and_resume_are_empty_in_saved_session(tmp_path):
    from mockinterview.cli import run
    from mockinterview.models import DemoBackend
    from mockinterview.runtime import InterviewApp

    answer_file = tmp_path / "empty.txt"
    answer_file.write_text("", encoding="utf-8")
    args = build_parser().parse_args([
        "new", "--demo", "--questions", "1", "--answers", str(answer_file),
        "--data-dir", str(tmp_path / "data"),
    ])
    assert args.jd is None and args.resume_file is None
    assert await run(args) == 0
    async with InterviewApp(tmp_path / "data", DemoBackend()) as app:
        sessions = await app.sessions()
        turn = await app.inspect(sessions[0]["id"])
        assert turn.state["options"]["jd"] == ""
        assert turn.state["options"]["resume"] == ""
        assert turn.state["options"]["focus"] == ["综合"]


def test_new_without_focus_has_no_personal_default():
    args = build_parser().parse_args(["new", "--jd", "job.txt", "--resume-file", "resume.md"])
    assert args.focus == []


async def test_demo_cannot_mix_into_real_session(tmp_path):
    from mockinterview.cli import run
    from mockinterview.models import DemoBackend
    from mockinterview.runtime import InterviewApp

    async with InterviewApp(tmp_path, DemoBackend()) as app:
        await app.start({"num_questions": 1, "demo": False}, "real-mode")
    args = build_parser().parse_args(
        [
            "resume",
            "real-mode",
            "--demo",
            "--data-dir",
            str(tmp_path),
        ]
    )
    with pytest.raises(ValueError, match="真实面试不能"):
        await run(args)
