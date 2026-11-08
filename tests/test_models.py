import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from mockinterview.models import (
    DemoBackend,
    ModelBackend,
    ModelOutputError,
    question_style_issues,
    rounds_digest,
)
from mockinterview.schemas import DIMENSIONS


class FakeModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if not self.outputs:
            raise AssertionError("unexpected model call")
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        return AIMessage(
            content=output,
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )


async def test_prompt_versions_keep_original_and_change_only_selected_instructions():
    from mockinterview import prompts, prompt_versions

    assert prompt_versions.COVERAGE["original"] == prompts.COVERAGE
    assert prompt_versions.PROBE["original"] == prompts.PROBE
    assert prompt_versions.COVERAGE["improved_v2"].startswith(prompt_versions.COVERAGE["improved"])
    assert "准确回查这次调用" in prompt_versions.COVERAGE["improved_v2"]
    question = {
        "text": "历史太长怎么办？", "reference_points": ["根据任务选择上下文"],
        "rounds": [{"kind": "main", "ask": "历史太长怎么办？", "answer": "摘要并外置原文"}],
    }
    reply = {
        "answer_kind": "answer", "reason": "已有方案", "score": 80,
        "covered": ["摘要与外置"], "missed": [], "sufficient": True,
        "has_new_evidence": True, "next_target": "无需追问",
    }
    systems = []
    for version in ("original", "improved"):
        model = FakeModel([reply, "具体说明回查方式？"])
        adapter = ModelBackend(
            SimpleNamespace(evaluator_model="fake-judge"),
            questioner=model, evaluator=FakeModel([]), prompt_version=version,
        )
        await adapter.coverage(question)
        await adapter.probe(question, "回查方式", 1)
        systems.append([call[0].content for call in model.calls])
    assert systems[0] == [prompts.COVERAGE, prompts.PROBE]
    assert systems[1][0] != systems[0][0]
    assert systems[1][1] != systems[0][1]


def backend(questioner=(), evaluator=(), notify=lambda msg: None):
    return ModelBackend(
        SimpleNamespace(evaluator_model="fake-judge"),
        notify,
        questioner=FakeModel(questioner),
        evaluator=FakeModel(evaluator),
    )


def topic(**overrides):
    return (
        dict(
            topic="Go 并发",
            direction="Go",
            difficulty=4,
            points=["取消等待"],
            source="resume",
            weak=True,
            evidence="project",
        )
        | overrides
    )


def draft(**overrides):
    return (
        dict(
            topic="Go 并发",
            difficulty=4,
            text="下游已经超时，你会先取消哪个请求？",
            reference_points=["3分：说明超时", "5分：解释传播"],
            probe_angles=["取消传播"],
        )
        | overrides
    )


def question():
    return draft() | {
        "rounds": [
            {"ask": "主问", "answer": "主答独有证据"},
            {"ask": "第一次追问", "answer": "追问独有证据"},
            {"ask": "第二次追问", "answer": ""},
            {"ask": "尚未回答的问题", "answer": None},
        ]
    }


def coverage(**overrides):
    return {
        "answer_kind": "answer",
        "reason": "已说明部分机制，需继续确认",
        "score": 60,
        "covered": [],
        "missed": ["取消传播"],
        "sufficient": False,
        "has_new_evidence": True,
        "next_target": "已确认取消请求；尚缺下游收到通知的机制证据",
        **overrides,
    }


def dimensions():
    return [
        {
            "name": name,
            "score": score,
            "assessments": [
                {
                    "kind": "correct",
                    "round_index": 1,
                    "candidate_quote": "主答独有证据",
                    "explanation": "与题意对应",
                }
            ],
            "evidence": ["原文：主答独有证据", "评分理由：与题意对应", "改进建议：可进一步考察"],
        }
        for name, score in zip(DIMENSIONS, [1, 3, 5, 3])
    ]


async def test_plan_exact_count_one_retry_and_evidence_clamp():
    notices = []
    model = backend(
        [{"topics": [topic(), topic()]}, {"topics": [topic(evidence="unknown")]}],
        notify=notices.append,
    )
    result = await model.plan(
        {
            "num_questions": 1,
            "focus": ["Go"],
            "jd": "岗位",
            "resume": "经历",
            "state_snapshot": "状态",
        }
    )
    assert result.tokens == 20
    assert len(result.data) == 1
    assert result.data[0]["evidence"] == "theoretical"
    assert result.data[0]["difficulty"] == 2
    assert result.data[0]["weak"] is False
    assert "状态" in model.questioner.calls[0][1].content
    assert any("纠错" in notice for notice in notices)


async def test_plan_without_personal_inputs_cannot_invent_project_evidence():
    model = backend(questioner=[{"topics": [topic()]}])
    result = await model.plan({
        "num_questions": 1, "focus": ["通用后端"], "jd": "", "resume": "",
        "state_snapshot": "",
    })
    assert result.data[0]["source"] == "focus"
    assert result.data[0]["evidence"] == "theoretical"
    assert result.data[0]["weak"] is False
    assert result.data[0]["difficulty"] <= 2
    assert "不得推测候选人的项目经历" in model.questioner.calls[0][1].content


