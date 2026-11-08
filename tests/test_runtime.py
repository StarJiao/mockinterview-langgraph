"""Recovery contracts using a deterministic model boundary and real SQLite files.

No provider credentials, HTTP calls, terminal, or wall-clock sleeps are needed.
"""

import asyncio
from collections import Counter
from copy import deepcopy

import pytest

from mockinterview.contracts import CallResult
from mockinterview.runtime import InterviewApp


def options(count=1, *, token_budget=100_000):
    return {
        "jd": "Go 后端工程师",
        "resume": "做过请求编排与缓存",
        "state_snapshot": "",
        "focus": ["Go"],
        "num_questions": count,
        "token_budget": token_budget,
    }


class FakeBackend:
    def __init__(self, *, coverage_score=70, plan_tokens=1, fail_probe_once=False):
        self.calls = Counter()
        self.coverage_score = coverage_score
        self.plan_tokens = plan_tokens
        self.fail_probe_once = fail_probe_once
        self.coverage_inputs = []
        self.probe_inputs = []
        self.scoring_inputs = []
        self.eval_started = {}
        self.eval_gates = {}

    async def plan(self, config):
        self.calls["plan"] += 1
        topics = [
            {
                "topic": f"go/topic-{index}",
                "points": ["说明机制"],
                "difficulty": 3,
                "source": "resume",
                "weak": False,
                "direction": "Go",
                "evidence": "project",
            }
            for index in range(config["num_questions"])
        ]
        return CallResult(topics, self.plan_tokens)

    async def question(self, topic, index, total):
        self.calls["question"] += 1
        return CallResult(
            {
                "topic": topic["topic"],
                "difficulty": 3,
                "text": f"第 {index}/{total} 题：你会怎样合并重复请求？",
                "reference_points": ["说明机制"],
                "probe_angles": [],
            },
            2,
        )

    async def coverage(self, question):
        self.calls["coverage"] += 1
        self.coverage_inputs.append(deepcopy(question))
        return CallResult(
            {
                "score": self.coverage_score,
                "answer_kind": "answer",
                "reason": "测试中的语义判断",
                "covered": [],
                "missed": ["说明机制"],
                "sufficient": self.coverage_score >= 90,
                "has_new_evidence": True,
                "next_target": "已说明合并请求；确认具体的合并范围与理由，不指定实现手段",
            },
            3,
        )

    async def probe(self, question, target, hop):
        self.calls["probe"] += 1
        self.probe_inputs.append((deepcopy(question), target, hop))
        if self.fail_probe_once:
            self.fail_probe_once = False
            raise RuntimeError("probe temporarily failed")
        return CallResult(f"第 {hop} 次追问：你刚才说的合并具体在哪里做？", 4)

    async def closing(self, question, reason, struggle):
        self.calls["closing"] += 1
        return CallResult("行，这一题先到这里。", 5)

    async def evaluate(self, question):
        self.calls["evaluate"] += 1
        qid = question["id"]
        self.calls[f"evaluate:{qid}"] += 1
        self.scoring_inputs.append(deepcopy(question))
        self.eval_started.setdefault(qid, asyncio.Event()).set()
        if gate := self.eval_gates.get(qid):
            await gate.wait()
        return CallResult(
            {"score": "12/20", "judge_model": "fake", "judge_text": "测试评分"},
            6,
        )


def app_at(directory, backend):
    return InterviewApp(directory, backend, notify=lambda _: None)


def current_question(turn):
    return turn.state["questions"][-1]


async def wait_for_evaluation(app, session_id, qid):
    """Yield to the score worker until inspect exposes its durable result."""
    async with asyncio.timeout(3):
        while True:
            turn = await app.inspect(session_id)
            if qid in turn.state.get("evaluations", {}):
                return turn
            await asyncio.sleep(0)


async def test_failed_scoring_does_not_block_completion_and_can_be_retried(tmp_path):
    class FailingBackend(FakeBackend):
        fail_score = True

        async def evaluate(self, question):
            if self.fail_score:
                self.calls["evaluate"] += 1
                raise TimeoutError("private provider details")
            return await super().evaluate(question)

    backend = FailingBackend(coverage_score=100)
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), session_id="score-failure")
        finished = await app.resume("score-failure", "完整回答")
        qid = finished.state["questions"][0]["id"]
        assert finished.done
        assert qid not in finished.state["evaluations"]
        assert finished.state["score_failures"][qid] == "TimeoutError"
        assert backend.calls["evaluate"] == 2

    async with app_at(tmp_path, backend) as app:
        saved = await app.inspect("score-failure")
        assert saved.done and saved.state["score_failures"][qid] == "TimeoutError"
        backend.fail_score = False
        repaired = await app.rescore("score-failure")
        assert repaired.done
        assert qid in repaired.state["evaluations"]
        assert repaired.state["score_failures"] == {}
        assert backend.calls["coverage"] == 1


