"""Tahap 1: analisis per post dengan structured output (penyedia LLM bisa Claude atau OpenAI-compatible)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from spi.analysis import prompts
from spi.analysis.llm import LLMError, LLMResult, Provider
from spi.analysis.schemas import PostAnalysis
from spi.config import AnalysisConfig
from spi.models import Post
from spi.storage import Storage

log = logging.getLogger("spi.analysis.per_post")

PER_POST_MAX_TOKENS = 8192  # model berpikir ikut memakai jatah ini


def analyze_post(provider: Provider, cfg: AnalysisConfig, post: Post, comments: list) -> LLMResult:
    """Analisis satu post. `result.parsed` berisi PostAnalysis, atau `result.refusal` berisi alasan gagal."""
    return provider.structured(
        model=cfg.per_post_model,
        system=prompts.SYSTEM_PER_POST,
        user=prompts.build_post_user_message(post, comments),
        schema=PostAnalysis,
        max_tokens=PER_POST_MAX_TOKENS,
    )


def analyze_pending(
    db: Storage,
    cfg: AnalysisConfig,
    provider: Provider,
    since: datetime,
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
            result = analyze_post(provider, cfg, post, comments)
        except LLMError as e:
            result = LLMResult(model=cfg.per_post_model, refusal=str(e))
        stats["tokens_in"] += result.tokens_in
        stats["tokens_out"] += result.tokens_out
        if result.parsed is None:
            stats["failed"] += 1
            stats["errors"].append(f"{post.platform}:{post.post_id}: {result.refusal}")
            log.error("analisis %s:%s gagal: %s", post.platform, post.post_id, result.refusal)
            continue
        db.save_analysis(
            post.platform,
            post.post_id,
            model=result.model or cfg.per_post_model,
            analysis=result.parsed.model_dump(),
            input_hash=h,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )
        stats["analyzed"] += 1
    return stats
