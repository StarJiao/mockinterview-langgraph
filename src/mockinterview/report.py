"""Reports contain persisted interview facts, never CLI system notifications."""

import json

from .state import round_label


def _paragraph(value: object) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def render_report(state: dict) -> str:
    options = state.get("options", {})
    questions = state.get("questions", [])
    evaluations = state.get("evaluations", {})
    usage = sum(state.get("usage", {}).values())
    lines = [
        f"# 模拟面试报告 · {state.get('session_id', '')}",
        "",
        f"- 方向：{' / '.join(options.get('focus', [])) or '综合'}",
        f"- 状态：{state.get('status', 'unknown')} · 题数：{len(questions)}",
        f"- Token：{usage} / {options.get('token_budget', '未设置')}",
        "",
        "## 逐题记录与评分",
    ]
    if options.get("demo"):
        lines[1:1] = ["", "> 离线演示：所有题目与评分为固定测试数据，不代表真实能力。"]
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
            lines.extend(["**应试人**", "", _paragraph(answer_text), ""])
            decision = round_.get("decision")
            if decision:
                reason = decision.get("reason", "") if isinstance(decision, dict) else decision
                lines.extend([f"快评决策：{_paragraph(reason)}", ""])
        if question.get("closing"):
            lines.extend(["**面试官 · 收尾**", "", _paragraph(question["closing"]), ""])
        evaluation = evaluations.get(question["id"])
        if not evaluation:
            lines.extend(["（尚未评分）", ""])
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
    lines.extend(
        [
            "",
            "## 整场汇总",
            "",
            f"已完成 {sum(q.get('status') == 'done' for q in questions)} 题，"
            f"已收集 {len(evaluations)} 份单题评分。",
            "以上为单题评分汇总，未额外调用模型生成整场评价。",
            "",
        ]
    )
    return "\n".join(lines)
