"""LangChain model adapters and an explicitly fake offline backend.

Responses are collected via ainvoke, validated, and returned to the Graph.
The CLI displays only checkpointed questions; partial model output never leaks
onto the screen. No native tool/schema feature is required from an OpenAI-
compatible endpoint: Pydantic validates JSON locally with one repair attempt.
"""

import json
import re
from typing import Callable, TypeVar

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from . import prompts
from .contracts import CallResult
from .schemas import DIMENSIONS, CoverageOutput, EvaluationOutput, PlanOutput, QuestionOutput
from .state import round_label

T = TypeVar("T", bound=BaseModel)


class ModelOutputError(ValueError):
    """Bounded output validation failed; tokens reports known usage so far."""

    def __init__(self, message: str, tokens: int = 0):
        super().__init__(message)
        self.tokens = tokens


def rounds_digest(question: dict) -> str:
    lines = []
    for i, rd in enumerate(question.get("rounds", [])):
        if rd is None or rd.get("answer") is None:
            continue
        label = round_label(question, i)
        answer = rd["answer"] if rd["answer"] != "" else "（已提交空回答）"
        # Keep the complete question and all submitted rounds; don't substitute
        # only the latest answer, which would erase already covered evidence.
        lines.append(f"[回合 {i + 1} · {label}] 面试官：{rd['ask']}\n候选人：{answer}")
    return "\n".join(lines)


def question_style_issues(text: str) -> list[str]:
    issues = []
    text = text.strip()
    if not text:
        return ["题面为空"]
    if len(text) > 200:
        issues.append(f"题面过长（{len(text)} 字 > 200 字上限）")
    template = re.search(
        r"请从.{0,24}(方面|角度|维度)|谈谈你对.{0,24}的(理解|看法|认识)|"
        r"请(详述|阐述|论述|说明)|分别(说明|阐述|论述|介绍)|有哪些方面",
        text,
    )
    if template:
        issues.append(f"命中机器之问模板「{template.group()}」")
    count = len(re.findall(r"什么|如何|怎么|为什么|为何|哪些", text))
    if count >= 2:
        issues.append(f"题面含 {count} 个疑问词（一次问多点）")
    return issues


def _json_object(text: str) -> dict:
    """Accept a bare object or an object wrapped in prose/Markdown fences."""
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # raw_decode respects braces inside strings, unlike a greedy regex.
        start = text.find("{")
        if start < 0:
            raise ValueError("响应中没有 JSON 对象") from None
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("模型必须输出 JSON 对象")
    return value


