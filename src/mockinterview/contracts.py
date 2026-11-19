"""Small model boundary; runtime, CLI, and tests share no provider-specific state."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class CallResult:
    data: Any
    tokens: int = 0


class Backend(Protocol):
    async def plan(self, options: dict) -> CallResult: ...
    async def question(
        self, topic: dict, index: int, total: int, *, on_delta=None
    ) -> CallResult: ...
    async def coverage(self, question: dict) -> CallResult: ...
    async def probe(
        self, question: dict, target: str, hop: int, *, on_delta=None
    ) -> CallResult: ...
    async def closing(self, question: dict, reason: str, struggle: bool) -> CallResult: ...
    async def evaluate(self, question: dict) -> CallResult: ...