async def test_saved_interrupt_survives_restart_and_followup_limit(tmp_path):
    backend = FakeBackend()
    async with app_at(tmp_path, backend) as app:
        first = await app.start(options(), session_id="restart")
        assert first.pending is not None
        assert not first.done
        saved_ask = first.pending["ask"]
        saved_qid = current_question(first)["id"]
        assert current_question(first)["rounds"][-1]["answer"] is None

    # A new app uses the SQLite checkpoint, not regenerated model output.
    async with app_at(tmp_path, backend) as app:
        restored = await app.resume("restart")
        assert restored.pending["ask"] == saved_ask
        assert current_question(restored)["id"] == saved_qid
        assert backend.calls["plan"] == 1
        assert backend.calls["question"] == 1

        answer_texts = ["主答\n我先确定合并范围。", "追答一", "追答二", "追答三"]
        for index, answer in enumerate(answer_texts):
            restored = await app.resume("restart", answer)
            rounds = current_question(restored)["rounds"]
            assert [r["answer"] for r in rounds[: index + 1]] == answer_texts[: index + 1]
            if index < 3:
                assert restored.pending is not None
                assert len(rounds) == index + 2
                assert rounds[-1]["answer"] is None

        assert restored.done
        assert restored.pending is None
        assert current_question(restored)["status"] == "done"
        assert len(current_question(restored)["rounds"]) == 4  # 1 main + 3 probes
        assert backend.calls["probe"] == 3
        assert backend.calls["question"] == 1
        assert saved_qid in restored.state["evaluations"]
        # Every probe sees all answers submitted so far, not only the latest one.
        assert [len(item[0]["rounds"]) for item in backend.probe_inputs] == [1, 2, 3]


async def test_none_is_not_submission_but_empty_string_is(tmp_path):
    backend = FakeBackend()
    async with app_at(tmp_path, backend) as app:
        pending = await app.start(options(), session_id="empty")
        unchanged = await app.resume("empty", None)
        assert unchanged.pending == pending.pending
        assert current_question(unchanged)["rounds"][0]["answer"] is None
        assert backend.calls["coverage"] == 0

        finished = await app.resume("empty", "")
        assert finished.done
        assert current_question(finished)["rounds"][0]["answer"] == ""
        assert len(current_question(finished)["rounds"]) == 1
        assert backend.calls["probe"] == 0
        assert backend.calls["coverage"] == 0
        assert backend.scoring_inputs[0]["rounds"][0]["answer"] == ""


async def test_scoring_runs_during_next_question_and_report_waits_for_last(tmp_path):
    backend = FakeBackend(coverage_score=100)
    async with app_at(tmp_path, backend) as app:
        first = await app.start(options(2), session_id="parallel")
        q1 = current_question(first)["id"]
        backend.eval_gates[q1] = asyncio.Event()
        backend.eval_started[q1] = asyncio.Event()

        # The first score deliberately cannot complete, but question two still appears.
        second = await asyncio.wait_for(app.resume("parallel", "第一题回答"), timeout=3)
        await asyncio.wait_for(backend.eval_started[q1].wait(), timeout=3)
        assert second.pending is not None
        assert len(second.state["questions"]) == 2
        assert second.state["questions"][0]["status"] == "done"
        assert q1 not in second.state.get("evaluations", {})
        q2 = current_question(second)["id"]
        assert q2 != q1

        backend.eval_gates[q1].set()
        scored = await wait_for_evaluation(app, "parallel", q1)
        assert scored.pending is not None  # Scoring does not consume user input.
        backend.eval_gates[q2] = asyncio.Event()
        backend.eval_started[q2] = asyncio.Event()

        finish_task = asyncio.create_task(app.resume("parallel", "第二题回答"))
        try:
            await asyncio.wait_for(backend.eval_started[q2].wait(), timeout=3)
            assert not finish_task.done()
            backend.eval_gates[q2].set()
            finished = await asyncio.wait_for(finish_task, timeout=3)
        finally:
            if not finish_task.done():
                finish_task.cancel()
                await asyncio.gather(finish_task, return_exceptions=True)

        assert finished.done
        assert set(finished.state["evaluations"]) == {q1, q2}
        assert backend.calls[f"evaluate:{q1}"] == 1
        assert backend.calls[f"evaluate:{q2}"] == 1
        assert all(q["status"] == "done" for q in backend.scoring_inputs)
        assert all(r["answer"] is not None for q in backend.scoring_inputs for r in q["rounds"])
        assert sum(finished.state["usage"].values()) > 0


