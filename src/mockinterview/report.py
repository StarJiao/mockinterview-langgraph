"""Reports contain persisted interview facts, never CLI system notifications.

Overall numbers and revision advice are rendered before the long per-question
detail, so the conclusion is visible without scrolling through every round.
"""

import json

from .state import round_label


def _paragraph(value: object) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _evaluation_dimensions(evaluation: dict) -> list[dict]:
    dimensions = evaluation.get("dimensions")
    if isinstance(dimensions, list) and dimensions:
        return [item for item in dimensions if isinstance(item, dict)]
    detail = evaluation.get("judge_text")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (ValueError, TypeError):
            return []
    if isinstance(detail, dict) and isinstance(detail.get("dimensions"), list):
        return [item for item in detail["dimensions"] if isinstance(item, dict)]
    return []


def render_overall_summary(state: dict) -> list[str]:
    """Whole-session numbers; derived from saved facts, no new model call."""
    questions = state.get("questions", [])
    evaluations = state.get("evaluations", {})
    done = sum(q.get("status") == "done" for q in questions)
    lines = [
        "## 整场汇总",
        "",
        f"已完成 {done} 题，已收集 {len(evaluations)} 份单题评分。",
    ]
    missing = sum(
        q.get("status") == "done" and q["id"] not in evaluations for q in questions
    )
    if missing:
        lines.append(f"尚有 {missing} 题未评分，可使用 score 命令补评。")
    lines.extend(
        [
            "以上为单题评分汇总，未额外调用模型生成整场评价。",
            "",
        ]
    )
    return lines


def render_review_advice(state: dict) -> list[str]:
    """Derive a revision priority from saved scores; never invent a judgement."""
    questions = state.get("questions", [])
    evaluations = state.get("evaluations", {})
    plan = state.get("plan", [])
    by_dimension: dict[str, list[float]] = {}
    per_question = []
    unassessed = 0
    for index, question in enumerate(questions, 1):
        evaluation = evaluations.get(question.get("id"))
        if not isinstance(evaluation, dict):
            continue
        values = []
        for dimension in _evaluation_dimensions(evaluation):
            value = dimension.get("score")
            if value is None:
                unassessed += 1
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            name = str(dimension.get("name") or "未命名维度")
            by_dimension.setdefault(name, []).append(numeric)
            values.append(numeric)
        if values:
            per_question.append((sum(values) / len(values), index, question))

    lines = ["## 后续复习建议（由已保存评分派生）", ""]
    if not per_question:
        lines.extend(
            [
                "本场没有可用的已保存评分，证据不足，不给薄弱方向判断；可先补评或新建面试。",
                "",
            ]
        )
        return lines

    ranked = sorted(
        (sum(values) / len(values), name, len(values))
        for name, values in by_dimension.items()
    )
    dimension_score, dimension_name, dimension_count = ranked[0]
    average, index, question = min(per_question, key=lambda item: item[0])
    topic = str(question.get("topic") or "未记录主题")
    direction = ""
    if index - 1 < len(plan):
        direction = str(plan[index - 1].get("direction") or "")
    suffix = f"（{direction}）" if direction and direction != topic else ""
    lines.extend(
        [
            f"- 优先复习方向：**{topic}**{suffix}，第 {index} 题四维均分 {average:.1f}/5。",
            f"- 最薄弱维度：**{dimension_name}**，已评估 {dimension_count} 题的均分 "
            f"{dimension_score:.1f}/5。",
        ]
    )
    gaps = []
    for question in questions:
        for round_ in question.get("rounds", []):
            assessment = round_.get("assessment")
            if isinstance(assessment, dict):
                for missed in assessment.get("missed", []) or []:
                    text = str(missed).strip()
                    if text and text not in gaps:
                        gaps.append(text)
    if gaps:
        lines.append("- 快评中记录过的缺口：" + "；".join(gaps[:3]) + "。")
    lines.extend(
        [
            f"- 证据边界：共 {len(per_question)} 题有数字评分，{unassessed} 个维度因证据不足"
            "未评估；未评估维度不按零分计入。",
            "- 本节只汇总本场已落盘的四维分数与快评记录，未额外调用模型生成整体评价，"
            "也不代表跨场能力结论。",
            "",
        ]
    )
    return lines


