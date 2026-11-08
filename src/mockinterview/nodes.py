"""Business nodes return updates; the runtime applies and checkpoints them."""

from copy import deepcopy

from langgraph.types import interrupt

from .state import MAX_PROBES, budget_exceeded, conclude, count_rounds, decide_probe, round_kind
from .telemetry import null_stage


class Nodes:
    def __init__(self, backend, store, jobs, notify, recorder=None):
        self.backend, self.store, self.jobs, self.notify = backend, store, jobs, notify
        self.recorder = recorder

    async def call(self, state: dict, key: str, invoke):
        session_id = state["session_id"]
        result = await self.store.get(session_id, key)
        if result is not None:
            # A replay reuses the durable receipt: no model call, no new metric.
            return deepcopy(result.data), {key: result.tokens}
        stage = self.recorder.stage(session_id, key) if self.recorder else null_stage()
        async with stage as metric:
            try:
                result = await invoke()
            except Exception as exc:
                await self.store.record_failed_usage(
                    session_id, getattr(exc, "tokens", 0)
                )
                raise
            metric["tokens"] = int(result.tokens or 0)
            await self.store.put(session_id, key, result)
        return deepcopy(result.data), {key: result.tokens}

    async def collected(self, state: dict) -> dict:
        evaluations = {}
        usage = await self.store.failed_usage(state["session_id"])
        for question in state.get("questions", []):
            if question["status"] != "done":
                continue
            key = f"eval:{question['id']}"
            result = await self.store.get(state["session_id"], key)
            if result is not None:
                evaluations[question["id"]] = result.data
                usage[key] = result.tokens
        return {"evaluations": evaluations, "usage": usage}

    async def plan(self, state: dict) -> dict:
        preset = state["options"].get("preset_questions")
        if preset:
            return {"plan": [
                {"topic": q["topic"], "direction": "预定题目", "points": q["reference_points"],
                 "difficulty": q["difficulty"], "source": "preset", "weak": False,
                 "evidence": "theoretical"}
                for q in preset
            ]}
        self.notify("生成本场有序考点…")
        plan, usage = await self.call(state, "plan", lambda: self.backend.plan(state["options"]))
        if len(plan) != state["options"]["num_questions"]:
            raise ValueError("考点数量与请求题数不一致")
        # Routing reads checkpointed State, not inspect()'s receipt projection.
        # Include prior failed attempts before deciding whether to start a question.
        collected = await self.collected(state)
        return {**collected, "plan": plan, "usage": {**collected["usage"], **usage}}

    async def question(self, state: dict) -> dict:
        index = len(state["questions"])
        topic = state["plan"][index]
        preset = state["options"].get("preset_questions")
        if preset:
            fixed = preset[index]
            draft = {"topic": fixed["topic"], "difficulty": fixed["difficulty"],
                     "text": fixed["rounds"][0]["ask"],
                     "reference_points": fixed["reference_points"], "probe_angles": []}
            usage = {}
        else:
            self.notify(f"正在生成第 {index + 1} 题：{topic['topic']}")
            draft, usage = await self.call(
                state,
                f"question:{index}",
                lambda: self.backend.question(topic, index + 1, len(state["plan"])),
            )
        questions = deepcopy(state["questions"])
        questions.append(
            {
                **draft,
                "id": f"{state['session_id']}-q{index + 1}",
                "status": "running",
                "rounds": deepcopy(fixed["rounds"]) if preset else
                [{"kind": "main", "ask": draft["text"], "answer": None}],
                "closing": "",
                "closing_reason": "",
            }
        )
        collected = await self.collected(state)
        return {**collected, "questions": questions, "usage": {**collected["usage"], **usage}}

    async def answer(self, state: dict) -> dict:
        # No IO/model call before interrupt: this node restarts on resume.
        q = state["questions"][-1]
        answer = interrupt(
            {
                "question_id": q["id"],
                "index": len(state["questions"]) - 1,
                "total": len(state["plan"]),
                "topic": q["topic"],
                "difficulty": q["difficulty"],
                "ask": q["rounds"][-1]["ask"],
                "hop": count_rounds(q, "probe"),
                "kind": round_kind(q, len(q["rounds"]) - 1),
            }
        )
        if not isinstance(answer, str):
            raise ValueError("答案必须是字符串，空字符串表示主动提交空回答")
        questions = deepcopy(state["questions"])
        questions[-1]["rounds"][-1]["answer"] = answer
        return {**await self.collected(state), "questions": questions}

    async def decide(self, state: dict) -> dict:
        collected = await self.collected(state)
        effective = {**state, "usage": {**state["usage"], **collected["usage"]}}
        questions = deepcopy(state["questions"])
        q = questions[-1]
        answer = q["rounds"][-1]["answer"]
        if answer is None:
            raise ValueError("未提交回答，不可快评")
        round_index = len(q["rounds"]) - 1
        if budget_exceeded(effective):
            decision = conclude("预算耗尽，结束本题", "budget")
        elif not answer.strip():
            decision = conclude("已提交空回答，结束本题", "empty")
        else:
            cov, usage = await self.call(
                state, f"coverage:v2:{q['id']}:{round_index}", lambda: self.backend.coverage(q)
            )
            collected["usage"].update(usage)
            q["rounds"][-1]["coverage"] = cov["score"]
            q["rounds"][-1]["assessment"] = cov
            effective["usage"].update(usage)
            decision = (
                conclude("快评后预算耗尽，结束本题", "budget")
                if budget_exceeded(effective)
                else decide_probe(cov, q)
            )
        q["rounds"][-1]["decision"] = decision
        return {**collected, "questions": questions, "decision": decision}

    async def probe(self, state: dict) -> dict:
        questions = deepcopy(state["questions"])
        q = questions[-1]
        clarifying = state["decision"]["action"] == "CLARIFY"
        hop = count_rounds(q, "probe") + (0 if clarifying else 1)
        self.notify(
            "正在补充题意（不占追问次数）…" if clarifying else f"正在生成追问 {hop}/{MAX_PROBES}…"
        )
        # Physical round index is the receipt identity, not the probe count:
        # a clarification and a probe may have the same displayed hop.
        round_index = len(q["rounds"])
        ask, usage = await self.call(
            state,
            f"probe:v2:{q['id']}:{round_index}",
            lambda: self.backend.probe(q, state["decision"]["target"], hop),
        )
        if not ask.strip():
            raise ValueError("追问文本为空")
        q["rounds"].append(
            {"kind": "clarification" if clarifying else "probe", "ask": ask, "answer": None}
        )
        return {"questions": questions, "usage": usage}

    async def finish_question(self, state: dict) -> dict:
        questions = deepcopy(state["questions"])
        q = questions[-1]
        reason = state["decision"]["reason"]
        struggle = state["decision"].get("reason_code") in {
            "give_up",
            "no_progress",
            "clarification_limit",
            "empty",
            "budget",
        }
        closing, usage = await self.call(
            state, f"closing:{q['id']}", lambda: self.backend.closing(q, reason, struggle)
        )
        q.update(status="done", closing=closing, closing_reason=reason)
        return {"questions": questions, "usage": usage}

    async def submit_score(self, state: dict) -> dict:
        # The preceding node's done snapshot is already checkpointed.
        await self.jobs.submit(state["session_id"], state["questions"][-1])
        return await self.collected(state)

    async def report(self, state: dict) -> dict:
        for q in state["questions"]:
            if q["status"] == "done":
                await self.jobs.submit(state["session_id"], q)
        self.notify("等待所有单题评分完成…")
        await self.jobs.wait(state["session_id"])
        collected = await self.collected(state)
        return {**collected, "status": "done"}
