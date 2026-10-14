"""Pemetaan nama adapter -> kelas scraper. Impor dilakukan malas agar library berat
(instaloader, TikTokApi) hanya dimuat saat benar-benar dipakai."""

from __future__ import annotations

import importlib
from typing import Any

from spi.scrapers.base import BaseScraper

# nama adapter -> (platform, modul, kelas)
_ADAPTERS: dict[str, tuple[str, str, str]] = {
    "instaloader": ("instagram", "spi.scrapers.instagram_instaloader", "InstaloaderScraper"),
    "instagram_playwright": ("instagram", "spi.scrapers.instagram_playwright", "InstagramPlaywrightScraper"),
    "tiktokapi": ("tiktok", "spi.scrapers.tiktok_tiktokapi", "TikTokApiScraper"),
    "tiktok_playwright": ("tiktok", "spi.scrapers.tiktok_playwright", "TikTokPlaywrightScraper"),
    "tiktok_apify": ("tiktok", "spi.scrapers.tiktok_apify", "TikTokApifyScraper"),
}


def available_adapters() -> list[str]:
    return list(_ADAPTERS)


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


def build(adapter: str, platform: str, cfg: Any, env: dict[str, str]) -> BaseScraper:
    """Buat instance scraper. `cfg` adalah AppConfig, `env` variabel lingkungan (.env)."""
    expected = platform_of(adapter)
    if expected != platform:
        raise ValueError(f"adapter {adapter!r} untuk platform {expected}, bukan {platform}")
    settings = {
        "scrape": cfg.scrape,
        "storage": cfg.storage,
        "env": env,
    }
    return resolve(adapter)(settings)
