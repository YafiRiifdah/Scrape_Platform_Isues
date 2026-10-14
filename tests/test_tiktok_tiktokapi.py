from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from TikTokApi import exceptions as ttx

from spi.config import ScrapeConfig, StorageConfig
from spi.scrapers.base import RateLimited, ScrapeBlocked, AccountNotFound
from spi.scrapers.tiktok_tiktokapi import TikTokApiScraper, map_comment, map_video

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def tt_video(vid="7001", days_ago=1, comments=()):
    ts = int((NOW - timedelta(days=days_ago)).timestamp())
    data = {
        "id": vid,
        "createTime": ts,
        "desc": "Banjir Jakarta hari ini #banjir #Jakarta",
        "statsV2": {"diggCount": "1500", "commentCount": "230", "shareCount": "40", "playCount": "90000"},
        "challenges": [{"title": "banjir"}, {"title": "Jakarta"}],
    }

    async def _comments(count=20, cursor=0, **kw):
        for c in comments:
            yield c

    return SimpleNamespace(id=vid, as_dict=data, comments=_comments)


def tt_comment(cid, text, likes=2, create_time=1760400000):
    return SimpleNamespace(
        id=str(cid),
        text=text,
        likes_count=likes,
        as_dict={"cid": str(cid), "text": text, "digg_count": likes, "create_time": create_time, "reply_id": "0"},
    )


class FakeUser:
    def __init__(self, videos, raise_exc=None):
        self._videos = videos
        self._raise = raise_exc

    async def videos(self, count=30, cursor=0, **kw):
        if self._raise:
            raise self._raise
        for v in self._videos:
            yield v


class FakeApi:
    """Meniru TikTokApi: async context manager + create_sessions + user()."""

    def __init__(self, user):
        self._user = user
        self.sessions_created = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def create_sessions(self, **kwargs):
        self.sessions_created += 1
        self.create_kwargs = kwargs

    def user(self, username=None, **kw):
        return self._user


def make_scraper(videos=(), raise_exc=None, ms_token="tok"):
    api = FakeApi(FakeUser(list(videos), raise_exc))
    settings = {
        "scrape": ScrapeConfig(older_posts_tolerance=1),
        "storage": StorageConfig(),
        "env": {"TIKTOK_MS_TOKEN": ms_token} if ms_token else {},
    }
    scraper = TikTokApiScraper(settings, api_factory=lambda: api, retry_sleep=lambda s: None)
    return scraper, api


def test_map_video_normalizes_fields():
    post = map_video(tt_video(), account="kompascom")
    assert post.platform == "tiktok"
    assert post.post_id == "7001"
    assert post.url == "https://www.tiktok.com/@kompascom/video/7001"
    assert post.created_at.tzinfo == timezone.utc
    assert post.caption.startswith("Banjir Jakarta")
    assert post.hashtags == ["banjir", "jakarta"]
    assert post.media_type == "video"
    assert (post.likes, post.comments_count, post.shares, post.views) == (1500, 230, 40, 90000)


def test_map_video_tolerates_missing_stats():
    v = tt_video()
    v.as_dict.pop("statsV2")
    v.as_dict.pop("challenges")
    post = map_video(v, account="x")
    assert post.likes is None and post.hashtags == []


def test_map_comment_reads_create_time_and_reply():
    c = map_comment(tt_comment(5, "mantap"), post_id="7001")
    assert c.comment_id == "5" and c.likes == 2
    assert c.created_at == datetime.fromtimestamp(1760400000, tz=timezone.utc)
    assert c.is_reply is False

    reply = tt_comment(6, "iya")
    reply.as_dict["reply_id"] = "5"
    r = map_comment(reply, post_id="7001")
    assert r.is_reply is True and r.parent_comment_id == "5"