def render_report(state: dict) -> str:
    options = state.get("options", {})
    questions = state.get("questions", [])
    evaluations = state.get("evaluations", {})
    failures = state.get("score_failures", {})
    usage = sum(state.get("usage", {}).values())
    lines = [
        f"# 模拟面试报告 · {state.get('session_id', '')}",
        "",
    ]
    if options.get("demo"):
        lines.extend(
            ["> 离线演示：所有题目与评分为固定测试数据，不代表真实能力。", ""]
        )
    lines.extend(
        [
            f"- 方向：{' / '.join(options.get('focus', [])) or '综合'}",
            f"- 提示词版本：{options.get('prompt_version', 'original')}"
            + (" · 预定题目" if options.get("preset_questions") else ""),
            f"- 状态：{state.get('status', 'unknown')} · 题数：{len(questions)}",
            f"- Token：{usage} / {options.get('token_budget', '未设置')}",
            "",
        ]
    )
    # Conclusion first: the per-question detail below can be very long.
    lines.extend(render_overall_summary(state))
    lines.extend(render_review_advice(state))
    lines.append("## 逐题记录与评分")
    for index, question in enumerate(questions, 1):
        lines.extend(
            [
                "",
                f"### {index}. {question.get('topic', '')}（难度 {question.get('difficulty', '—')}）",
                "",
                f"题目 ID：`{question['id']}` · 状态：{question.get('status', '')}",
                "",
            ]
        )
        for point in question.get("reference_points", []):
            lines.append(f"> 考点：{_paragraph(point)}")
        lines.append("")
        rounds = question.get("rounds", [])
        for index, round_ in enumerate(rounds):
            label = round_label(question, index)
            lines.extend([f"**面试官 · {label}**", "", _paragraph(round_["ask"]), ""])
            answer = round_.get("answer")
            answer_text = "（未提交回答）" if answer is None else answer or "（已提交空回答）"
            lines.extend(["**候选人**", "", _paragraph(answer_text), ""])
            decision = round_.get("decision")
            if decision:
                reason = decision.get("reason", "") if isinstance(decision, dict) else decision
                lines.extend([f"快评决策：{_paragraph(reason)}", ""])
            assessment = round_.get("assessment")
            if isinstance(assessment, dict):
                lines.extend(
                    [
                        f"快评覆盖分：{assessment.get('score', '—')}/100 · "
                        f"证据充分：{'是' if assessment.get('sufficient') else '否'}",
                        f"已确认：{'；'.join(map(_paragraph, assessment.get('covered', []))) or '无'}",
                        f"尚缺：{'；'.join(map(_paragraph, assessment.get('missed', []))) or '无'}",
                        f"下一轮目标：{_paragraph(assessment.get('next_target', '')) or '无'}",
                        "",
                    ]
                )
        if question.get("closing"):
            lines.extend(["**面试官 · 收尾**", "", _paragraph(question["closing"]), ""])
        evaluation = evaluations.get(question["id"])
        if not evaluation:
            error = failures.get(question["id"])
            lines.extend([f"（评分失败：{error}；可单独补评）" if error else "（尚未评分）", ""])
            continue
        score = evaluation.get("score")
        if score is None:
            assessed = evaluation.get("assessed_dimensions", 0)
            score = f"未计算总分（已评估 {assessed}/4 个维度，其他维度证据不足）"
        lines.extend(
            [
                f"**评分：{score}** · {evaluation.get('judge_model', '')}",
                "",
            ]
        )
        detail = evaluation.get("judge_text", "")
        # The model boundary may return serialized structured judge feedback, as the Go version did.
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (ValueError, TypeError):
                pass
        if isinstance(detail, dict):
            for dimension in detail.get("dimensions", []):
                evidence = dimension.get("evidence", [])
                if isinstance(evidence, list):
                    evidence = "；".join(str(item) for item in evidence)
                value = dimension.get("score")
                label = "未评估（证据不足）" if value is None else f"{value}/5"
                lines.extend(
                    [
                        f"- {dimension.get('name', '')}：{label}",
                        f"  - 证据：{_paragraph(evidence)}",
                    ]
                )
                categories = {
                    "correct": "明确正确",
                    "incorrect": "明确错误",
                    "asked_not_expanded": "已问但未展开",
                    "not_asked": "未被问到",
                    "unclear_question": "题意不清或提问带偏",
                }
                for item in dimension.get("assessments", []):
                    category = categories.get(item.get("kind"), item.get("kind", "未知"))
                    lines.append(
                        f"  - 依据分类：{category} · 回合 {item.get('round_index', '—')} · "
                        f"候选人原文：{_paragraph(item.get('candidate_quote') or '无可引用内容')}；"
                        f"说明：{_paragraph(item.get('explanation', ''))}"
                    )
            for key, label in [
                ("strengths", "优点"),
                ("weaknesses", "不足"),
                ("suggestions", "建议"),
                ("summary", "总结"),
            ]:
                content = detail.get(key)
                if content:
                    content = "；".join(map(str, content)) if isinstance(content, list) else content
                    lines.extend(["", f"{label}：{_paragraph(content)}"])
            if not detail.get("dimensions"):
                lines.extend(["```json", json.dumps(detail, ensure_ascii=False, indent=2), "```"])
        else:
            lines.append(_paragraph(detail))
    return "\n".join(lines)
