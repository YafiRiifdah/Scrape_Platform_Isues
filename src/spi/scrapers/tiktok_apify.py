"""Adapter TikTok lewat layanan pihak ketiga Apify (berbayar, pay-per-result).

Actor yang dipakai (apify.com/clockworks):
- `clockworks~tiktok-scraper`          -> daftar video per profil
- `clockworks~tiktok-comments-scraper` -> komentar per URL video (dibatch sekali untuk semua post)

Bagian HTTP/token ada di `apify_base.py` dan dipakai bersama adapter Instagram.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from spi.models import Comment, Post
from spi.scrapers.apify_base import (  # noqa: F401 - diekspor ulang untuk kompatibilitas
    COMMENT_BATCH_SIZE,
    ApifyBase,
    RunActor,
    parse_iso_utc,
    raise_for_apify_status,
)
from spi.scrapers.base import AccountNotFound, ScraperError
from spi.scrapers.tiktok_common import epoch_utc, to_int

log = logging.getLogger("spi.scrapers.tiktok_apify")

POSTS_ACTOR = "clockworks~tiktok-scraper"
COMMENTS_ACTOR = "clockworks~tiktok-comments-scraper"


def _iso_or_epoch(item: dict[str, Any], iso_key: str, epoch_key: str) -> Optional[datetime]:
    return parse_iso_utc(item.get(iso_key)) or epoch_utc(item.get(epoch_key))


def _author_name(item: dict[str, Any], fallback: str) -> str:
    meta = item.get("authorMeta")
    if isinstance(meta, dict) and meta.get("name"):
        return str(meta["name"])
    return str(item.get("authorMeta.name") or fallback)


def map_post_item(item: dict[str, Any], account: str) -> Post:
    vid = str(item.get("id"))
    author = _author_name(item, account)
    hashtags = []
    for h in item.get("hashtags") or []:
        name = h.get("name") if isinstance(h, dict) else h
        if name:
            hashtags.append(str(name).lower())
    return Post(
        platform="tiktok",
        account=account,
        post_id=vid,
        url=item.get("webVideoUrl") or f"https://www.tiktok.com/@{author}/video/{vid}",
        created_at=_iso_or_epoch(item, "createTimeISO", "createTime") or datetime.now(timezone.utc),
        caption=item.get("text") or "",
        hashtags=hashtags,
        media_type="video",
        likes=to_int(item.get("diggCount")),
        comments_count=to_int(item.get("commentCount")),
        shares=to_int(item.get("shareCount")),
        views=to_int(item.get("playCount")),
    )


def map_comment_item(item: dict[str, Any], post_id: str) -> Comment:
    parent = item.get("repliesToId") or item.get("replyToId")
    return Comment(
        platform="tiktok",
        post_id=post_id,
        comment_id=str(item.get("cid")),
        text=item.get("text") or "",
        created_at=_iso_or_epoch(item, "createTimeISO", "createTime"),
        likes=to_int(item.get("diggCount")),
        is_reply=bool(parent),
        parent_comment_id=str(parent) if parent else None,
        replies_count=to_int(item.get("replyCommentTotal")),
    )


def _is_error_item(item: dict[str, Any]) -> bool:
    return bool(item.get("errorCode")) or ("error" in item and "id" not in item and "cid" not in item)


class TikTokApifyScraper(ApifyBase):
    platform = "tiktok"

    def __init__(self, settings: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self._known_posts: dict[str, Post] = {}  # url -> Post, untuk batch komentar
        self._comments_cache: dict[str, list[Comment]] = {}  # post_id -> komentar
        self._comments_fetched = False

    # ---- posts ----------------------------------------------------------------
    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        self._require_access()
        items = self._run_actor(
            POSTS_ACTOR,
            {
                "profiles": [username],
                "resultsPerPage": max_posts,
                "profileSorting": "latest",
                "excludePinnedPosts": False,
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
                "shouldDownloadSubtitles": False,
            },
        )
        # Kumpulkan semua post dulu (hasil Apify sudah lengkap di memori) supaya panggilan
        # fetch_comments pertama bisa membatch komentar untuk SEMUA post dalam satu run actor.
        posts: list[Post] = []
        for item in items:
            if _is_error_item(item):
                code = str(item.get("errorCode") or "")
                msg = f"tiktok:{username}: {item.get('error') or code}"
                if code in ("NOT_FOUND", "PROFILE_PRIVATE") or "not found" in msg.lower() or "private" in msg.lower():
                    raise AccountNotFound(msg)
                raise ScraperError(msg)
            post = map_post_item(item, username)
            if since is not None and post.created_at < since:
                continue  # termasuk post pinned yang lebih tua
            posts.append(post)
            if len(posts) >= max_posts:
                break
        for post in posts:
            self._known_posts[post.url] = post
        log.info("Apify tiktok:%s: %d post dalam rentang (dari %d hasil)", username, len(posts), len(items))
        yield from posts

    # ---- comments -------------------------------------------------------------
    def _fetch_comments_batch(self, urls: list[str], limit: int) -> None:
        for start in range(0, len(urls), COMMENT_BATCH_SIZE):
            chunk = urls[start : start + COMMENT_BATCH_SIZE]
            items = self._run_actor(
                COMMENTS_ACTOR,
                {"postURLs": chunk, "commentsPerPost": limit, "maxRepliesPerComment": 0},
            )
            for item in items:
                if _is_error_item(item):
                    log.warning("Apify komentar gagal untuk %s: %s", item.get("url"), item.get("error"))
                    continue
                video_url = str(item.get("videoWebUrl") or item.get("url") or "")
                post = self._known_posts.get(video_url) or self._match_by_id(video_url)
                if post is None:
                    continue
                self._comments_cache.setdefault(post.post_id, []).append(map_comment_item(item, post.post_id))

    def _match_by_id(self, video_url: str) -> Optional[Post]:
        for url, post in self._known_posts.items():
            if post.post_id and post.post_id in video_url:
                return post
        return None

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]:
        if limit <= 0:
            return
        if post.url not in self._known_posts:
            self._known_posts[post.url] = post
        if not self._comments_fetched:
            # Sekali jalan untuk semua post yang sudah diketahui: jauh lebih murah daripada satu run per post.
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