def _content(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    # Standard text content blocks only, never reasoning blocks.
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _tokens(message) -> int:
    usage = getattr(message, "usage_metadata", None) or {}
    if "total_tokens" in usage:
        return int(usage["total_tokens"])
    legacy = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    return int(
        legacy.get(
            "total_tokens", legacy.get("prompt_tokens", 0) + legacy.get("completion_tokens", 0)
        )
    )


def _tidy_line(text: str) -> str:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    for left, right in [('"', '"'), ("「", "」"), ("“", "”")]:
        if len(text) > 2 and text.startswith(left) and text.endswith(right):
            text = text[len(left) : -len(right)].strip()
    return text


class ModelBackend:
    def __init__(
        self,
        config,
        notify: Callable[[str], None] = lambda msg: None,
        *,
        questioner=None,
        evaluator=None,
    ):
        self.notify = notify
        self.judge_model = config.evaluator_model
        self.questioner = (
            questioner
            if questioner is not None
            else ChatOpenAI(
                model=config.questioner_model,
                api_key=config.questioner_api_key,
                base_url=config.questioner_base_url,
                timeout=config.timeout,
                max_retries=2,
                temperature=0.5,
                use_responses_api=False,
            )
        )
        self.evaluator = (
            evaluator
            if evaluator is not None
            else ChatOpenAI(
                model=config.evaluator_model,
                api_key=config.evaluator_api_key,
                base_url=config.evaluator_base_url,
                timeout=config.timeout,
                max_retries=2,
                temperature=0.5,
                use_responses_api=False,
            )
        )

    async def _structured(
        self, model, messages: list, schema: type[T], validate=None
    ) -> CallResult:
        messages = list(messages)
        total = 0
        for attempt in range(2):
            try:
                message = await model.ainvoke(messages)
            except Exception as exc:
                if total:
                    raise ModelOutputError(f"纠错调用失败：{type(exc).__name__}", total) from exc
                raise
            total += _tokens(message)
            text = _content(message)
            try:
                data = schema.model_validate(_json_object(text))
                parsed = data.model_dump()
                if validate is not None:
                    validate(parsed)
                return CallResult(parsed, total)
            except (ValueError, ValidationError) as exc:
                if attempt:
                    raise ModelOutputError(f"模型 JSON 无效（已纠错一次）：{exc}", total) from exc
                self.notify("模型 JSON 格式不合格，纠错重试一次…")
                messages += [
                    AIMessage(content=text),
                    HumanMessage(
                        content=(
                            f"上次输出未通过 JSON 校验：{exc}\n请只输出符合以下 schema 的完整 JSON：\n"
                            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
                        )
                    ),
                ]
        raise AssertionError("unreachable")

    async def _text(self, system: str, user: str) -> CallResult:
        message = await self.questioner.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        text = _tidy_line(_content(message))
        if not text:
            raise ModelOutputError("模型返回空文本", _tokens(message))
        return CallResult(text, _tokens(message))

    async def plan(self, options: dict) -> CallResult:
        count = options["num_questions"]
        user = f"目标 JD：\n{options.get('jd', '')}\n\n候选人简历：\n{options.get('resume', '')}\n"
        if options.get("state_snapshot", "").strip():
            user += f"\n候选人掌握状态快照（薄弱点加权依据）：\n{options['state_snapshot']}\n"
        user += f"\n本场 focus 方向：{' / '.join(options.get('focus', []))}；必须输出恰好 {count} 个有序考点。"
        messages = [SystemMessage(content=prompts.PLAN), HumanMessage(content=user)]
        result = await self._structured(self.questioner, messages, PlanOutput)
        total = result.tokens
        if len(result.data["topics"]) != count:
            received = len(result.data["topics"])
            self.notify(f"考点数 {received} 与请求题数 {count} 不一致，纠错重试…")
            messages += [
                AIMessage(content=json.dumps(result.data, ensure_ascii=False)),
                HumanMessage(
                    content=(
                        f"上次输出了 {received} 个考点，本场要求恰好 {count} 个。"
                        f"请重新筛选并输出完整 JSON；方向覆盖及薄弱点必须在这 {count} 个名额内安排，不得增加数量。"
                    )
                ),
            ]
            try:
                result = await self._structured(self.questioner, messages, PlanOutput)
            except Exception as exc:
                raise ModelOutputError(
                    f"考点数量纠错失败：{type(exc).__name__}", total + getattr(exc, "tokens", 0)
                ) from exc
            total += result.tokens
            if len(result.data["topics"]) != count:
                raise ModelOutputError(
                    f"考点数 {len(result.data['topics'])} 与请求题数 {count} 不一致（已纠错重试一次）",
                    total,
                )
        normalized = 0
        for topic in result.data["topics"]:
            if topic["evidence"] not in ("project", "tested"):
                topic.update(
                    evidence="theoretical", weak=False, difficulty=min(topic["difficulty"], 2)
                )
                normalized += 1
        if normalized:
            self.notify(f"虚实识别：{normalized} 个考点仅理论推测，已降难度至 ≤2、不占薄弱加权。")
        return CallResult(result.data["topics"], total)

    async def question(self, topic: dict, index: int, total: int) -> CallResult:
        user = (
            f"指定考点：{topic['topic']}\n难度：{topic['difficulty']}；证据：{topic['evidence']}\n"
            f"评价要点：{'；'.join(topic['points'])}\n请出本场第 {index}/{total} 题。"
        )

        async def ask(extra=""):
            return await self._structured(
                self.questioner,
                [SystemMessage(content=prompts.QUESTION), HumanMessage(content=user + extra)],
                QuestionOutput,
            )

        result = await ask()
        tokens = result.tokens
        if result.data["topic"] != topic["topic"]:
            self.notify("选题偏离固定计划，纠错重试…")
            try:
                result = await ask("\n选题偏离固定计划，topic 必须为：" + topic["topic"])
            except Exception as exc:
                raise ModelOutputError(
                    f"题目主题纠错失败：{type(exc).__name__}", tokens + getattr(exc, "tokens", 0)
                ) from exc
            tokens += result.tokens
        issues = question_style_issues(result.data["text"])
        if issues:
            self.notify(
                f"题面不合格（{'；'.join(issues)}），纠错重试…\n被拒题面：{result.data['text']}"
            )
            try:
                corrected = await ask(
                    f"\n上次题面：{result.data['text']}\n问题：{'；'.join(issues)}\n"
                    "保持同一考点和难度，改成背景铺垫 + 单一落脚问点。不得使用机器模板，不得泄露评价锚点。"
                )
            except Exception as exc:
                tokens += getattr(exc, "tokens", 0)
                self.notify(f"题面纠错失败，沿用首次题面：{type(exc).__name__}")
            else:
                result = corrected
                tokens += corrected.tokens
                if question_style_issues(result.data["text"]):
                    self.notify("题面仍不合格，采用新版本继续（沿用 Go 版策略）。")
        data = result.data
        if data["topic"] != topic["topic"]:
            raise ModelOutputError(
                f"出题主题 {data['topic']!r} 偏离计划 {topic['topic']!r}", tokens
            )
        if not data["reference_points"]:
            raise ModelOutputError("出题缺少评价锚点", tokens)
        if not 1 <= data["difficulty"] <= 5:
            data["difficulty"] = 3
        if topic["evidence"] == "theoretical":
            data["difficulty"] = min(data["difficulty"], 2)
        return CallResult(data, tokens)

    async def coverage(self, question: dict) -> CallResult:
        user = (
            f"题目：{question['text']}\n评价锚点：\n"
            + "\n".join(question["reference_points"])
            + f"\n对话轮次：\n{rounds_digest(question)}"
        )
        result = await self._structured(
            self.questioner,
            [SystemMessage(content=prompts.COVERAGE), HumanMessage(content=user)],
            CoverageOutput,
        )
        result.data["score"] = max(0, min(100, result.data["score"]))
        return result

    async def probe(self, question: dict, target: str, hop: int) -> CallResult:
        clarifying = question.get("rounds", [{}])[-1].get("decision", {}).get("action") == "CLARIFY"
        user = (
            f"原题：{question['text']}\n本轮目标：{target}\n\n完整问答：\n{rounds_digest(question)}"
        )
        if clarifying:
            user += "\n先回应最新的题意澄清，再请候选人继续回答原问题。不提出新的考点。"
        else:
            user += (
                f"\n备选考察角度（不得重复已确认的内容）：{'；'.join(question.get('probe_angles', []))}"
                f"\n请给出第 {hop} 次正式追问；必要时先交代背景，只问一个点。"
            )
        return await self._text(prompts.CLARIFY if clarifying else prompts.PROBE, user)

    async def closing(self, question: dict, reason: str, struggle: bool) -> CallResult:
        rule = (
            "收尾类型 A（证据受限）：中性地结束本题，不说候选人吃力或不会，不用‘没事’‘不纠结’等暗示表现差的话；题意未对齐时承认本题条件未对齐。"
            if struggle
            else "收尾类型 B（正常收尾）：简短回顾一个实际讨论过的点，然后收束；不空洞夸奖，也不把未核实的说法认定为正确。"
        )
        return await self._text(
            prompts.CLOSING.format(rule=rule),
            f"题目：{question['text']}\n收尾原因：{reason}\n\n对话轮次：\n{rounds_digest(question)}",
        )

    async def evaluate(self, question: dict) -> CallResult:
        user = (
            f"题目：{question['text']}\n考点：{question['topic']}（难度 {question['difficulty']}）\n评价锚点：\n"
            + "\n".join(question["reference_points"])
            + f"\n完整对话（评分对象 = 以下全部轮次的累积）：\n{rounds_digest(question)}"
            + f"\n本题结束原因（流程事实，不是能力结论）：{question.get('closing_reason', '未提供')}"
        )

        def validate_evidence(data):
            names = []
            for dimension in data["dimensions"]:
                dimension["name"] = re.sub(
                    r"^\d{1,2}(?:\. |、|．|\) |）)\s*", "", dimension["name"].strip()
                )
                names.append(dimension["name"])
                for item in dimension["assessments"]:
                    index = item["round_index"] - 1
                    rounds = question.get("rounds", [])
                    if index >= len(rounds) or rounds[index].get("answer") is None:
                        raise ValueError("评价证据引用了不存在或未作答的回合")
                    quote = item["candidate_quote"]
                    if quote and quote not in rounds[index]["answer"]:
                        raise ValueError("candidate_quote 必须是所引用回合中候选人的真实原文片段")
                    if item["kind"] in {"correct", "incorrect"} and not quote.strip():
                        raise ValueError("判定明确正确或错误时必须引用候选人原文")
            if len(names) != 4 or set(names) != set(DIMENSIONS):
                raise ValueError("judge 输出维度不全或重复：必须恰好包含四个评价维度")

        result = await self._structured(
            self.evaluator,
            [SystemMessage(content=prompts.EVALUATE), HumanMessage(content=user)],
            EvaluationOutput,
            validate=validate_evidence,
        )
        dimensions = sorted(
            result.data["dimensions"], key=lambda item: DIMENSIONS.index(item["name"])
        )
        assessed = sum(item["score"] is not None for item in dimensions)
        score = f"{sum(item['score'] for item in dimensions)}/20" if assessed == 4 else None
        return CallResult(
            {
                "score": score,
                "assessed_dimensions": assessed,
                "judge_model": self.judge_model,
                "judge_text": json.dumps({"dimensions": dimensions}, ensure_ascii=False, indent=2),
                "dimensions": dimensions,
            },
            result.tokens,
        )


class DemoBackend:
    """No network, no credentials, no real assessment. For interrupt/UI trials."""

    async def plan(self, options: dict) -> CallResult:
        focus = options.get("focus") or ["流程演示"]
        return CallResult(
            [
                {
                    "topic": f"{focus[i % len(focus)]} / 演示考点{i + 1}",
                    "direction": focus[i % len(focus)],
                    "points": ["描述一个具体做法", "说明失败时如何处理"],
                    "difficulty": 2,
                    "source": "demo",
                    "weak": False,
                    "evidence": "theoretical",
                }
                for i in range(options["num_questions"])
            ]
        )

    async def question(self, topic: dict, index: int, total: int) -> CallResult:
        return CallResult(
            {
                "topic": topic["topic"],
                "difficulty": 2,
                "text": f"【离线演示 {index}/{total}】围绕“{topic['topic']}”，你最近实践过的一个具体做法是什么？",
                "reference_points": ["3分：描述具体做法", "5分：说明失败处理与验证"],
                "probe_angles": ["失败处理", "验证方法", "替代方案"],
            }
        )

    async def coverage(self, question: dict) -> CallResult:
        return CallResult(
            {
                "score": 60,
                "answer_kind": "answer",
                "reason": "离线固定信号，不是真实快评",
                "covered": ["离线固定信号，不是真实快评"],
                "missed": [],
                "sufficient": False,
                "has_new_evidence": True,
                "next_target": "围绕已回答内容确认一个具体做法的依据（离线演示）",
            }
        )

    async def probe(self, question: dict, target: str, hop: int) -> CallResult:
        questions = [
            "如果这个做法失败了，你会先检查什么？",
            "你会用哪条测试验证刚才的判断？",
            "换一个业务场景，你会保留刚才做法的哪一部分？",
        ]
        return CallResult("【离线演示】" + questions[(hop - 1) % len(questions)])

    async def closing(self, question: dict, reason: str, struggle: bool) -> CallResult:
        return CallResult("【离线演示】本题问答结束，接下来汇总演示评分。")

    async def evaluate(self, question: dict) -> CallResult:
        dimensions = [
            {
                "name": name,
                "score": 3,
                "evidence": ["离线演示固定数据，不评价真实回答，也不代表能力水平。"],
            }
            for name in DIMENSIONS
        ]
        return CallResult(
            {
                "score": "12/20",
                "judge_model": "demo / 非真实评测",
                "judge_text": "这是离线演示固定评分，不代表你的能力。",
                "dimensions": dimensions,
            }
        )
