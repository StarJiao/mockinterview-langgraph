"""Local configuration: dotenv is read explicitly, never copied into checkpoints."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
import os

from dotenv import dotenv_values


@dataclass(frozen=True)
class Config:
    questioner_model: str = "deepseek-chat"
    questioner_api_key: str = field(default="", repr=False)
    questioner_base_url: str = "https://api.deepseek.com"
    evaluator_model: str = "qwen-plus"
    evaluator_api_key: str = field(default="", repr=False)
    evaluator_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    timeout: float = 300
    token_budget: int = 200_000

    @classmethod
    def load(cls, path: str | Path = ".env", environ: Mapping[str, str] | None = None) -> "Config":
        # Do not mutate os.environ: another session may use a different env file.
        values = {key: value for key, value in dotenv_values(path).items() if value is not None}
        values.update(os.environ if environ is None else environ)

        def get(*keys: str, default: str = "") -> str:
            return next((values[key] for key in keys if values.get(key)), default)

        config = cls(
            questioner_model=get("QUESTIONER_MODEL", default=cls.questioner_model),
            questioner_api_key=get("QUESTIONER_API_KEY", "DEEPSEEK_API_KEY"),
            questioner_base_url=get(
                "QUESTIONER_BASE_URL", "DEEPSEEK_BASE_URL", default=cls.questioner_base_url
            ),
            evaluator_model=get("EVALUATOR_MODEL", default=cls.evaluator_model),
            evaluator_api_key=get("EVALUATOR_API_KEY", "DASHSCOPE_API_KEY"),
            evaluator_base_url=get(
                "EVALUATOR_BASE_URL", "DASHSCOPE_BASE_URL", default=cls.evaluator_base_url
            ),
            timeout=float(get("MODEL_TIMEOUT", default="300")),
            token_budget=int(get("SESSION_TOKEN_BUDGET", default="200000")),
        )
        if config.timeout <= 0 or config.token_budget <= 0:
            raise ValueError("MODEL_TIMEOUT 和 SESSION_TOKEN_BUDGET 必须大于 0")
        return config
