"""Antarmuka bersama semua adapter scraper dan exception yang dipahami runner."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

from spi.models import Comment, Post


class ScraperError(Exception):
    """Dasar semua error scraper."""


class RateLimited(ScraperError):
    """Platform membatasi laju request; runner menunggu lalu mencoba lagi."""


class ScrapeBlocked(ScraperError):
    """Login wall, challenge, atau respons kosong; akun ditandai gagal."""


class AccountNotFound(ScraperError):
    """Username tidak ada atau privat."""


@runtime_checkable
class BaseScraper(Protocol):
    platform: str

    def __init__(self, settings: dict[str, Any]) -> None: ...

    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]: ...

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]: ...

    def close(self) -> None: ...


def should_stop(created_at: datetime, since: Optional[datetime], older_seen: int, tolerance: int) -> tuple[bool, int]:
    """Aturan berhenti bersama: toleransi beberapa post pinned yang lebih tua dari `since`.

    Mengembalikan (berhenti?, jumlah post lebih tua yang sudah terlihat berturut-turut).
    """
    if since is None or created_at >= since:
        return False, 0
    older_seen += 1
    return older_seen > tolerance, older_seen
