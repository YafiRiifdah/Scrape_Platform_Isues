"""Adapter cadangan (pendekatan B) untuk Instagram berbasis Playwright.

Belum diimplementasikan. Rencana saat adapter Instaloader rusak:

1. `playwright.sync_api.sync_playwright()` -> `chromium.launch_persistent_context(
   user_data_dir="data/browser_profile_instagram", headless=False)`. Profil persisten
   menyimpan cookie login; pengguna login manual sekali di jendela yang terbuka
   (bisa melewati 2FA/challenge seperti manusia).
2. `fetch_posts`: buka `https://www.instagram.com/{username}/`, pasang
   `page.on("response", handler)` yang menangkap respons JSON dari endpoint
   `/graphql/query` atau `/api/v1/feed/user/...`; scroll ke bawah sampai post lebih tua
   dari `since` (dengan toleransi pinned) atau `max_posts` tercapai. Petakan node ke `Post`.
3. `fetch_comments`: buka `https://www.instagram.com/p/{shortcode}/`, klik "lihat komentar
   lainnya" berulang sambil menangkap respons `/api/v1/media/{id}/comments/`.
4. Deteksi login wall / challenge (URL berisi `/accounts/login` atau `/challenge/`) ->
   raise `ScrapeBlocked`; HTTP 429 -> `RateLimited`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Optional

from spi.models import Comment, Post

_MSG = (
    "adapter instagram_playwright belum diimplementasikan. Gunakan adapter 'instaloader', "
    "atau isi kerangka ini sesuai rencana di docstring modul."
)


class InstagramPlaywrightScraper:
    platform = "instagram"

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        raise NotImplementedError(_MSG)
        yield  # pragma: no cover - menjadikan fungsi ini generator

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]:
        raise NotImplementedError(_MSG)
        yield  # pragma: no cover

    def close(self) -> None:
        return None
