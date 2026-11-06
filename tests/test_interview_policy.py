"""Policy/recovery regressions using declared semantic outputs, not real-model evals.

The utterances come from the failure modes under review. Fake coverage outputs
verify routing contracts; they do NOT prove an LLM will classify them correctly.
"""

from copy import deepcopy

import pytest

from mockinterview.report import render_report
from mockinterview.schemas import DIMENSIONS
from mockinterview.state import count_rounds, decide_probe, round_label
from test_runtime import FakeBackend, app_at, current_question, options


def assessment(**overrides):
    return {
        "answer_kind": "answer",
        "reason": "已给出部分证据",
        "score": 70,
        "covered": [],
        "missed": ["防复发"],
        "sufficient": False,
        "has_new_evidence": True,
        "next_target": "已确认止血流程；下一轮只确认防复发方案，不指定实现手段",
        **overrides,
    }


class ScriptedBackend(FakeBackend):
    def __init__(self, judgments):
        super().__init__()
        self.judgments = iter(judgments)

    async def coverage(self, question):
        result = await super().coverage(question)
        result.data = next(self.judgments)
        return result

    async def probe(self, question, target, hop):
        result = await super().probe(question, target, hop)
        if question["rounds"][-1]["decision"]["action"] == "CLARIFY":
            result.data = "补充一个假设：持续十分钟。按这个条件，你会先排查什么？"
        return result


async def test_low_coverage_gets_one_supplement_before_no_progress_stop_after_restart(tmp_path):
    backend = ScriptedBackend(
        [
            assessment(score=10),
            assessment(score=10, has_new_evidence=False),
        ]
    )
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "supplement")
        turn = await app.resume("supplement", "先回滚，再定位受影响数据")
        assert turn.pending["hop"] == 1
        assert turn.pending["kind"] == "probe"
        assert current_question(turn)["rounds"][0]["decision"]["action"] == "SUPPLEMENT"
        assert "引导他说出来" not in backend.probe_inputs[0][1]
        ask = turn.pending["ask"]

    async with app_at(tmp_path, backend) as app:
        restored = await app.resume("supplement")
        assert restored.pending["ask"] == ask
        finished = await app.resume("supplement", "还是先回滚")
        assert finished.done
        assert finished.state["decision"]["reason_code"] == "no_progress"
        assert backend.calls["probe"] == 1
        assert backend.calls["coverage"] == 2
        assert len(current_question(finished)["rounds"]) == 2


async def test_clarification_and_three_probes_survive_restart_without_receipt_collision(tmp_path):
    backend = ScriptedBackend(
        [
            assessment(answer_kind="clarification", score=10, next_target="需说明持续时间"),
            assessment(score=25),
            assessment(),
            assessment(),
            assessment(),
        ]
    )
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "clarify")
        turn = await app.resume("clarify", "一段时间是多久，秒级还是很久？")
        assert turn.pending["kind"] == "clarification"
        assert turn.pending["hop"] == 0
        assert count_rounds(current_question(turn), "probe") == 0
        assert turn.state["decision"]["action"] == "CLARIFY"
        saved_ask = turn.pending["ask"]

    async with app_at(tmp_path, backend) as app:
        restored = await app.resume("clarify")
        assert restored.pending["ask"] == saved_ask
        assert backend.calls["probe"] == 1
        turn = await app.resume("clarify", "先回滚，再查受影响的数据")
        assert turn.pending["kind"] == "probe" and turn.pending["hop"] == 1
        assert turn.pending["ask"] != saved_ask

    # Resume in the middle of formal probes as well as after the clarification.
    async with app_at(tmp_path, backend) as app:
        restored = await app.resume("clarify")
        assert restored.pending["hop"] == 1
        for hop in (2, 3):
            turn = await app.resume("clarify", f"补充机制证据 {hop}")
            assert turn.pending["hop"] == hop
        finished = await app.resume("clarify", "最终说明边界与取舍")
        assert finished.done
        q = current_question(finished)
        assert len(q["rounds"]) == 5  # main + one clarification + three probes
        assert count_rounds(q, "clarification") == 1
        assert count_rounds(q, "probe") == 3
        assert [item[2] for item in backend.probe_inputs] == [0, 1, 2, 3]
        assert backend.calls["probe"] == 4
        assert len([k for k in finished.state["usage"] if k.startswith("probe:v2:")]) == 4
        assert [round_label(q, i) for i in range(5)] == [
            "主问",
            "题意澄清",
            "追问 1",
            "追问 2",
            "追问 3",
        ]


async def test_clarification_at_last_probe_still_has_one_bounded_opportunity(tmp_path):
    backend = ScriptedBackend(
        [
            assessment(),
            assessment(),
            assessment(),
            assessment(answer_kind="clarification", next_target="解释最后追问的条件"),
            assessment(),
        ]
    )
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "last-clarification")
        for _ in range(3):
            turn = await app.resume("last-clarification", "给出部分机制")
        assert turn.pending["hop"] == 3
        turn = await app.resume("last-clarification", "最后这个问题的约束是什么？")
        assert turn.pending["kind"] == "clarification" and turn.pending["hop"] == 3
        finished = await app.resume("last-clarification", "按补充条件给出回答")
        assert finished.done
        assert len(current_question(finished)["rounds"]) == 5
        assert finished.state["decision"]["reason_code"] == "probe_limit"


