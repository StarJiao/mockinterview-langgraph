"""Review advice is derived from persisted scores, never from a new model call."""

from mockinterview.report import render_report


def dimension(name, score):
    return {
        "name": name,
        "score": score,
        "evidence": ["原文：回答"],
        "assessments": [
            {
                "kind": "correct",
                "round_index": 1,
                "candidate_quote": "回答",
                "explanation": "说明",
            }
        ],
    }


def make_state(evaluations):
    questions = []
    for index in (1, 2):
        questions.append(
            {
                "id": f"s-q{index}",
                "topic": f"考点{index}",
                "difficulty": 3,
                "status": "done",
                "text": "题面",
                "reference_points": [],
                "closing": "",
                "closing_reason": "证据充分",
                "rounds": [
                    {
                        "kind": "main",
                        "ask": "问题",
                        "answer": "回答",
                        "assessment": {
                            "score": 50,
                            "covered": [],
                            "missed": ["幂等设计"],
                            "sufficient": False,
                        },
                        "decision": {"reason": "继续收集证据"},
                    }
                ],
            }
        )
    return {
        "session_id": "s",
        "options": {},
        "questions": questions,
        "evaluations": evaluations,
        "plan": [
            {"topic": "考点1", "direction": "Go 并发"},
            {"topic": "考点2", "direction": "Redis"},
        ],
        "usage": {},
        "status": "done",
        "score_failures": {},
    }


def test_review_advice_names_the_weakest_question_and_keeps_evidence_boundary():
    weak = {"score": None, "judge_text": "", "dimensions": [
        dimension("技术准确性", 2), dimension("机制深度", 2),
        dimension("表达结构", 2), dimension("迁移与场景应用", 2),
    ]}
    strong = {"score": None, "judge_text": "", "dimensions": [
        dimension("技术准确性", 5), dimension("机制深度", 5),
        dimension("表达结构", 5), dimension("迁移与场景应用", 5),
    ]}
    text = render_report(make_state({"s-q1": weak, "s-q2": strong}))
    assert "## 后续复习建议" in text
    assert "**考点1**" in text
    assert "第 1 题四维均分 2.0/5" in text
    assert "最薄弱维度" in text
    assert "幂等设计" in text
    assert "未额外调用模型" in text


def test_review_advice_refuses_to_judge_without_saved_scores():
    text = render_report(make_state({}))
    assert "证据不足，不给薄弱方向判断" in text
    assert "优先复习方向" not in text


def test_unassessed_dimensions_are_not_counted_as_zero():
    partial = {"score": None, "judge_text": "", "dimensions": [
        dimension("技术准确性", 4), dimension("机制深度", None),
        dimension("表达结构", None), dimension("迁移与场景应用", None),
    ]}
    text = render_report(make_state({"s-q1": partial}))
    assert "4.0/5" in text
    assert "3 个维度因证据不足未评估" in text


def test_conclusion_sections_come_before_the_long_detail():
    text = render_report(make_state({}))
    positions = [
        text.index("# 模拟面试报告"),
        text.index("## 整场汇总"),
        text.index("## 后续复习建议"),
        text.index("## 逐题记录与评分"),
        text.index("### 1."),
    ]
    assert positions == sorted(positions)
    assert text.index("**候选人**") > text.index("## 逐题记录与评分")


def test_summary_still_reports_missing_scores_before_detail():
    text = render_report(make_state({"s-q1": {"score": "12/20", "judge_text": ""}}))
    assert "尚有 1 题未评分" in text
    assert text.index("尚有 1 题未评分") < text.index("## 逐题记录与评分")
