import json

import pytest

from mockinterview.preset import load_preset
from mockinterview.report import render_report
from mockinterview.runtime import InterviewApp
from test_runtime import FakeBackend, options


def test_preset_rejects_missing_middle_answer(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"questions": [{
        "topic": "测试", "difficulty": 2, "reference_points": ["依据"],
        "rounds": [
            {"kind": "main", "ask": "主问"},
            {"kind": "probe", "ask": "追问", "answer": "回答"},
        ],
    }]}), encoding="utf-8")
    with pytest.raises(ValueError, match="只有最后一轮"):
        load_preset(path)


def test_both_comparison_fixtures_have_valid_fixed_history():
    complete = load_preset("inputs/preset-example.json")
    followup = load_preset("inputs/preset-followup-example.json")
    assert len(complete) == len(followup) == 1
    assert len(complete[0]["rounds"]) == 3
    assert len(followup[0]["rounds"]) == 2
    assert all("answer" in rd for fixture in (complete, followup) for rd in fixture[0]["rounds"])


async def test_fixed_history_starts_at_last_answer_and_keeps_full_context(tmp_path):
    path = "inputs/preset-example.json"
    preset = load_preset(path)
    backend = FakeBackend(coverage_score=100)
    opts = {**options(), "preset_questions": preset, "prompt_version": "improved"}
    async with InterviewApp(tmp_path, backend) as app:
        done = await app.start(opts, "fixed-history")
        assert done.done
        assert backend.calls["plan"] == backend.calls["question"] == 0
        assert backend.calls["coverage"] == 1
        assert len(backend.coverage_inputs[0]["rounds"]) == 3
        assert [r["answer"] for r in done.state["questions"][0]["rounds"]] == [
            r["answer"] for r in preset[0]["rounds"]
        ]


async def test_unanswered_last_fixed_round_interrupts_and_recovers(tmp_path):
    preset = load_preset("inputs/preset-example.json")
    preset[0]["rounds"][-1].pop("answer")
    backend = FakeBackend(coverage_score=100)
    opts = {**options(), "preset_questions": preset}
    async with InterviewApp(tmp_path, backend) as app:
        turn = await app.start(opts, "fixed-pending")
        assert turn.pending["ask"] == preset[0]["rounds"][-1]["ask"]
        assert backend.calls["coverage"] == 0
    async with InterviewApp(tmp_path, backend) as app:
        done = await app.resume("fixed-pending", "用外部存储保留原文和回查 ID")
        assert done.done
        assert backend.calls["coverage"] == 1
        assert backend.coverage_inputs[0]["rounds"][0]["answer"] == preset[0]["rounds"][0]["answer"]


def test_report_shows_why_fixed_history_stopped():
    report = render_report({
        "session_id": "comparison", "options": {"prompt_version": "improved"},
        "status": "done", "questions": [{
            "id": "q1", "topic": "上下文", "difficulty": 2, "status": "done",
            "rounds": [{"kind": "main", "ask": "怎么裁剪？", "answer": "按任务取舍",
                        "assessment": {"score": 94, "sufficient": True,
                                       "covered": ["按任务取舍"], "missed": [],
                                       "next_target": "无需追问"},
                        "decision": {"reason": "本题已有足够评价证据"}}],
        }],
    })
    assert "快评覆盖分：94/100 · 证据充分：是" in report
    assert "已确认：按任务取舍" in report
    assert "尚缺：无" in report
    assert "下一轮目标：无需追问" in report
