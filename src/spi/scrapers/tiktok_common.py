"""Pemetaan JSON TikTok -> model, dan deteksi browser sistem. Dipakai adapter A dan B."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from spi.models import Comment, Post

SYSTEM_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def detect_system_browser() -> Optional[str]:
    """Cari Edge/Chrome terpasang. Chromium bawaan Playwright kadang diblokir Windows
    (gagal 'spawn UNKNOWN' saat berjendela), sedangkan browser sistem lolos."""
    for candidate in SYSTEM_BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def browser_executable(env: dict[str, str]) -> Optional[str]:
    return env.get("SPI_BROWSER_PATH") or detect_system_browser()


def to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def epoch_utc(value: Any) -> Optional[datetime]:
    ts = to_int(value)
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def map_item(d: dict[str, Any], account: str) -> Post:
    """Petakan satu item video TikTok (dict dari itemList / as_dict) ke Post. Semua akses pakai .get()."""
    stats = d.get("statsV2") or d.get("stats") or {}
    vid = str(d.get("id"))
    created = epoch_utc(d.get("createTime")) or datetime.now(timezone.utc)
    author = d.get("author")
    username = author.get("uniqueId") if isinstance(author, dict) and author.get("uniqueId") else account
    return Post(
        platform="tiktok",
        account=account,
        post_id=vid,
        url=f"https://www.tiktok.com/@{username}/video/{vid}",
        created_at=created,
        caption=d.get("desc") or "",
        hashtags=[str(c.get("title", "")).lower() for c in d.get("challenges") or [] if c.get("title")],
        media_type="video",
        likes=to_int(stats.get("diggCount")),
        comments_count=to_int(stats.get("commentCount")),
        shares=to_int(stats.get("shareCount")),
        views=to_int(stats.get("playCount")),
    )


def map_comment_dict(d: dict[str, Any], post_id: str) -> Comment:
    reply_id = str(d.get("reply_id") or "0")
    is_reply = reply_id not in ("0", "")
    return Comment(
        platform="tiktok",
        post_id=post_id,
        comment_id=str(d.get("cid")),
        text=d.get("text") or "",
        created_at=epoch_utc(d.get("create_time")),
        likes=to_int(d.get("digg_count")),
        is_reply=is_reply,
        parent_comment_id=reply_id if is_reply else None,
        replies_count=to_int(d.get("reply_comment_total")),
    )
