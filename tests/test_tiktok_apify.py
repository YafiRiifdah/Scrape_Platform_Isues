from datetime import datetime, timedelta, timezone

import pytest

from spi.config import ScrapeConfig, StorageConfig
from spi.models import Post
from spi.scrapers.base import AccountNotFound, RateLimited, ScrapeBlocked, ScraperError
from spi.scrapers.tiktok_apify import (
    COMMENTS_ACTOR,
    POSTS_ACTOR,
    TikTokApifyScraper,
    raise_for_apify_status,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def apify_post(vid, days_ago=1, author="kompascom"):
    created = NOW - timedelta(days=days_ago)
    return {
        "id": vid,
        "text": "Banjir Jakarta #banjir #Jakarta",
        "createTime": int(created.timestamp()),
        "createTimeISO": created.isoformat().replace("+00:00", "Z"),
        "diggCount": 150,
        "shareCount": 4,
        "playCount": 9000,
        "commentCount": 39,
        "hashtags": [{"name": "banjir"}, {"name": "Jakarta"}],
        "webVideoUrl": f"https://www.tiktok.com/@{author}/video/{vid}",
        "authorMeta": {"name": author},
        "isPinned": days_ago > 30,
    }


def apify_comment(cid, video_url, text="mantap", likes=3):
    return {
        "cid": cid,
        "text": text,
        "diggCount": likes,
        "createTimeISO": "2026-10-13T10:00:00.000Z",
        "videoWebUrl": video_url,
        "uniqueId": "netizen",
        "replyCommentTotal": 0,
    }


class FakeRunner:
    def __init__(self, results):
        self.results = results  # actor -> list items (atau exception)
        self.calls = []

    def __call__(self, actor, payload):
        self.calls.append((actor, payload))
        r = self.results[actor]
        if isinstance(r, Exception):
            raise r
        return r


def make_scraper(results, token="apify_token"):
    runner = FakeRunner(results)
    settings = {
        "scrape": ScrapeConfig(),
        "storage": StorageConfig(),
        "env": {"APIFY_TOKEN": token} if token else {},
    }
    return TikTokApifyScraper(settings, run_actor=runner), runner


def test_fetch_posts_maps_items_and_filters_by_since():
    items = [apify_post("pinned", 40), apify_post("a", 1), apify_post("b", 2), apify_post("old", 20)]
    scraper, runner = make_scraper({POSTS_ACTOR: items})

    posts = list(scraper.fetch_posts("kompascom", NOW - timedelta(days=7), max_posts=50))

    assert [p.post_id for p in posts] == ["a", "b"]
    p = posts[0]
    assert p.platform == "tiktok" and p.account == "kompascom"
    assert p.url == "https://www.tiktok.com/@kompascom/video/a"
    assert p.hashtags == ["banjir", "jakarta"]
    assert (p.likes, p.comments_count, p.shares, p.views) == (150, 39, 4, 9000)
    assert p.created_at.tzinfo == timezone.utc

    actor, payload = runner.calls[0]
    assert actor == POSTS_ACTOR
    assert payload["profiles"] == ["kompascom"]
    assert payload["resultsPerPage"] == 50
    assert payload["shouldDownloadVideos"] is False


def test_fetch_posts_respects_max_posts():
    items = [apify_post(f"p{i}", 1) for i in range(10)]
    scraper, _ = make_scraper({POSTS_ACTOR: items})
    assert len(list(scraper.fetch_posts("kompascom", None, max_posts=3))) == 3


def test_error_item_not_found_raises_account_not_found():
    scraper, _ = make_scraper({POSTS_ACTOR: [{"url": "x", "error": "profile not found", "errorCode": "NOT_FOUND"}]})
    with pytest.raises(AccountNotFound):
        list(scraper.fetch_posts("tidakada", None, 10))


def test_fetch_comments_batches_all_posts_in_one_run_and_serves_per_post():
    posts_items = [apify_post("a", 1), apify_post("b", 1)]
    url_a, url_b = posts_items[0]["webVideoUrl"], posts_items[1]["webVideoUrl"]
    comments = [apify_comment("1", url_a, likes=9), apify_comment("2", url_a), apify_comment("3", url_a), apify_comment("4", url_b)]
    scraper, runner = make_scraper({POSTS_ACTOR: posts_items, COMMENTS_ACTOR: comments})
    posts = list(scraper.fetch_posts("kompascom", None, 10))

    got_a = list(scraper.fetch_comments(posts[0], limit=2))
    got_b = list(scraper.fetch_comments(posts[1], limit=2))

    assert [c.comment_id for c in got_a] == ["1", "2"]
    assert got_a[0].likes == 9 and got_a[0].post_id == "a" and got_a[0].is_reply is False
    assert got_a[0].created_at == datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    assert [c.comment_id for c in got_b] == ["4"]

    comment_calls = [c for c in runner.calls if c[0] == COMMENTS_ACTOR]
    assert len(comment_calls) == 1
    payload = comment_calls[0][1]
    assert set(payload["postURLs"]) == {url_a, url_b}
    assert payload["commentsPerPost"] == 2
    assert payload["maxRepliesPerComment"] == 0


def test_fetch_comments_for_unknown_post_runs_single_request():
    scraper, runner = make_scraper({COMMENTS_ACTOR: [apify_comment("1", "https://www.tiktok.com/@x/video/z")]})
    post = Post(platform="tiktok", account="x", post_id="z", url="https://www.tiktok.com/@x/video/z", created_at=NOW)

    got = list(scraper.fetch_comments(post, limit=5))

    assert [c.comment_id for c in got] == ["1"]
    assert runner.calls[0][1]["postURLs"] == [post.url]


def test_missing_token_gives_actionable_error():
    scraper, _ = make_scraper({POSTS_ACTOR: []}, token=None)
    with pytest.raises(ScrapeBlocked, match="APIFY_TOKEN"):
        list(scraper.fetch_posts("kompascom", None, 10))


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (429, "", RateLimited),
        (401, '{"error":{"type":"token-not-found"}}', ScrapeBlocked),
        (402, '{"error":{"type":"insufficient-funds"}}', ScrapeBlocked),
        (408, "", ScraperError),
        (500, "", ScraperError),
    ],
)
def test_raise_for_apify_status(status, body, expected):
    with pytest.raises(expected):
        raise_for_apify_status(status, body)


def test_raise_for_apify_status_passes_on_success():
    raise_for_apify_status(200, "[]")
    raise_for_apify_status(201, "[]")
