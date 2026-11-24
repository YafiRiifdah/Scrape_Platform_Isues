"""Adapter Instagram lewat layanan pihak ketiga Apify (berbayar, pay-per-result).

Tidak butuh akun Instagram. Dipakai karena Instagram menolak akses tanpa login dan menolak
login dari script (Instaloader) saat diuji November 2026.

Actor yang dipakai (apify.com/apify):
- `apify~instagram-scraper`         -> post per profil (`resultsType: posts`)
- `apify~instagram-comment-scraper` -> komentar per URL post, dibatch sekali untuk semua post

Balasan komentar tidak diambil (fitur berbayar terpisah); jumlah balasannya tetap tersimpan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from spi.models import Comment, Post
from spi.scrapers.apify_base import COMMENT_BATCH_SIZE, ApifyBase, parse_iso_utc
from spi.scrapers.base import AccountNotFound, ScraperError
from spi.scrapers.tiktok_common import to_int

log = logging.getLogger("spi.scrapers.instagram_apify")

POSTS_ACTOR = "apify~instagram-scraper"
COMMENTS_ACTOR = "apify~instagram-comment-scraper"

_MEDIA_TYPES = {"image": "image", "video": "video", "sidecar": "carousel"}
_NOT_FOUND_HINTS = ("not_found", "not found", "does not exist", "page not found")


def _non_negative(value: Any) -> Optional[int]:
    """Instagram mengirim -1 bila jumlah like disembunyikan."""
    n = to_int(value)
    return n if n is not None and n >= 0 else None


def map_post_item(item: dict[str, Any], account: str) -> Post:
    code = str(item.get("shortCode") or item.get("id"))
    kind = str(item.get("type") or "").lower()
    media_type = _MEDIA_TYPES.get(kind, "unknown")
    views = _non_negative(item.get("videoPlayCount")) or _non_negative(item.get("videoViewCount"))
    return Post(
        platform="instagram",
        account=account,
        post_id=code,
        url=item.get("url") or f"https://www.instagram.com/p/{code}/",
        created_at=parse_iso_utc(item.get("timestamp")) or datetime.now(timezone.utc),
        caption=item.get("caption") or "",
        hashtags=[str(h).lower() for h in item.get("hashtags") or [] if h],
        media_type=media_type,
        likes=_non_negative(item.get("likesCount")),
        comments_count=_non_negative(item.get("commentsCount")),
        shares=None,
        views=views if media_type == "video" else None,
    )


def map_comment_item(item: dict[str, Any], post_id: str) -> Comment:
    return Comment(
        platform="instagram",
        post_id=post_id,
        comment_id=str(item.get("id")),
        text=item.get("text") or "",
        created_at=parse_iso_utc(item.get("timestamp")),
        likes=_non_negative(item.get("likesCount")),
        is_reply=False,
        parent_comment_id=None,
        replies_count=_non_negative(item.get("repliesCount")),
    )


def _is_error_item(item: dict[str, Any]) -> bool:
    return "error" in item and not item.get("shortCode") and not item.get("text")


def _error_text(item: dict[str, Any]) -> str:
    return f"{item.get('error')}: {item.get('errorDescription') or ''}".strip(": ")


class InstagramApifyScraper(ApifyBase):
    platform = "instagram"

    def __init__(self, settings: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self._known_posts: dict[str, Post] = {}  # url -> Post, untuk batch komentar
        self._comments_cache: dict[str, list[Comment]] = {}
        self._comments_fetched = False

    # ---- posts ----------------------------------------------------------------
    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        self._token()
        payload: dict[str, Any] = {
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": "posts",
            "resultsLimit": max_posts,
            "addParentData": False,
        }
        if since is not None:
            # Filter tanggal di sisi Apify: hasil yang lebih tua tidak dikirim, jadi tidak ditagih.
            payload["onlyPostsNewerThan"] = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
        items = self._run_actor(POSTS_ACTOR, payload)

        posts: list[Post] = []
        for item in items:
            if _is_error_item(item):
                msg = f"instagram:{username}: {_error_text(item)}"
                if any(h in msg.lower() for h in _NOT_FOUND_HINTS):
                    raise AccountNotFound(msg)
                raise ScraperError(msg)
            post = map_post_item(item, username)
            if since is not None and post.created_at < since:
                continue  # misalnya post pinned yang lebih tua
            posts.append(post)
            if len(posts) >= max_posts:
                break
        for post in posts:
            self._known_posts[post.url] = post
        log.info("Apify instagram:%s: %d post dalam rentang (dari %d hasil)", username, len(posts), len(items))
        yield from posts

    # ---- comments -------------------------------------------------------------
    def _match_post(self, post_url: str) -> Optional[Post]:
        direct = self._known_posts.get(post_url)
        if direct is not None:
            return direct
        for post in self._known_posts.values():
            if post.post_id and f"/{post.post_id}" in post_url:
                return post
        return None

    def _fetch_comments_batch(self, urls: list[str], limit: int) -> None:
        for start in range(0, len(urls), COMMENT_BATCH_SIZE):
            chunk = urls[start : start + COMMENT_BATCH_SIZE]
            items = self._run_actor(
                COMMENTS_ACTOR,
                {"directUrls": chunk, "resultsLimit": limit, "includeNestedComments": False},
            )
            for item in items:
                if _is_error_item(item):
                    log.warning("Apify komentar gagal untuk %s: %s", item.get("postUrl") or item.get("url"), _error_text(item))
                    continue
                post = self._match_post(str(item.get("postUrl") or ""))
                if post is None:
                    continue
                self._comments_cache.setdefault(post.post_id, []).append(map_comment_item(item, post.post_id))

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]:
        if limit <= 0:
            return
        if post.url not in self._known_posts:
            self._known_posts[post.url] = post
        if not self._comments_fetched:
            self._comments_fetched = True
            self._fetch_comments_batch(list(self._known_posts), limit)
        elif post.post_id not in self._comments_cache:
            self._fetch_comments_batch([post.url], limit)
        comments = self._comments_cache.get(post.post_id, [])
        comments.sort(key=lambda c: c.likes or 0, reverse=True)
        yield from comments[:limit]

    def close(self) -> None:
        self._known_posts.clear()
        self._comments_cache.clear()
        self._comments_fetched = False
