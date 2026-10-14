"""Adapter TikTok berbasis library TikTokApi (pendekatan A).

Tidak perlu login; butuh cookie `msToken` dari tiktok.com (env TIKTOK_MS_TOKEN).
TikTokApi bersifat async; adapter ini membungkusnya menjadi sinkron dengan event loop
khusus agar runner tetap sederhana dan sama untuk kedua platform.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Iterator, Optional

from TikTokApi import TikTokApi
from TikTokApi import exceptions as ttx

from spi.models import Comment, Post
from spi.scrapers.base import AccountNotFound, ScrapeBlocked, ScraperError, should_stop
from spi.scrapers.tiktok_common import detect_system_browser, map_comment_dict, map_item

log = logging.getLogger("spi.scrapers.tiktok")

RETRY_DELAYS = (10, 30, 60)
MS_TOKEN_HINT = (
    "Perbarui TIKTOK_MS_TOKEN di .env: buka tiktok.com di browser, DevTools (F12) -> "
    "Application -> Cookies -> msToken. Jalankan dari IP rumahan (bukan VPN/datacenter)."
)


def map_video(video: Any, account: str) -> Post:
    """Petakan TikTokApi Video (lewat `as_dict`) ke model Post."""
    d = dict(video.as_dict or {})
    d.setdefault("id", getattr(video, "id", None))
    return map_item(d, account)


def map_comment(comment: Any, post_id: str) -> Comment:
    d = dict(getattr(comment, "as_dict", None) or {})
    d.setdefault("cid", getattr(comment, "id", None))
    d.setdefault("text", getattr(comment, "text", None))
    d.setdefault("digg_count", getattr(comment, "likes_count", None))
    return map_comment_dict(d, post_id)


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
        env = self.settings.get("env", {})
        ms_token = env.get("TIKTOK_MS_TOKEN") or None
        if not ms_token:
            log.warning("TIKTOK_MS_TOKEN kosong; mencoba tanpa token. Bila respons kosong, isi token. %s", MS_TOKEN_HINT)
        executable_path = env.get("SPI_BROWSER_PATH") or detect_system_browser()
        log.info("browser TikTok: %s", executable_path or "Chromium bawaan Playwright")

        async def _open():
            api = self._api_factory()
            await api.__aenter__()
            try:
                await api.create_sessions(
                    ms_tokens=[ms_token] if ms_token else None,
                    num_sessions=1,
                    sleep_after=3,
                    browser="chromium",
                    headless=False,
                    executable_path=executable_path,
                )
            except BaseException:
                await api.__aexit__(None, None, None)
                raise
            return api

        try:
            self._api = self._run(_open())
        except ttx.TikTokException:
            raise
        except Exception as e:  # noqa: BLE001
            raise ScraperError(
                f"gagal membuka browser untuk TikTok ({type(e).__name__}: {str(e).splitlines()[0]}). "
                "Pastikan Edge/Chrome terpasang atau isi SPI_BROWSER_PATH di .env dengan path browser."
            ) from e
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
            except (KeyError, IndexError, TypeError) as e:
                raise ScrapeBlocked(
                    f"TikTok mengirim data tidak lengkap ({type(e).__name__}: {e}); biasanya tanda pemblokiran bot. "
                    "Gunakan adapter 'tiktok_playwright' di config.yaml. " + MS_TOKEN_HINT
                ) from e
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