def test_fetch_posts_filters_by_since_with_pinned_tolerance_and_max():
    videos = [tt_video("pinned", days_ago=40), tt_video("a", 1), tt_video("b", 2), tt_video("old", 30), tt_video("older", 31), tt_video("c", 1)]
    scraper, api = make_scraper(videos)

    got = [p.post_id for p in scraper.fetch_posts("kompascom", NOW - timedelta(days=7), max_posts=10)]

    assert got == ["a", "b"]
    assert api.sessions_created == 1
    assert api.create_kwargs["ms_tokens"] == ["tok"]
    assert api.create_kwargs["headless"] is False


def test_fetch_comments_respects_limit():
    comments = [tt_comment(i, f"k{i}") for i in range(5)]
    scraper, _ = make_scraper([tt_video("7001", comments=comments)])
    post = next(scraper.fetch_posts("kompascom", None, 10))

    got = list(scraper.fetch_comments(post, limit=3))

    assert [c.comment_id for c in got] == ["0", "1", "2"]


def test_empty_response_is_retried_then_blocked():
    scraper, api = make_scraper(raise_exc=ttx.EmptyResponseException(None, "empty"))

    with pytest.raises(ScrapeBlocked, match="msToken"):
        list(scraper.fetch_posts("kompascom", None, 10))
    assert scraper.attempts_made == 3


def test_captcha_is_blocked_without_retry():
    scraper, _ = make_scraper(raise_exc=ttx.CaptchaException(None, "captcha"))
    with pytest.raises(ScrapeBlocked):
        list(scraper.fetch_posts("kompascom", None, 10))
    assert scraper.attempts_made == 1


def test_not_found_maps_to_account_not_found():
    scraper, _ = make_scraper(raise_exc=ttx.NotFoundException(None, "nf"))
    with pytest.raises(AccountNotFound):
        list(scraper.fetch_posts("kompascom", None, 10))


def test_missing_ms_token_proceeds_without_token():
    scraper, api = make_scraper([tt_video("a", 1)], ms_token=None)
    got = list(scraper.fetch_posts("kompascom", None, 10))
    assert [p.post_id for p in got] == ["a"]
    assert api.create_kwargs["ms_tokens"] is None


def test_browser_executable_from_env_is_passed_to_create_sessions():
    api = FakeApi(FakeUser([tt_video("a", 1)]))
    settings = {
        "scrape": ScrapeConfig(),
        "storage": StorageConfig(),
        "env": {"SPI_BROWSER_PATH": r"C:\Browsers\msedge.exe"},
    }
    scraper = TikTokApiScraper(settings, api_factory=lambda: api, retry_sleep=lambda s: None)
    list(scraper.fetch_posts("kompascom", None, 10))
    assert api.create_kwargs["executable_path"] == r"C:\Browsers\msedge.exe"


def test_browser_executable_autodetected_when_env_missing(monkeypatch):
    from spi.scrapers import tiktok_tiktokapi as mod

    monkeypatch.setattr(mod, "detect_system_browser", lambda: r"C:\Detected\chrome.exe")
    scraper, api = make_scraper([tt_video("a", 1)])
    list(scraper.fetch_posts("kompascom", None, 10))
    assert api.create_kwargs["executable_path"] == r"C:\Detected\chrome.exe"


def test_session_failure_is_cleaned_up_and_reported():
    from spi.scrapers.base import ScraperError

    class BrokenApi(FakeApi):
        async def create_sessions(self, **kwargs):
            raise RuntimeError("BrowserType.launch: spawn UNKNOWN")

    api = BrokenApi(FakeUser([]))
    settings = {"scrape": ScrapeConfig(), "storage": StorageConfig(), "env": {}}
    scraper = TikTokApiScraper(settings, api_factory=lambda: api, retry_sleep=lambda s: None)

    with pytest.raises(ScraperError, match="browser"):
        list(scraper.fetch_posts("kompascom", None, 10))
    assert api.closed is True
    scraper.close()  # tidak boleh melempar


def test_close_closes_api():
    scraper, api = make_scraper([tt_video("a", 1)])
    list(scraper.fetch_posts("kompascom", None, 10))
    scraper.close()
    assert api.closed is True
