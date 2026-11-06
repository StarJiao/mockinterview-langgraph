"""Checkpointed business facts. Clients, connections and Tasks never enter State."""

from typing import Annotated, TypedDict


def merge_by_key(old: dict, new: dict) -> dict:
    """Idempotent merge: replaying a saved result must not add tokens twice."""
    return {**old, **new}


class InterviewState(TypedDict):
    session_id: str
    options: dict
    plan: list[dict]
    questions: list[dict]
    decision: dict
    evaluations: Annotated[dict[str, dict], merge_by_key]
    usage: Annotated[dict[str, int], merge_by_key]
    status: str


MAX_PROBES = 3
MAX_CLARIFICATIONS = 1


def round_kind(question: dict, index: int) -> str:
    """Old checkpoints have no kind: their non-main rounds were all probes."""
    return question["rounds"][index].get("kind", "main" if index == 0 else "probe")


def count_rounds(question: dict, kind: str) -> int:
    return sum(round_kind(question, i) == kind for i in range(len(question.get("rounds", []))))


def round_label(question: dict, index: int, *, with_limit: bool = False) -> str:
    kind = round_kind(question, index)
    if kind == "main":
        return "主问"
    if kind == "clarification":
        return "题意澄清"
    hop = sum(round_kind(question, i) == "probe" for i in range(index + 1))
    return f"追问 {hop}" + (f"/{MAX_PROBES}" if with_limit else "")


def conclude(reason: str, code: str) -> dict:
    return {"action": "CONCLUDE", "reason": reason, "reason_code": code, "target": ""}


def budget_exceeded(state: dict) -> bool:
    limit = state["options"].get("token_budget", 200000)
    return limit > 0 and sum(state.get("usage", {}).values()) >= limit


def route_progress(state: dict) -> str:
    if len(state["questions"]) >= len(state["plan"]) or budget_exceeded(state):
        return "report"
    return "question"


def route_decision(state: dict) -> str:
    return "finish_question" if state["decision"]["action"] == "CONCLUDE" else "probe"


def decide_probe(coverage: dict, question: dict) -> dict:
    """The model supplies semantic evidence; code owns bounded interview policy."""
    intent = coverage["answer_kind"]
    if intent == "give_up":
        return conclude("候选人明确表示跳过或不会，结束本题", "give_up")
    if intent == "clarification":
        if count_rounds(question, "clarification") >= MAX_CLARIFICATIONS:
            return conclude("已补充一次题意，仍需澄清；本题评估受限", "clarification_limit")
        return {
            "action": "CLARIFY",
            "reason": "先回应候选人的题意澄清，不据此判断能力",
            "target": coverage["next_target"],
        }
    if count_rounds(question, "probe") >= MAX_PROBES:
        return conclude(f"正式追问已达 {MAX_PROBES} 次上限", "probe_limit")
    if coverage["sufficient"]:
        return conclude("本题已有足够评价证据，结束本题", "sufficient")

    score = max(0, min(100, int(coverage["score"])))
    # A supplement can itself receive a clarification request. Only count the
    # opportunity as used for stopping after a subsequent substantive answer.
    supplemented = any(
        rd.get("decision", {}).get("action") == "SUPPLEMENT"
        for rd in question.get("rounds", [])[:-1]
    )
    if score < 50 and supplemented and not coverage["has_new_evidence"]:
        return conclude("已给补充机会，仍缺少有效的新证据；按已呈现内容评估", "no_progress")
    supplement = score < 50 and not supplemented
    return {
        "action": "SUPPLEMENT" if supplement else "PROBE",
        "reason": f"覆盖分 {score}，"
        + ("先给一次针对性补充机会" if supplement else "继续收集评价证据"),
        "target": coverage["next_target"],
    }
