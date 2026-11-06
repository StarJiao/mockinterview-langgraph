"""Validate model outputs, not the mutable Graph State.

Provider JSON is untrusted. Semantic rules (fixed topic, exact plan count,
evidence clamp and four rubric dimensions) are applied in models.py.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DIMENSIONS = ("技术准确性", "机制深度", "表达结构", "迁移与场景应用")


class Topic(BaseModel):
    topic: str
    direction: str = ""
    points: list[str] = Field(default_factory=list)
    difficulty: int = 3
    source: str = "jd"
    weak: bool = False
    evidence: str = "theoretical"


class PlanOutput(BaseModel):
    topics: list[Topic]


class QuestionOutput(BaseModel):
    topic: str
    difficulty: int = 3
    text: str
    reference_points: list[str] = Field(default_factory=list)
    probe_angles: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("出题官返回空题目")
        return value.strip()


class CoverageOutput(BaseModel):
    # Intent describes the latest reply; coverage describes all submitted replies.
    answer_kind: Literal["answer", "clarification", "give_up"]
    reason: str = Field(min_length=1)
    score: int
    covered: list[str] = Field(default_factory=list)
    missed: list[str] = Field(default_factory=list)
    sufficient: bool
    has_new_evidence: bool
    next_target: str = Field(min_length=1)


class Assessment(BaseModel):
    kind: Literal["correct", "incorrect", "asked_not_expanded", "not_asked", "unclear_question"]
    round_index: int = Field(ge=1)
    candidate_quote: str
    explanation: str = Field(min_length=1)


class Dimension(BaseModel):
    name: str
    assessments: list[Assessment] = Field(min_length=1)
    score: int | None = Field(ge=1, le=5)
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def score_needs_observed_evidence(self):
        observed = {"correct", "incorrect", "asked_not_expanded"}
        if self.score is not None and not any(item.kind in observed for item in self.assessments):
            raise ValueError("只有未问到或题意不清的证据时，score 必须为 null（未评估）")
        return self


class EvaluationOutput(BaseModel):
    dimensions: list[Dimension]
