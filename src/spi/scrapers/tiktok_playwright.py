"""Adapter TikTok pendekatan B: browser sungguhan (Edge/Chrome via Playwright, profil persisten)
yang membuka halaman profil/video lalu merekam respons JSON internal TikTok saat halaman di-scroll.

Kenapa: library TikTokApi memanggil endpoint TikTok langsung dan diblokir (respons kosong /
`userInfo` kosong), sedangkan request yang dibuat halaman TikTok sendiri ditandatangani dengan
benar dan lolos. Flag otomasi Chromium dimatikan agar tidak terdeteksi bot.

Endpoint yang disadap:
- `/api/post/item_list/`  -> daftar video (`itemList`, `hasMore`)
- `/api/comment/list/`    -> komentar level atas (`comments`, `has_more`); balasan tidak diambil.

CAPTCHA: bila muncul, scraper menunggu `captcha_wait_seconds` agar pengguna menyelesaikannya di
jendela browser; profil persisten menyimpan hasilnya untuk run berikutnya.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from spi.models import Comment, Post
from spi.scrapers.base import AccountNotFound, RateLimited, ScrapeBlocked, ScraperError, should_stop
from spi.scrapers.tiktok_common import browser_executable, map_comment_dict, map_item

log = logging.getLogger("spi.scrapers.tiktok_playwright")

ITEM_LIST_PATH = "/api/post/item_list/"
COMMENT_LIST_PATH = "/api/comment/list/"
CAPTCHA_SELECTOR = "#captcha-verify-image, [id^='captcha_container'], [class*='captcha-verify'], div[class*='captcha_verify']"
NOT_FOUND_MARKERS = ("Couldn't find this account", "Tidak dapat menemukan akun", "This account is private", "Akun ini privat")
MAX_IDLE_SCROLLS = 4
CAPTCHA_POLL_SECONDS = 5


def parse_item_list(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    items = payload.get("itemList") or []
    return [i for i in items if isinstance(i, dict) and i.get("id")], bool(payload.get("hasMore"))


def parse_comment_list(payload: dict[str, Any], post_id: str) -> tuple[list[Comment], bool]:
    raw = payload.get("comments") or []
    comments = [map_comment_dict(c, post_id) for c in raw if isinstance(c, dict) and c.get("cid")]
    return comments, bool(payload.get("has_more"))


class PostCollector:
    """Kumpulkan post dari beberapa halaman item_list dengan aturan berhenti bersama."""

    def __init__(self, account: str, since: Optional[datetime], max_posts: int, tolerance: int):
        self.account, self.since, self.max_posts, self.tolerance = account, since, max_posts, tolerance
        self.posts: list[Post] = []
        self._seen: set[str] = set()
        self._older_seen = 0
        self.done = False

    def add(self, items: list[dict[str, Any]]) -> bool:
        for raw in items:
            if self.done:
                break
            post = map_item(raw, self.account)
            if post.post_id in self._seen:
                continue
            self._seen.add(post.post_id)
            stop, self._older_seen = should_stop(post.created_at, self.since, self._older_seen, self.tolerance)
            if stop:
                self.done = True
                break
            if self._older_seen:
                continue
            self.posts.append(post)
            if len(self.posts) >= self.max_posts:
                self.done = True
        return self.done


class TikTokPlaywrightScraper:
    platform = "tiktok"

    def __init__(
        self,
        settings: dict[str, Any],
        page_factory: Optional[Callable[[], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._page_factory = page_factory or self._new_page
        self._sleep = sleep
        self._pw: Any = None
        self._ctx: Any = None

    # ---- browser -----------------------------------------------------------
    def _ensure_context(self) -> Any:
        if self._ctx is not None:
            return self._ctx
        from playwright.sync_api import sync_playwright

        env = self.settings.get("env", {})
        profile_dir = Path(self.settings["storage"].browser_profile_dir) / "tiktok"
        profile_dir.mkdir(parents=True, exist_ok=True)
        exe = browser_executable(env)
        log.info("membuka browser %s dengan profil %s", exe or "Chromium bawaan", profile_dir)
        try:
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                executable_path=exe,
                viewport={"width": 1280, "height": 900},
                locale="id-ID",
                timezone_id="Asia/Jakarta",
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:  # noqa: BLE001
            self.close()
            raise ScraperError(
                f"gagal membuka browser ({type(e).__name__}: {str(e).splitlines()[0]}). "
                "Pastikan Edge/Chrome terpasang atau isi SPI_BROWSER_PATH di .env."
            ) from e
        return self._ctx

    def _new_page(self) -> Any:
        return self._ensure_context().new_page()

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _sink_handler(path: str, sink: list[tuple[int, dict[str, Any]]]) -> Callable[[Any], None]:
        def handler(response: Any) -> None:
            url = getattr(response, "url", "")
            if path not in url or "/reply/" in url:
                return
            status = getattr(response, "status", 200)
            payload: dict[str, Any] = {}
            if status < 400:
                try:
                    payload = response.json() or {}
                except Exception:  # noqa: BLE001 - body kosong = tanda pemblokiran, ditangani sebagai idle
                    payload = {}
            sink.append((status, payload if isinstance(payload, dict) else {}))

        return handler

    def _pause(self, page: Any, lo: float = 1.5, hi: float = 3.0) -> None:
        page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))

    def _check_blocked(self, page: Any, label: str) -> None:
        body = ""
        try:
            body = page.inner_text("body") or ""
        except Exception:  # noqa: BLE001
            pass
        if any(marker in body for marker in NOT_FOUND_MARKERS):
            raise AccountNotFound(f"{label}: akun tidak ditemukan atau privat")
        if "/login" in page.url:
            raise ScrapeBlocked(f"{label}: TikTok meminta login")
        captcha = page.locator(CAPTCHA_SELECTOR)
        if captcha.count() and captcha.first.is_visible():
            wait = float(self.settings["scrape"].captcha_wait_seconds)
            waited = 0.0
            log.warning("%s: CAPTCHA muncul. Selesaikan di jendela browser (menunggu hingga %.0f detik)", label, wait)
            while waited < wait:
                self._sleep(CAPTCHA_POLL_SECONDS)
                waited += CAPTCHA_POLL_SECONDS
                captcha = page.locator(CAPTCHA_SELECTOR)
                if not captcha.count() or not captcha.first.is_visible():
                    log.info("%s: CAPTCHA selesai", label)
                    return
            raise ScrapeBlocked(
                f"{label}: CAPTCHA tidak diselesaikan dalam {wait:.0f} detik. Jalankan lagi dan selesaikan CAPTCHA di jendela browser."
            )

    @staticmethod
    def _goto(page: Any, url: str, label: str) -> None:
        """Navigasi; error jaringan/HTTP dari Playwright diterjemahkan ke ScraperError."""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except ScraperError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ScraperError(f"{label}: halaman tidak bisa dibuka ({str(e).splitlines()[0]})") from e

    @staticmethod
    def _raise_for_status(status: int, label: str) -> None:
        if status == 429:
            raise RateLimited(f"{label}: TikTok membalas 429")
        if status >= 400:
            raise ScrapeBlocked(f"{label}: TikTok membalas HTTP {status}")

    # ---- scraping ----------------------------------------------------------
    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        tolerance = self.settings["scrape"].older_posts_tolerance
        collector = PostCollector(username, since, max_posts, tolerance)
        sink: list[tuple[int, dict[str, Any]]] = []
        page = self._page_factory()
        page.on("response", self._sink_handler(ITEM_LIST_PATH, sink))
        try:
            self._goto(page, f"https://www.tiktok.com/@{username}", f"tiktok:{username}")
            self._pause(page, 3, 5)
            self._check_blocked(page, f"tiktok:{username}")
            idle = 0
            has_more = True
            while not collector.done and has_more and idle < MAX_IDLE_SCROLLS:
                got_data = False
                while sink:
                    status, payload = sink.pop(0)
                    self._raise_for_status(status, f"tiktok:{username}")
                    items, more = parse_item_list(payload)
                    if items:
                        got_data = True
                        collector.add(items)
                    has_more = has_more and (more or not payload)
                if collector.done or not has_more:
                    break
                idle = 0 if got_data else idle + 1
                page.mouse.wheel(0, 2500)
                self._pause(page)
            log.info("tiktok:%s: %d post terkumpul", username, len(collector.posts))
        finally:
            page.close()
        yield from collector.posts

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]:
        if limit <= 0:
            return
        sink: list[tuple[int, dict[str, Any]]] = []
        collected: list[Comment] = []
        seen: set[str] = set()
        page = self._page_factory()
        page.on("response", self._sink_handler(COMMENT_LIST_PATH, sink))
        try:
            self._goto(page, post.url, f"tiktok:{post.account}/{post.post_id}")
            self._pause(page, 3, 5)
            self._check_blocked(page, f"tiktok:{post.account}/{post.post_id}")
            idle = 0
            has_more = True
            while len(collected) < limit and has_more and idle < MAX_IDLE_SCROLLS:
                got_data = False
                while sink:
                    status, payload = sink.pop(0)
                    self._raise_for_status(status, f"tiktok:{post.post_id}")
                    comments, more = parse_comment_list(payload, post.post_id)
                    for c in comments:
                        if c.comment_id not in seen:
                            seen.add(c.comment_id)
                            collected.append(c)
                    if comments:
                        got_data = True
                    has_more = has_more and (more or not payload)
                if len(collected) >= limit or not has_more:
                    break
                idle = 0 if got_data else idle + 1
                page.mouse.move(1000, 600)  # panel komentar di sisi kanan
                page.mouse.wheel(0, 2500)
                self._pause(page)
        finally:
            page.close()
        yield from collected[:limit]

    def close(self) -> None:
        for closer in (lambda: self._ctx and self._ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception as e:  # noqa: BLE001
                log.debug("gagal menutup browser: %s", e)
        self._ctx = None
        self._pw = None
