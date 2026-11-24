"""Pemetaan nama adapter -> kelas scraper. Impor dilakukan malas agar library berat
(instaloader, TikTokApi) hanya dimuat saat benar-benar dipakai."""

from __future__ import annotations

import importlib
from typing import Any, Optional

from spi.scrapers.base import BaseScraper

# nama adapter -> (platform, modul, kelas)
_ADAPTERS: dict[str, tuple[str, str, str]] = {
    "instaloader": ("instagram", "spi.scrapers.instagram_instaloader", "InstaloaderScraper"),
    "instagram_playwright": ("instagram", "spi.scrapers.instagram_playwright", "InstagramPlaywrightScraper"),
    "instagram_apify": ("instagram", "spi.scrapers.instagram_apify", "InstagramApifyScraper"),
    "tiktokapi": ("tiktok", "spi.scrapers.tiktok_tiktokapi", "TikTokApiScraper"),
    "tiktok_playwright": ("tiktok", "spi.scrapers.tiktok_playwright", "TikTokPlaywrightScraper"),
    "tiktok_apify": ("tiktok", "spi.scrapers.tiktok_apify", "TikTokApifyScraper"),
}


# Adapter yang memakai token Apify milik pemilik job.
APIFY_ADAPTERS = frozenset({"instagram_apify", "tiktok_apify"})


def available_adapters() -> list[str]:
    return list(_ADAPTERS)


def uses_apify(adapter: str) -> bool:
    return adapter in APIFY_ADAPTERS


def platform_of(adapter: str) -> str:
    return _entry(adapter)[0]


def _entry(adapter: str) -> tuple[str, str, str]:
    try:
        return _ADAPTERS[adapter]
    except KeyError:
        raise KeyError(
            f"adapter tidak dikenal: {adapter!r}. Pilihan: {', '.join(_ADAPTERS)}"
        ) from None


def resolve(adapter: str) -> type[BaseScraper]:
    _, module_name, class_name = _entry(adapter)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def build(
    adapter: str, platform: str, cfg: Any, env: dict[str, str], apify: Any = None, apify_error: Optional[str] = None
) -> BaseScraper:
    """Buat instance scraper. `cfg` adalah AppConfig, `env` variabel lingkungan (.env).

    `apify` adalah ApifyAccess milik pemilik job dan hanya diteruskan ke adapter Apify; `apify_error`
    menjelaskan mengapa akses itu tidak tersedia.
    """
    expected = platform_of(adapter)
    if expected != platform:
        raise ValueError(f"adapter {adapter!r} untuk platform {expected}, bukan {platform}")
    settings: dict[str, Any] = {
        "scrape": cfg.scrape,
        "storage": cfg.storage,
        "env": env,
    }
    if uses_apify(adapter):
        settings["apify"] = apify
        settings["apify_error"] = apify_error
    return resolve(adapter)(settings)