async def test_durable_score_reused_after_restart_while_second_question_pending(tmp_path):
    backend = FakeBackend(coverage_score=100)
    async with app_at(tmp_path, backend) as app:
        first = await app.start(options(2), session_id="reuse")
        q1 = current_question(first)["id"]
        second = await app.resume("reuse", "先回答第一题")
        q2_ask = second.pending["ask"]
        scored = await wait_for_evaluation(app, "reuse", q1)
        stored_eval = deepcopy(scored.state["evaluations"][q1])

    async with app_at(tmp_path, backend) as app:
        restored = await app.resume("reuse")
        assert restored.pending["ask"] == q2_ask
        assert restored.state["evaluations"][q1] == stored_eval
        assert backend.calls[f"evaluate:{q1}"] == 1
        assert backend.calls["question"] == 2
        finished = await app.resume("reuse", "再回答第二题")
        assert finished.done
        assert len(finished.state["evaluations"]) == 2
        assert backend.calls[f"evaluate:{q1}"] == 1


async def test_unfinished_background_score_resubmitted_after_process_restart(tmp_path):
    old_backend = FakeBackend(coverage_score=100)
    async with app_at(tmp_path, old_backend) as app:
        first = await app.start(options(2), session_id="unfinished-score")
        q1 = current_question(first)["id"]
        old_backend.eval_gates[q1] = asyncio.Event()
        old_backend.eval_started[q1] = asyncio.Event()
        second = await app.resume("unfinished-score", "第一题已作答")
        await asyncio.wait_for(old_backend.eval_started[q1].wait(), timeout=3)
        assert second.pending is not None
        assert q1 not in second.state.get("evaluations", {})
        saved_question = second.pending["ask"]
        # Context exit cancels the process-local task, not the saved interview.

    new_backend = FakeBackend(coverage_score=100)
    async with app_at(tmp_path, new_backend) as app:
        restored = await app.resume("unfinished-score")
        assert restored.pending["ask"] == saved_question
        scored = await wait_for_evaluation(app, "unfinished-score", q1)
        assert q1 in scored.state["evaluations"]
        assert new_backend.calls[f"evaluate:{q1}"] == 1
        assert new_backend.calls["plan"] == 0
        assert new_backend.calls["question"] == 0
        assert new_backend.scoring_inputs[0]["rounds"][0]["answer"] == "第一题已作答"


async def test_failed_probe_retries_checkpoint_without_repeating_answer_or_decision(tmp_path):
    backend = FakeBackend(fail_probe_once=True)
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), session_id="retry")
        with pytest.raises(RuntimeError, match="probe temporarily failed"):
            await app.resume("retry", "同一 key 的请求共享计算结果")
        assert backend.calls["coverage"] == 1
        assert backend.calls["probe"] == 1
        failed = await app.inspect("retry")
        assert len(current_question(failed)["rounds"]) == 1
        assert current_question(failed)["rounds"][0]["answer"] == "同一 key 的请求共享计算结果"

    async with app_at(tmp_path, backend) as app:
        retried = await app.resume("retry")
        assert retried.pending is not None
        assert len(current_question(retried)["rounds"]) == 2
        assert current_question(retried)["rounds"][1]["answer"] is None
        assert backend.calls["coverage"] == 1
        assert backend.calls["probe"] == 2
        assert backend.calls["question"] == 1


async def test_planning_exhausts_budget_before_generating_any_question(tmp_path):
    backend = FakeBackend(plan_tokens=100)
    async with app_at(tmp_path, backend) as app:
        finished = await app.start(options(3, token_budget=100), session_id="budget")
        assert finished.done
        assert finished.pending is None
        assert finished.state["questions"] == []
        assert finished.state.get("evaluations", {}) == {}
        assert backend.calls["question"] == 0
        assert backend.calls["evaluate"] == 0
        assert sum(finished.state["usage"].values()) == 100


async def test_failed_planning_usage_stops_new_questions_after_restart(tmp_path):
    from mockinterview.models import ModelOutputError

    class FailedPlan(FakeBackend):
        async def plan(self, config):
            raise ModelOutputError("规划输出无效", tokens=99)

    async with app_at(tmp_path, FailedPlan()) as app:
        with pytest.raises(ModelOutputError):
            await app.start(options(token_budget=100), "plan-budget")

    backend = FakeBackend(plan_tokens=1)
    async with app_at(tmp_path, backend) as app:
        finished = await app.resume("plan-budget")
        assert finished.done
        assert finished.state["questions"] == []
        assert backend.calls["question"] == 0
        assert sum(finished.state["usage"].values()) == 100
        again = await app.resume("plan-budget")
        assert sum(again.state["usage"].values()) == 100
        assert backend.calls["plan"] == 1