async def test_plan_bad_count_after_retry_is_error():
    model = backend([{"topics": []}, {"topics": []}])
    with pytest.raises(ModelOutputError, match="已纠错重试一次") as error:
        await model.plan({"num_questions": 1})
    assert error.value.tokens == 20
    assert len(model.questioner.calls) == 2


async def test_json_correction_is_bounded_and_usage_accumulated():
    model = backend(["not JSON", "```json\n" + json.dumps({"topics": [topic()]}) + "\n```"])
    result = await model.plan({"num_questions": 1})
    assert result.tokens == 20
    assert "schema" in model.questioner.calls[1][-1].content
    failing = backend(["bad", "still bad"])
    with pytest.raises(ModelOutputError, match="已纠错一次"):
        await failing.plan({"num_questions": 1})
    assert len(failing.questioner.calls) == 2


async def test_failed_repair_keeps_known_usage():
    failing = backend(["bad", RuntimeError("network")])
    with pytest.raises(ModelOutputError) as err:
        await failing.plan({"num_questions": 1})
    assert err.value.tokens == 10
    failing = backend([{"topics": []}, "bad", "still bad"])
    with pytest.raises(ModelOutputError) as err:
        await failing.plan({"num_questions": 1})
    assert err.value.tokens == 30


async def test_question_topic_correction_and_theoretical_clamp():
    model = backend([draft(topic="别的主题"), draft()])
    result = await model.question(topic(evidence="theoretical"), 1, 3)
    assert result.data["topic"] == "Go 并发"
    assert result.data["difficulty"] == 2
    assert result.tokens == 20
    assert "第 1/3 题" in model.questioner.calls[0][1].content
    failing = backend([draft(topic="别的主题"), draft(topic="仍然跑偏")])
    with pytest.raises(ModelOutputError, match="偏离计划"):
        await failing.question(topic(), 1, 3)


async def test_question_style_correction_and_bounded_fallback():
    bad = draft(text="请详述超时控制。")
    model = backend([bad, draft()])
    result = await model.question(topic(), 1, 1)
    assert result.data["text"] == draft()["text"]
    assert result.tokens == 20
    notices = []
    failing = backend([bad, RuntimeError("offline")], notify=notices.append)
    result = await failing.question(topic(), 1, 1)
    assert result.data["text"] == bad["text"]
    assert any("沿用" in notice for notice in notices)
    with pytest.raises(ModelOutputError, match="评价锚点"):
        await backend([draft(reference_points=[])]).question(topic(), 1, 1)


def test_style_checks_keep_go_rules():
    assert not question_style_issues("锁等待变长，你怎么确认它是否进入了饥饿模式？")
    assert question_style_issues("为什么超时，以及如何取消？")
    assert question_style_issues("请从取消和传播方面说明方案")
    assert question_style_issues("字" * 201)


def test_digest_full_rounds_keeps_empty_distinct_from_missing():
    source = question()
    before = deepcopy(source)
    digest = rounds_digest(source)
    assert "主答独有证据" in digest
    assert "追问独有证据" in digest
    assert "（已提交空回答）" in digest
    assert "尚未回答的问题" not in digest
    assert source == before


async def test_coverage_and_probe_use_entire_question_history():
    model = backend(
        [
            coverage(score=120),
            "「你刚才说取消，那下游如何收到通知？」",
        ]
    )
    result = await model.coverage(question())
    assert result.data["score"] == 100
    probe = await model.probe(question(), "目标：取消传播", 2)
    assert probe.data == "你刚才说取消，那下游如何收到通知？"
    for messages in model.questioner.calls:
        assert "主答独有证据" in messages[-1].content
        assert "追问独有证据" in messages[-1].content
    assert "目标：取消传播" in model.questioner.calls[1][-1].content


async def test_evaluation_normalizes_dimensions_keeps_full_evidence():
    dims = dimensions()
    for i, item in enumerate(dims):
        item["name"] = f"{i + 1}. {item['name']} "
    model = backend(evaluator=[{"dimensions": dims}])
    result = await model.evaluate(question())
    assert result.data["score"] == "12/20"
    assert result.data["judge_model"] == "fake-judge"
    assert len(result.data["dimensions"]) == 4
    assert [dim["score"] for dim in result.data["dimensions"]] == [1, 3, 5, 3]
    assert "追问独有证据" in model.evaluator.calls[0][-1].content
    assert "未被问到" in model.evaluator.calls[0][0].content
    with pytest.raises(ModelOutputError, match="维度不全"):
        await backend(evaluator=[{"dimensions": dims[:2]}] * 2).evaluate(question())


