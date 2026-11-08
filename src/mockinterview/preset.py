"""Validated, fixed interview history for controlled prompt comparisons."""

import json
from pathlib import Path

from .state import MAX_CLARIFICATIONS, MAX_PROBES


def load_preset(path: str | Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"预定问题文件不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict) or set(data) != {"questions"}:
        raise ValueError("预定问题文件顶层只能包含 questions 数组")
    questions = data["questions"]
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions 必须是非空数组")
    for qi, q in enumerate(questions, 1):
        if not isinstance(q, dict) or set(q) != {"topic", "difficulty", "reference_points", "rounds"}:
            raise ValueError(f"第 {qi} 题需要 topic、difficulty、reference_points、rounds")
        if not isinstance(q["topic"], str) or not q["topic"].strip():
            raise ValueError(f"第 {qi} 题 topic 不能为空")
        if type(q["difficulty"]) is not int or not 1 <= q["difficulty"] <= 5:
            raise ValueError(f"第 {qi} 题 difficulty 必须为 1–5")
        points = q["reference_points"]
        if not isinstance(points, list) or not points or any(not isinstance(p, str) or not p.strip() for p in points):
            raise ValueError(f"第 {qi} 题 reference_points 必须是非空字符串数组")
        rounds = q["rounds"]
        if not isinstance(rounds, list) or not rounds:
            raise ValueError(f"第 {qi} 题 rounds 必须是非空数组")
        probes = clarifications = 0
        for ri, rd in enumerate(rounds, 1):
            if not isinstance(rd, dict) or not {"kind", "ask"} <= set(rd) or set(rd) - {"kind", "ask", "answer"}:
                raise ValueError(f"第 {qi} 题第 {ri} 轮只接受 kind、ask、可选 answer")
            kind = rd["kind"]
            if kind not in {"main", "probe", "clarification"} or (kind == "main") != (ri == 1):
                raise ValueError(f"第 {qi} 题第 {ri} 轮 kind 不符合主问/追问顺序")
            if not isinstance(rd["ask"], str) or not rd["ask"].strip():
                raise ValueError(f"第 {qi} 题第 {ri} 轮 ask 不能为空")
            if "answer" in rd and not isinstance(rd["answer"], str):
                raise ValueError(f"第 {qi} 题第 {ri} 轮 answer 必须是字符串")
            if ri < len(rounds) and "answer" not in rd:
                raise ValueError(f"第 {qi} 题只有最后一轮可以不写 answer")
            probes += kind == "probe"
            clarifications += kind == "clarification"
        if probes > MAX_PROBES or clarifications > MAX_CLARIFICATIONS:
            raise ValueError(f"第 {qi} 题超出追问或澄清次数上限")
    return questions
