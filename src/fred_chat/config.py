"""Server-side configuration. Secret values never appear in model representations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fred_api_key: SecretStr | None = None
    llm_api_key: SecretStr | None = None
    llm_provider: Literal["openai"] = "openai"
    llm_model: str = Field(default="gpt-4.1-mini", min_length=1, max_length=120)
    cache_ttl_seconds: int = Field(default=21_600, ge=0, le=604_800)
    demo_mode: bool = False
    cache_dir: Path = Path(".cache/fred")
    request_timeout_seconds: float = Field(default=15, gt=0, le=60)
    max_tool_calls: int = Field(default=6, ge=1, le=6)

    @field_validator("fred_api_key", "llm_api_key", mode="before")
    @classmethod
    def empty_secret_is_missing(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @property
    def configuration_messages(self) -> list[str]:
        messages = []
        if self.demo_mode:
            messages.append("DEMO: synthetic fixtures; no live economic observations.")
        elif not self.fred_api_key:
            messages.append("FRED_API_KEY is missing. Live economic data is unavailable; configure a key or explicitly enable DEMO_MODE.")
        if not self.llm_api_key:
            messages.append("LLM_API_KEY is missing. Deterministic tools and supported prompt shortcuts remain available; live LLM interpretation is unavailable.")
        return messages


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load only declared settings; never dump the process environment.

    A local .env is read without overriding environment variables. It is runtime
    configuration and must remain ignored by version control.
    """
    if env is None:
        try:
            from dotenv import load_dotenv
        except ImportError:
            pass
        else:
            load_dotenv(override=False)
        env = os.environ
    names = {
        "FRED_API_KEY": "fred_api_key",
        "LLM_API_KEY": "llm_api_key",
        "LLM_PROVIDER": "llm_provider",
        "LLM_MODEL": "llm_model",
        "CACHE_TTL_SECONDS": "cache_ttl_seconds",
        "DEMO_MODE": "demo_mode",
        "CACHE_DIR": "cache_dir",
        "REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
        "MAX_TOOL_CALLS": "max_tool_calls",
    }
    return Settings(**{field: env[key] for key, field in names.items() if key in env})