@pytest.mark.parametrize("unassessed_count", [1, 4])
async def test_evaluation_unasked_dimension_is_unscored_without_total(unassessed_count):
    dims = dimensions()
    for dim in dims[-unassessed_count:]:
        dim.update(
            score=None,
            assessments=[
                {
                    "kind": "not_asked",
                    "round_index": 1,
                    "candidate_quote": "",
                    "explanation": "没有提问该维度，不推断候选人不会",
                }
            ],
        )
    result = await backend(evaluator=[{"dimensions": dims}]).evaluate(question())
    assert result.data["score"] is None
    assert result.data["assessed_dimensions"] == 4 - unassessed_count
    assert result.data["dimensions"][-1]["score"] is None


async def test_numeric_score_without_observed_evidence_is_repaired():
    bad = dimensions()
    bad[-1]["assessments"][0].update(kind="not_asked", candidate_quote="")
    corrected = deepcopy(bad)
    corrected[-1]["score"] = None
    model = backend(evaluator=[{"dimensions": bad}, {"dimensions": corrected}])
    result = await model.evaluate(question())
    assert result.tokens == 20
    assert result.data["score"] is None
    assert "score 必须为 null" in model.evaluator.calls[1][-1].content


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_quote": "捏造的回答"},
        {"candidate_quote": "主问"},  # Interviewer's words are not the candidate's quote.
        {"round_index": 99},
        {"round_index": 4},  # Unanswered round.
        {"candidate_quote": ""},
    ],
)
async def test_evidence_must_reference_a_real_submitted_answer(change):
    dims = dimensions()
    dims[0]["assessments"][0].update(change)
    model = backend(evaluator=[{"dimensions": dims}] * 2)
    with pytest.raises(ModelOutputError) as err:
        await model.evaluate(question())
    assert err.value.tokens == 20
    assert len(model.evaluator.calls) == 2


@pytest.mark.parametrize("score", [0, 6])
async def test_invalid_score_is_not_silently_clamped(score):
    dims = dimensions()
    dims[0]["score"] = score
    with pytest.raises(ModelOutputError):
        await backend(evaluator=[{"dimensions": dims}] * 2).evaluate(question())


async def test_missing_intent_cannot_silently_fall_back_to_old_coverage():
    old = {"score": 10, "missed": ["防复发"], "worth_digging": False}
    model = backend([old, coverage(score=25)])
    result = await model.coverage(question())
    assert result.tokens == 20
    assert result.data["answer_kind"] == "answer"


async def test_clarification_uses_dedicated_prompt_and_keeps_round_context():
    source = question()
    source["rounds"] = [
        {
            "kind": "main",
            "ask": "切流后写入还在持续，为什么？",
            "answer": "秒级还是很久？",
            "decision": {"action": "CLARIFY"},
        },
    ]
    model = backend(["补充一个假设：持续十分钟。按这个条件，你会先排查什么？"])
    await model.probe(source, "需交代持续时间", 0)
    system, user = model.questioner.calls[0]
    assert "这一轮不是技术追问" in system.content
    assert "不编造" in system.content
    assert "秒级还是很久" in user.content
    assert "第 0" not in user.content
    assert "40 字" not in system.content


async def test_hypothetical_answer_keeps_question_and_prior_design_in_judge_input():
    source = question()
    source["rounds"] = [
        {"ask": "两条匹配订单怎么处理？", "answer": "中断，让用户选择"},
        {"kind": "probe", "ask": "这里假设不做中断，会在哪一步选错？", "answer": "第二步"},
    ]
    dims = dimensions()
    for item in dims:
        item["assessments"][0]["candidate_quote"] = "中断，让用户选择"
    model = backend(evaluator=[{"dimensions": dims}])
    await model.evaluate(source)
    system, user = model.evaluator.calls[0]
    assert "[回合 2 · 追问 1]" in user.content
    assert "面试官：这里假设不做中断" in user.content
    assert "候选人：第二步" in user.content
    assert "候选人：中断，让用户选择" in user.content
    assert "不能把面试官设定的假设当成候选人主动提出的方案" in system.content


async def test_text_output_empty_is_error_and_closing_has_context():
    with pytest.raises(ModelOutputError, match="空文本"):
        await backend(["  "]).probe(question(), "目标", 1)
    model = backend(["行，我们先到这里。"])
    result = await model.closing(question(), "追问到达上限", False)
    assert result.data == "行，我们先到这里。"
    assert "类型 B" in model.questioner.calls[0][0].content
    assert "追问独有证据" in model.questioner.calls[0][1].content


async def test_offline_demo_has_exact_plan_and_honest_fake_scores():
    demo = DemoBackend()
    plan = await demo.plan({"num_questions": 3, "focus": ["Go", "Agent"]})
    assert [topic["direction"] for topic in plan.data] == ["Go", "Agent", "Go"]
    main = await demo.question(plan.data[0], 1, 3)
    assert "离线演示" in main.data["text"]
    assert (await demo.coverage(main.data)).data["score"] >= 50
    probes = [(await demo.probe(main.data, "目标", hop)).data for hop in range(1, 4)]
    assert len(set(probes)) == 3
    evaluation = (await demo.evaluate(main.data)).data
    assert "非真实评测" in evaluation["judge_model"]
    assert "不代表" in evaluation["judge_text"]
