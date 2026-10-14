"""Adapter cadangan (pendekatan B) untuk TikTok berbasis Playwright.

Belum diimplementasikan. Rencana saat adapter TikTokApi rusak:

1. `chromium.launch_persistent_context(user_data_dir="data/browser_profile_tiktok",
   headless=False)`; tidak wajib login, tapi profil persisten membuat cookie
   (`msToken`, `ttwid`) stabil dan mengurangi CAPTCHA.
2. `fetch_posts`: buka `https://www.tiktok.com/@{username}`, tangkap respons JSON
   `/api/post/item_list/` lewat `page.on("response")`, scroll sampai `since` atau
   `max_posts`. Field: `itemList[*].id`, `createTime`, `desc`, `stats`/`statsV2`,
   `challenges[*].title`.
3. `fetch_comments`: buka URL video, scroll panel komentar, tangkap `/api/comment/list/`
   dan `/api/comment/list/reply/`. Field: `comments[*].cid`, `text`, `digg_count`,
   `create_time`, `reply_id`.
4. Halaman verifikasi/CAPTCHA (elemen `#captcha-verify-image` atau URL `/verify`) ->
   `ScrapeBlocked`; respons kosong berulang -> `RateLimited`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Optional

from spi.models import Comment, Post

_MSG = (
    "adapter tiktok_playwright belum diimplementasikan. Gunakan adapter 'tiktokapi', "
    "atau isi kerangka ini sesuai rencana di docstring modul."
)


class TikTokPlaywrightScraper:
    platform = "tiktok"

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        raise NotImplementedError(_MSG)
        yield  # pragma: no cover

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]:
        raise NotImplementedError(_MSG)
        yield  # pragma: no cover

    def close(self) -> None:
        return None
