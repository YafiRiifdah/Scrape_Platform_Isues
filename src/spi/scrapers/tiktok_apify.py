"""Adapter TikTok lewat layanan pihak ketiga Apify (berbayar, pay-per-result).

Actor yang dipakai (apify.com/clockworks):
- `clockworks~tiktok-scraper`          -> daftar video per profil
- `clockworks~tiktok-comments-scraper` -> komentar per URL video (dibatch sekali untuk semua post)

Butuh `APIFY_TOKEN` di .env (Apify Console -> Settings -> Integrations -> API token).
Memakai endpoint sinkron `run-sync-get-dataset-items`, jadi satu panggilan HTTP = satu run actor.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

import requests

from spi.models import Comment, Post
from spi.scrapers.base import AccountNotFound, RateLimited, ScrapeBlocked, ScraperError
from spi.scrapers.tiktok_common import epoch_utc, to_int

log = logging.getLogger("spi.scrapers.tiktok_apify")

APIFY_BASE = "https://api.apify.com/v2"
POSTS_ACTOR = "clockworks~tiktok-scraper"
COMMENTS_ACTOR = "clockworks~tiktok-comments-scraper"
SYNC_TIMEOUT_SECONDS = 300
TOKEN_HINT = "Isi APIFY_TOKEN di .env (Apify Console -> Settings -> Integrations -> Personal API tokens)."
COMMENT_BATCH_SIZE = 50

RunActor = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


def raise_for_apify_status(status: int, body: str) -> None:
    if status < 400:
        return
    if status == 429:
        raise RateLimited(f"Apify membalas 429 (batas laju): {body[:200]}")
    if status in (401, 403):
        raise ScrapeBlocked(f"Apify menolak token (HTTP {status}). {TOKEN_HINT} Detail: {body[:200]}")
    if status == 402:
        raise ScrapeBlocked(f"Kredit Apify habis (HTTP 402). Isi saldo atau naikkan paket. Detail: {body[:200]}")
    if status == 408:
        raise ScraperError(
            "Run Apify melewati batas waktu sinkron 300 detik. Kecilkan max_posts_per_account atau "
            "max_comments_per_post lalu coba lagi."
        )
    raise ScraperError(f"Apify membalas HTTP {status}: {body[:300]}")


def _iso_or_epoch(item: dict[str, Any], iso_key: str, epoch_key: str) -> Optional[datetime]:
    iso = item.get(iso_key)
    if iso:
        try:
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return epoch_utc(item.get(epoch_key))


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
    )


def _is_error_item(item: dict[str, Any]) -> bool:
    return bool(item.get("errorCode")) or ("error" in item and "id" not in item and "cid" not in item)


class TikTokApifyScraper:
    platform = "tiktok"

    def __init__(self, settings: dict[str, Any], run_actor: Optional[RunActor] = None) -> None:
        self.settings = settings
        self._run_actor = run_actor or self._run_actor_http
        self._known_posts: dict[str, Post] = {}  # url -> Post, untuk batch komentar
        self._comments_cache: dict[str, list[Comment]] = {}  # post_id -> komentar
        self._comments_fetched = False

    # ---- HTTP ---------------------------------------------------------------
    def _token(self) -> str:
        token = self.settings.get("env", {}).get("APIFY_TOKEN")
        if not token:
            raise ScrapeBlocked("APIFY_TOKEN belum diisi. " + TOKEN_HINT)
        return token

    def _run_actor_http(self, actor: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items"
        params = {"token": self._token(), "timeout": SYNC_TIMEOUT_SECONDS, "format": "json", "clean": "true"}
        log.info("Apify %s: menjalankan actor (%s)", actor, ", ".join(f"{k}={v!r}" for k, v in payload.items() if k != "postURLs"))
        try:
            resp = requests.post(url, params=params, json=payload, timeout=SYNC_TIMEOUT_SECONDS + 30)
        except requests.RequestException as e:
            raise ScraperError(f"koneksi ke Apify gagal: {e}") from e
        raise_for_apify_status(resp.status_code, resp.text)
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ScraperError(f"respons Apify bukan JSON: {resp.text[:200]}") from e
        return data if isinstance(data, list) else []

    # ---- posts ----------------------------------------------------------------
    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        self._token()
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
        yielded = 0
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
            self._known_posts[post.url] = post
            yield post
            yielded += 1
            if yielded >= max_posts:
                break

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