async def test_session_identity_is_not_silently_overwritten(tmp_path):
    backend = FakeBackend()
    async with app_at(tmp_path, backend) as app:
        original = await app.start(options(), session_id="identity")
        with pytest.raises((ValueError, KeyError)):
            await app.start(options(), session_id="identity")
        with pytest.raises((ValueError, KeyError)):
            await app.resume("does-not-exist")
        with pytest.raises((ValueError, KeyError)):
            await app.inspect("does-not-exist")
        preserved = await app.inspect("identity")
        assert preserved.pending == original.pending
        assert backend.calls["plan"] == 1
        assert len(await app.sessions()) == 1


async def test_single_writer_lock_prevents_two_processes_editing_session(tmp_path):
    backend = FakeBackend()
    async with app_at(tmp_path, backend) as first:
        await first.start(options(), "locked")
        async with app_at(tmp_path, backend) as second:
            with pytest.raises(ValueError, match="另一进程"):
                await second.resume("locked", "不能写入")
            unchanged = await second.inspect("locked")
            assert current_question(unchanged)["rounds"][0]["answer"] is None
    async with app_at(tmp_path, backend) as third:
        assert (await third.resume("locked")).pending


async def test_failed_model_usage_is_durable_and_not_counted_twice(tmp_path):
    from mockinterview.models import ModelOutputError

    class InvalidOnce(FakeBackend):
        async def probe(self, question, target, hop):
            if self.calls["probe"] == 0:
                self.calls["probe"] += 1
                raise ModelOutputError("输出无效", tokens=17)
            return await super().probe(question, target, hop)

    backend = InvalidOnce()
    async with app_at(tmp_path, backend) as app:
        await app.start(options(), "known-usage")
        with pytest.raises(ModelOutputError):
            await app.resume("known-usage", "已提交回答")
        failed = await app.inspect("known-usage")
        assert sum(v for k, v in failed.state["usage"].items() if k.startswith("failed:")) == 17
    async with app_at(tmp_path, backend) as app:
        retried = await app.resume("known-usage")
        assert retried.pending["hop"] == 1
        expected = 1 + 2 + 3 + 17 + 4
        assert sum(retried.state["usage"].values()) == expected
        assert sum((await app.inspect("known-usage")).state["usage"].values()) == expected


async def test_question_checkpoint_failure_does_not_regenerate_saved_model_result(tmp_path):
    backend = FakeBackend()
    async with app_at(tmp_path, backend) as app:
        original_put = app.checkpointer.aput

        async def fail_on_question(config, checkpoint, metadata, new_versions):
            if checkpoint.get("channel_values", {}).get("questions"):
                raise OSError("simulated checkpoint write failure")
            return await original_put(config, checkpoint, metadata, new_versions)

        app.checkpointer.aput = fail_on_question
        with pytest.raises(OSError, match="checkpoint write"):
            await app.start(options(), "write-failure")
    async with app_at(tmp_path, backend) as app:
        recovered = await app.resume("write-failure")
        assert recovered.pending
        assert backend.calls["question"] == 1
        assert current_question(recovered)["rounds"][0]["answer"] is None


async def test_stage_metrics_are_written_by_cli_runtime_and_record_failures(tmp_path):
    from mockinterview.models import ModelOutputError

    class FailedPlan(FakeBackend):
        async def plan(self, config):
            raise ModelOutputError("规划输出无效", tokens=99)

    # The CLI runtime has no live StageTracker; metrics must still land in SQLite.
    async with app_at(tmp_path, FailedPlan()) as app:
        with pytest.raises(ModelOutputError):
            await app.start(options(), "metric-failure")
        failed = await app.store.stage_metrics("metric-failure")
        assert len(failed) == 1
        assert failed[0]["key"] == "plan"
        assert failed[0]["label"] == "生成考点计划"
        assert failed[0]["ok"] is False
        assert failed[0]["error"] == "ModelOutputError"
        assert failed[0]["tokens"] == 99
        assert failed[0]["duration_ms"] >= 0
        assert "规划输出无效" not in str(failed[0])  # provider text is not stored

    async with app_at(tmp_path, FakeBackend()) as app:
        assert await app.store.stage_metrics("metric-failure") == failed
        summary = await app.store.stage_summary("metric-failure")
        assert summary["calls"] == 1
        assert summary["total_tokens"] == 99
        assert summary["failures"] == 1
