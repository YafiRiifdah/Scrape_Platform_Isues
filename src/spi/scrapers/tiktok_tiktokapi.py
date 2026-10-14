"""Adapter TikTok berbasis library TikTokApi (pendekatan A).

Tidak perlu login; butuh cookie `msToken` dari tiktok.com (env TIKTOK_MS_TOKEN).
TikTokApi bersifat async; adapter ini membungkusnya menjadi sinkron dengan event loop
khusus agar runner tetap sederhana dan sama untuk kedua platform.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from TikTokApi import TikTokApi
from TikTokApi import exceptions as ttx

from spi.models import Comment, Post
from spi.scrapers.base import AccountNotFound, ScrapeBlocked, should_stop

log = logging.getLogger("spi.scrapers.tiktok")

RETRY_DELAYS = (10, 30, 60)
MS_TOKEN_HINT = (
    "Perbarui TIKTOK_MS_TOKEN di .env: buka tiktok.com di browser, DevTools (F12) -> "
    "Application -> Cookies -> msToken. Jalankan dari IP rumahan (bukan VPN/datacenter)."
)


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _epoch_utc(value: Any) -> Optional[datetime]:
    ts = _int(value)
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def map_video(video: Any, account: str) -> Post:
    """Petakan TikTokApi Video (lewat `as_dict`) ke model Post. Semua akses pakai .get()."""
    d: dict[str, Any] = video.as_dict or {}
    stats = d.get("statsV2") or d.get("stats") or {}
    vid = str(d.get("id") or video.id)
    created = _epoch_utc(d.get("createTime")) or datetime.now(timezone.utc)
    return Post(
        platform="tiktok",
        account=account,
        post_id=vid,
        url=f"https://www.tiktok.com/@{account}/video/{vid}",
        created_at=created,
        caption=d.get("desc") or "",
        hashtags=[str(c.get("title", "")).lower() for c in d.get("challenges") or [] if c.get("title")],
        media_type="video",
        likes=_int(stats.get("diggCount")),
        comments_count=_int(stats.get("commentCount")),
        shares=_int(stats.get("shareCount")),
        views=_int(stats.get("playCount")),
    )


def map_comment(comment: Any, post_id: str) -> Comment:
    d: dict[str, Any] = getattr(comment, "as_dict", None) or {}
    reply_id = str(d.get("reply_id") or "0")
    is_reply = reply_id not in ("0", "")
    return Comment(
        platform="tiktok",
        post_id=post_id,
        comment_id=str(d.get("cid") or comment.id),
        text=d.get("text") if d.get("text") is not None else (comment.text or ""),
        created_at=_epoch_utc(d.get("create_time")),
        likes=_int(d.get("digg_count", getattr(comment, "likes_count", None))),
        is_reply=is_reply,
        parent_comment_id=reply_id if is_reply else None,
    )


class TikTokApiScraper:
    platform = "tiktok"

    def __init__(
        self,
        settings: dict[str, Any],
        api_factory: Optional[Callable[[], Any]] = None,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._api_factory = api_factory or TikTokApi
        self._retry_sleep = retry_sleep
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._api: Any = None
        self._video_cache: dict[str, Any] = {}
        self.attempts_made = 0

    # ---- sesi ------------------------------------------------------------
    def _run(self, coro):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def _ensure_api(self) -> Any:
        if self._api is not None:
            return self._api
        ms_token = self.settings.get("env", {}).get("TIKTOK_MS_TOKEN") or None
        if not ms_token:
            log.warning("TIKTOK_MS_TOKEN kosong; mencoba tanpa token. Bila respons kosong, isi token. %s", MS_TOKEN_HINT)

        async def _open():
            api = self._api_factory()
            await api.__aenter__()
            await api.create_sessions(
                ms_tokens=[ms_token] if ms_token else None,
                num_sessions=1,
                sleep_after=3,
                browser="chromium",
                headless=False,
            )
            return api

        self._api = self._run(_open())
        log.info("sesi TikTok dibuat")
        return self._api

    def _with_retry(self, fn: Callable[[], Any]) -> Any:
        """Jalankan fn; respons kosong/invalid dicoba ulang, captcha langsung gagal."""
        self.attempts_made = 0
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            self.attempts_made = attempt
            try:
                return fn()
            except ttx.CaptchaException as e:
                raise ScrapeBlocked(f"TikTok menampilkan CAPTCHA ({e}). " + MS_TOKEN_HINT) from e
            except ttx.NotFoundException as e:
                raise AccountNotFound(str(e)) from e
            except (ttx.EmptyResponseException, ttx.InvalidResponseException, ttx.InvalidJSONException) as e:
                if attempt == len(RETRY_DELAYS):
                    raise ScrapeBlocked(
                        f"TikTok mengembalikan respons kosong/invalid {attempt}x ({e}). " + MS_TOKEN_HINT
                    ) from e
                log.warning("respons TikTok kosong/invalid (%s), coba lagi dalam %ds", e, delay)
                self._retry_sleep(delay)
        raise AssertionError("unreachable")

    # ---- scraping --------------------------------------------------------
    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        api = self._ensure_api()
        tolerance = self.settings["scrape"].older_posts_tolerance

        async def _collect() -> list[Post]:
            posts: list[Post] = []
            older_seen = 0
            user = api.user(username=username)
            agen = user.videos(count=max_posts)
            try:
                async for video in agen:
                    post = map_video(video, username)
                    stop, older_seen = should_stop(post.created_at, since, older_seen, tolerance)
                    if stop:
                        break
                    if older_seen:
                        continue
                    self._video_cache[post.post_id] = video
                    posts.append(post)
                    if len(posts) >= max_posts:
                        break
            finally:
                await agen.aclose()
            return posts

        yield from self._with_retry(lambda: self._run(_collect()))

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]:
        if limit <= 0:
            return
        api = self._ensure_api()
        video = self._video_cache.get(post.post_id) or api.video(url=post.url)

        async def _collect() -> list[Comment]:
            out: list[Comment] = []
            agen = video.comments(count=limit)
            try:
                async for c in agen:
                    out.append(map_comment(c, post.post_id))
                    if len(out) >= limit:
                        break
            finally:
                await agen.aclose()
            return out

        yield from self._with_retry(lambda: self._run(_collect()))

    def close(self) -> None:
        self._video_cache.clear()
        if self._api is not None:
            try:
                self._run(self._api.__aexit__(None, None, None))
            except Exception as e:  # noqa: BLE001
                log.warning("gagal menutup sesi TikTok: %s", e)
            self._api = None
        if self._loop is not None:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            self._loop = None
