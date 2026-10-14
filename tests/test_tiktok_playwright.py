from datetime import datetime, timedelta, timezone

import pytest

from spi.scrapers.base import AccountNotFound, RateLimited, ScrapeBlocked
from spi.scrapers.tiktok_playwright import (
    PostCollector,
    TikTokPlaywrightScraper,
    parse_comment_list,
    parse_item_list,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def item(vid, days_ago=1):
    ts = int((NOW - timedelta(days=days_ago)).timestamp())
    return {
        "id": vid,
        "createTime": ts,
        "desc": "Banjir Jakarta #banjir",
        "statsV2": {"diggCount": "10", "commentCount": "2", "shareCount": "1", "playCount": "500"},
        "challenges": [{"title": "banjir"}],
        "author": {"uniqueId": "kompascom"},
    }


def test_parse_item_list_returns_items_and_has_more():
    items, has_more = parse_item_list({"itemList": [item("1"), item("2")], "hasMore": True, "statusCode": 0})
    assert [i["id"] for i in items] == ["1", "2"] and has_more is True


def test_parse_item_list_tolerates_empty_or_error_payload():
    assert parse_item_list({}) == ([], False)
    assert parse_item_list({"statusCode": 10201, "itemList": None}) == ([], False)


def test_parse_comment_list_maps_fields():
    payload = {
        "comments": [
            {"cid": "9", "text": "mantap", "digg_count": 4, "create_time": 1760400000, "reply_id": "0", "reply_comment_total": 2},
            {"cid": "10", "text": "balasan", "digg_count": 0, "create_time": 1760400100, "reply_id": "9"},
        ],
        "has_more": 1,
        "cursor": 20,
    }
    comments, has_more = parse_comment_list(payload, post_id="7001")
    assert has_more is True
    assert comments[0].comment_id == "9" and comments[0].likes == 4 and comments[0].is_reply is False
    assert comments[0].created_at == datetime.fromtimestamp(1760400000, tz=timezone.utc)
    assert comments[1].is_reply is True and comments[1].parent_comment_id == "9"


def test_post_collector_stops_after_tolerated_older_posts_and_dedupes():
    c = PostCollector(account="kompascom", since=NOW - timedelta(days=7), max_posts=100, tolerance=1)

    done = c.add([item("pinned", 40), item("a", 1), item("a", 1), item("b", 2)])
    assert done is False
    assert [p.post_id for p in c.posts] == ["a", "b"]

    done = c.add([item("old1", 20), item("old2", 21), item("c", 1)])
    assert done is True
    assert [p.post_id for p in c.posts] == ["a", "b"]


def test_post_collector_stops_at_max_posts():
    c = PostCollector(account="x", since=None, max_posts=2, tolerance=3)
    assert c.add([item("1"), item("2"), item("3")]) is True
    assert len(c.posts) == 2


class FakeLocator:
    def __init__(self, visible):
        self._visible = visible

    def count(self):
        return 1 if self._visible else 0

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible


class FakeResponse:
    def __init__(self, kind, status, payload):
        self.url = f"https://www.tiktok.com/api/{'post/item_list' if kind == 'item_list' else 'comment/list'}/?aid=1988"
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


class FakePage:
    """Halaman palsu: setiap goto/scroll 'menerima' satu respons dari daftar `responses`."""

    def __init__(self, responses, url="https://www.tiktok.com/@kompascom", captcha=False, body_text=""):
        self._responses = [FakeResponse(*r) for r in responses]
        self.url = url
        self._captcha = captcha
        self._body_text = body_text
        self._handlers = []
        self.scrolls = 0
        self.closed = False

    def on(self, event, handler):
        self._handlers.append(handler)

    def goto(self, url, **kw):
        self.url = url if "video" in url else self.url
        self._emit()

    def _emit(self):
        if self._responses:
            resp = self._responses.pop(0)
            for h in self._handlers:
                h(resp)

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        if "captcha" in selector:
            return FakeLocator(self._captcha)
        return FakeLocator(False)

    def inner_text(self, selector):
        return self._body_text

    def close(self):
        self.closed = True

    @property
    def mouse(self):
        page = self

        class M:
            def wheel(self, dx, dy):
                page.scrolls += 1
                page._emit()

            def move(self, x, y):
                pass

        return M()


def make_scraper(page, captcha_wait=0):
    from spi.config import ScrapeConfig, StorageConfig

    settings = {
        "scrape": ScrapeConfig(older_posts_tolerance=1, captcha_wait_seconds=captcha_wait),
        "storage": StorageConfig(),
        "env": {},
    }
    return TikTokPlaywrightScraper(settings, page_factory=lambda: page, sleep=lambda s: None)


def test_fetch_posts_collects_from_intercepted_item_list_responses():
    page = FakePage(
        responses=[
            ("item_list", 200, {"itemList": [item("a", 1), item("b", 2)], "hasMore": True}),
            ("item_list", 200, {"itemList": [item("c", 3)], "hasMore": False}),
        ]
    )
    scraper = make_scraper(page)

    got = [p.post_id for p in scraper.fetch_posts("kompascom", NOW - timedelta(days=7), max_posts=10)]

    assert got == ["a", "b", "c"]
    assert page.closed is True


def test_fetch_posts_stops_when_no_new_data_after_several_scrolls():
    page = FakePage(responses=[("item_list", 200, {"itemList": [item("a", 1)], "hasMore": True})])
    scraper = make_scraper(page)
    got = list(scraper.fetch_posts("kompascom", None, max_posts=10))
    assert [p.post_id for p in got] == ["a"]
    assert page.scrolls <= 8


def test_fetch_posts_raises_blocked_when_captcha_persists():
    page = FakePage(responses=[], captcha=True)
    scraper = make_scraper(page, captcha_wait=0)
    with pytest.raises(ScrapeBlocked, match="CAPTCHA"):
        list(scraper.fetch_posts("kompascom", None, 10))


def test_fetch_posts_raises_not_found_for_missing_account():
    page = FakePage(responses=[], body_text="Couldn't find this account")
    scraper = make_scraper(page)
    with pytest.raises(AccountNotFound):
        list(scraper.fetch_posts("tidakada", None, 10))


def test_fetch_posts_raises_rate_limited_on_429():
    page = FakePage(responses=[("item_list", 429, {})])
    scraper = make_scraper(page)
    with pytest.raises(RateLimited):
        list(scraper.fetch_posts("kompascom", None, 10))


def test_fetch_comments_collects_until_limit():
    from spi.models import Post

    post = Post(platform="tiktok", account="kompascom", post_id="7001", url="https://www.tiktok.com/@kompascom/video/7001", created_at=NOW)
    page = FakePage(
        responses=[
            ("comment_list", 200, {"comments": [{"cid": "1", "text": "a", "digg_count": 1}, {"cid": "2", "text": "b", "digg_count": 0}], "has_more": 1}),
            ("comment_list", 200, {"comments": [{"cid": "3", "text": "c", "digg_count": 0}], "has_more": 0}),
        ]
    )
    scraper = make_scraper(page)

    got = list(scraper.fetch_comments(post, limit=2))

    assert [c.comment_id for c in got] == ["1", "2"]
