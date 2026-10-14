import pytest

from spi.scrapers import registry
from spi.scrapers.base import AccountNotFound, RateLimited, ScrapeBlocked, ScraperError


def test_known_adapter_names_are_registered():
    assert set(registry.available_adapters()) == {
        "instaloader",
        "instagram_playwright",
        "tiktokapi",
        "tiktok_playwright",
        "tiktok_apify",
    }


def test_unknown_adapter_raises_clear_error():
    with pytest.raises(KeyError, match="adapter tidak dikenal"):
        registry.resolve("tidak_ada")


def test_adapter_platform_is_enforced():
    assert registry.platform_of("instaloader") == "instagram"
    assert registry.platform_of("tiktokapi") == "tiktok"
    assert registry.platform_of("tiktok_playwright") == "tiktok"


def test_playwright_adapters_are_stubs_with_helpful_message():
    from spi.scrapers.instagram_playwright import InstagramPlaywrightScraper

    scraper = InstagramPlaywrightScraper(settings={})
    with pytest.raises(NotImplementedError, match="belum diimplementasikan"):
        list(scraper.fetch_posts("kompascom", since=None, max_posts=1))


def test_exception_hierarchy():
    assert issubclass(RateLimited, ScraperError)
    assert issubclass(ScrapeBlocked, ScraperError)
    assert issubclass(AccountNotFound, ScraperError)
