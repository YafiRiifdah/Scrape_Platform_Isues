"""Konfigurasi aplikasi: daftar akun target dan parameter scraping/analisis."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from spi.models import Platform

DEFAULT_ADAPTERS: dict[str, str] = {
    "instagram": "instaloader",
    "tiktok": "tiktok_apify",  # library/scraping langsung diblokir TikTok saat diuji Okt 2026
}

DelayRange = tuple[float, float]


def _check_range(value: DelayRange) -> DelayRange:
    low, high = value
    if low < 0 or high < low:
        raise ValueError("rentang jeda harus [min, max] dengan min >= 0 dan max >= min")
    return (float(low), float(high))


class AccountConfig(BaseModel):
    platform: Platform
    username: str
    label: str
    adapter: Optional[str] = None

    @model_validator(mode="after")
    def _default_adapter(self) -> "AccountConfig":
        if self.adapter is None:
            self.adapter = DEFAULT_ADAPTERS[self.platform]
        return self

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.username}"


class ScrapeConfig(BaseModel):
    days: int = 7
    max_comments_per_post: int = 200
    max_posts_per_account: int = 100
    delay_between_requests: DelayRange = (2.0, 6.0)
    delay_between_accounts: DelayRange = (30.0, 90.0)
    older_posts_tolerance: int = 3
    rate_limit_sleep_minutes: float = 15
    captcha_wait_seconds: float = 180

    _v1 = field_validator("delay_between_requests")(_check_range)
    _v2 = field_validator("delay_between_accounts")(_check_range)


class AnalysisConfig(BaseModel):
    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    base_url: Optional[str] = None  # hanya untuk openai_compatible, mis. Gemini/Groq/OpenRouter/Ollama
    api_key_env: str = "LLM_API_KEY"  # nama variabel .env yang menyimpan key penyedia openai_compatible
    request_delay_seconds: float = 0.0  # jeda antar panggilan, berguna untuk tier gratis
    per_post_model: str = "claude-haiku-5-5"
    report_model: str = "claude-opus-5-5"
    max_comments_to_llm: int = 200
    top_replied_to_llm: int = 10  # komentar paling banyak dibalas yang dijamin ikut ke LLM
    language: str = "id"
    server_side_fallback: bool = True


class StorageConfig(BaseModel):
    db_path: str = "data/spi.sqlite"
    reports_dir: str = "reports"
    ig_session_file: str = "data/ig_session"
    browser_profile_dir: str = "data/browser_profile"


class AppConfig(BaseModel):
    accounts: list[AccountConfig] = Field(min_length=1)
    scrape: ScrapeConfig = Field(default_factory=ScrapeConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


def load_config(path: str | Path) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)