async def test_repeated_clarification_ends_as_limited_evidence_not_inability(tmp_path):
    backend = ScriptedBackend([assessment(answer_kind="clarification")] * 2)
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "bounded-clarification")
        await app.resume("bounded-clarification", "需要明确时间范围")
        finished = await app.resume("bounded-clarification", "还有一个必要条件没说明")
        assert finished.done
        assert finished.state["decision"]["reason_code"] == "clarification_limit"
        assert "吃力" not in current_question(finished)["closing_reason"]
        assert count_rounds(current_question(finished), "probe") == 0
        assert backend.calls["probe"] == 1


async def test_supplement_is_not_consumed_by_a_clarification_reply(tmp_path):
    backend = ScriptedBackend(
        [
            assessment(score=10),
            assessment(score=10, answer_kind="clarification", has_new_evidence=False),
            assessment(score=20, has_new_evidence=True),
        ]
    )
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "supplement-clarify")
        await app.resume("supplement-clarify", "先回滚")
        turn = await app.resume("supplement-clarify", "你说的旧任务是否也回滚？")
        assert turn.pending["kind"] == "clarification"
        turn = await app.resume("supplement-clarify", "暂停旧任务，然后核对写入归属")
        assert not turn.done
        assert turn.pending["hop"] == 2


@pytest.mark.parametrize(
    "judgment, reason",
    [
        (assessment(answer_kind="give_up", score=0), "give_up"),
        (assessment(sufficient=True, score=95, missed=[]), "sufficient"),
    ],
)
async def test_explicit_skip_or_sufficient_evidence_does_not_force_followups(
    tmp_path, judgment, reason
):
    backend = ScriptedBackend([judgment])
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "early-end")
        turn = await app.resume("early-end", "测试中的显式跳过或充分回答")
        assert turn.done
        assert turn.state["decision"]["reason_code"] == reason
        assert backend.calls["probe"] == 0


async def test_budget_after_coverage_prevents_another_model_probe(tmp_path):
    backend = ScriptedBackend([assessment(score=10)])
    async with app_at(tmp_path, backend) as app:
        await app.start(options(token_budget=6), "coverage-budget")
        finished = await app.resume("coverage-budget", "部分回答")
        assert finished.done  # plan 1 + question 2 + coverage 3 uses the budget
        assert finished.state["decision"]["reason_code"] == "budget"
        assert backend.calls["probe"] == 0


def test_legacy_rounds_keep_their_original_probe_count_without_mutation():
    question = {"rounds": [{"ask": "主问"}, {"ask": "旧追问一"}, {"ask": "旧追问二"}]}
    before = deepcopy(question)
    assert count_rounds(question, "probe") == 2
    assert round_label(question, 2, with_limit=True) == "追问 2/3"
    assert decide_probe(assessment(), question)["action"] == "PROBE"
    assert question == before


async def test_clarification_receipt_survives_checkpoint_failure(tmp_path):
    backend = ScriptedBackend([assessment(answer_kind="clarification")])
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "clarify-write-failure")
        original_put = app.checkpointer.aput

        async def fail_on_clarification(config, checkpoint, metadata, new_versions):
            questions = checkpoint.get("channel_values", {}).get("questions", [])
            if questions and count_rounds(questions[-1], "clarification"):
                raise OSError("simulated clarification checkpoint failure")
            return await original_put(config, checkpoint, metadata, new_versions)

        app.checkpointer.aput = fail_on_clarification
        with pytest.raises(OSError, match="clarification checkpoint"):
            await app.resume("clarify-write-failure", "秒级还是很久？")
    async with app_at(tmp_path, backend) as app:
        turn = await app.resume("clarify-write-failure")
        assert turn.pending["kind"] == "clarification"
        assert turn.pending["hop"] == 0
        assert backend.calls["probe"] == 1
        assert backend.calls["coverage"] == 1
        assert len(current_question(turn)["rounds"]) == 2


async def test_unassessed_score_is_complete_and_not_retried_after_restart(tmp_path):
    class UnassessedBackend(FakeBackend):
        async def evaluate(self, question):
            result = await super().evaluate(question)
            result.data = {
                "score": None,
                "assessed_dimensions": 0,
                "judge_model": "fake",
                "dimensions": [
                    {
                        "name": name,
                        "score": None,
                        "assessments": [
                            {
                                "kind": "unclear_question",
                                "round_index": 1,
                                "candidate_quote": "需要明确题目的条件",
                                "explanation": "必要条件缺失，无法据此判断能力",
                            }
                        ],
                        "evidence": ["应先补足题目条件"],
                    }
                    for name in DIMENSIONS
                ],
            }
            return result

    backend = UnassessedBackend(coverage_score=100)
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "unassessed")
        finished = await app.resume("unassessed", "需要明确题目的条件")
        assert finished.done
        qid = current_question(finished)["id"]
        evaluation = finished.state["evaluations"][qid]
        assert evaluation["score"] is None
        assert "已评估 0/4 个维度" in render_report(finished.state)

    async with app_at(tmp_path, backend) as app:
        restored = await app.resume("unassessed")
        assert restored.done
        assert restored.state["evaluations"][qid] == evaluation
        assert backend.calls["evaluate"] == 1
