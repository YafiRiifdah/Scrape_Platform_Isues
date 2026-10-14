"""Tahap 1: analisis per post dengan Claude Haiku 5.5 dan structured output."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

import anthropic

from spi.analysis import prompts
from spi.analysis.schemas import PostAnalysis
from spi.config import AnalysisConfig
from spi.models import Post
from spi.storage import Storage

log = logging.getLogger("spi.analysis.per_post")

MAX_RATE_LIMIT_RETRIES = 5


def _system_blocks() -> list[dict[str, Any]]:
    return [{"type": "text", "text": prompts.SYSTEM_PER_POST, "cache_control": {"type": "ephemeral"}}]


def _call_with_retry(client: Any, sleep: Callable[[float], None], **kwargs) -> Any:
    """Panggil messages.parse; 429 dan 5xx dicoba ulang, 4xx lain langsung dilempar."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return client.messages.parse(**kwargs)
        except anthropic.RateLimitError as e:
            wait = float(e.response.headers.get("retry-after", "30") or 30)
            log.warning("rate limit Claude, menunggu %.0f detik", wait)
            sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < MAX_RATE_LIMIT_RETRIES:
                log.warning("server error %s, coba lagi", e.status_code)
                sleep(5.0)
                continue
            raise
    raise RuntimeError("gagal setelah beberapa kali rate limit")


def analyze_post(client: Any, cfg: AnalysisConfig, post: Post, comments: list, sleep: Callable[[float], None]) -> tuple[Optional[PostAnalysis], Any, Optional[str]]:
    """Mengembalikan (analysis | None, usage, error | None)."""
    response = _call_with_retry(
        client,
        sleep,
        model=cfg.per_post_model,
        max_tokens=2048,
        output_config={"effort": "low"},
        system=_system_blocks(),
        messages=[{"role": "user", "content": prompts.build_post_user_message(post, comments)}],
        output_format=PostAnalysis,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        detail = getattr(getattr(response, "stop_details", None), "category", None)
        return None, response.usage, f"refusal ({detail})" if response.stop_reason == "refusal" else "output kosong"
    return response.parsed_output, response.usage, None


def analyze_pending(
    db: Storage,
    cfg: AnalysisConfig,
    client: Any,
    since: datetime,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int, Post], None]] = None,
) -> dict[str, Any]:
    """Analisis semua post sejak `since` yang belum dianalisis atau datanya berubah."""
    stats: dict[str, Any] = {"analyzed": 0, "skipped": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0, "errors": []}
    posts = db.get_posts_since(since)
    for idx, post in enumerate(posts, start=1):
        comments = db.get_comments_for_llm(post.platform, post.post_id, cfg.max_comments_to_llm, top_replied=cfg.top_replied_to_llm)
        h = prompts.input_hash(post, comments)
        existing = db.get_analysis(post.platform, post.post_id)
        if existing and existing["input_hash"] == h:
            stats["skipped"] += 1
            continue
        if progress:
            progress(idx, len(posts), post)
        try:
            analysis, usage, error = analyze_post(client, cfg, post, comments, sleep)
        except anthropic.APIConnectionError as e:
            analysis, usage, error = None, None, f"koneksi: {e}"
        except anthropic.APIStatusError as e:
            analysis, usage, error = None, None, f"API {e.status_code}: {e.message}"
        if usage is not None:
            stats["tokens_in"] += usage.input_tokens or 0
            stats["tokens_out"] += usage.output_tokens or 0
        if analysis is None:
            stats["failed"] += 1
            stats["errors"].append(f"{post.platform}:{post.post_id}: {error}")
            log.error("analisis %s:%s gagal: %s", post.platform, post.post_id, error)
            continue
        db.save_analysis(
            post.platform,
            post.post_id,
            model=cfg.per_post_model,
            analysis=analysis.model_dump(),
            input_hash=h,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )
        stats["analyzed"] += 1
    return stats
