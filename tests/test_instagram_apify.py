from datetime import datetime, timedelta, timezone

import pytest

from spi.config import ScrapeConfig, StorageConfig
from spi.models import Post
from spi.scrapers.apify_base import ApifyAccess
from spi.scrapers.base import AccountNotFound, ScrapeBlocked, ScraperError
from spi.scrapers.instagram_apify import (
    COMMENTS_ACTOR,
    POSTS_ACTOR,
    InstagramApifyScraper,
    map_comment_item,
    map_post_item,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def ig_post(code, days_ago=1, kind="Image", likes=1200, pinned=False):
    ts = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", ".000Z")
    return {
        "id": f"id{code}",
        "type": kind,
        "shortCode": code,
        "caption": "Harga beras naik #beras #Ekonomi",
        "hashtags": ["beras", "Ekonomi"],
        "url": f"https://www.instagram.com/p/{code}/",
        "commentsCount": 340,
        "likesCount": likes,
        "timestamp": ts,
        "videoViewCount": 9000 if kind == "Video" else None,
        "videoPlayCount": 15000 if kind == "Video" else None,
        "ownerUsername": "kompascom",
        "isPinned": pinned,
    }


def ig_comment(cid, code, text="setuju", likes=3, replies=0):
    return {
        "id": cid,
        "text": text,
        "ownerUsername": "netizen",
        "timestamp": "2026-11-23T10:00:00.000Z",
        "likesCount": likes,
        "repliesCount": replies,
        "postUrl": f"https://www.instagram.com/p/{code}/",
    }


class FakeRunner:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, actor, payload):
        self.calls.append((actor, payload))
        r = self.results[actor]
        if isinstance(r, Exception):
            raise r
        return r


def make_scraper(results, token="apify_token"):
    runner = FakeRunner(results)
    settings = {"scrape": ScrapeConfig(), "storage": StorageConfig(), "env": {}}
    if token:
        settings["apify"] = ApifyAccess(token=token, user_id=1, credential_id=1)
    return InstagramApifyScraper(settings, run_actor=runner), runner


# ---- mapping ----------------------------------------------------------------------
def test_map_post_item_normalizes_fields():
    p = map_post_item(ig_post("ABC", kind="Sidecar"), account="kompascom")
    assert p.platform == "instagram" and p.account == "kompascom"
    assert p.post_id == "ABC"
    assert p.url == "https://www.instagram.com/p/ABC/"
    assert p.created_at == NOW - timedelta(days=1)
    assert p.hashtags == ["beras", "ekonomi"]
    assert p.media_type == "carousel"
    assert p.likes == 1200 and p.comments_count == 340
    assert p.views is None and p.shares is None


def test_map_post_item_video_uses_play_count_and_hidden_likes_become_none():
    p = map_post_item(ig_post("VID", kind="Video", likes=-1), account="kompascom")
    assert p.media_type == "video"
    assert p.views == 15000
    assert p.likes is None


def test_map_comment_item_reads_reply_count():
    c = map_comment_item(ig_comment("c1", "ABC", likes=7, replies=12), post_id="ABC")
    assert c.platform == "instagram" and c.post_id == "ABC" and c.comment_id == "c1"
    assert c.likes == 7 and c.replies_count == 12 and c.is_reply is False
    assert c.created_at == datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)


# ---- posts ------------------------------------------------------------------------
def test_fetch_posts_sends_profile_url_date_filter_and_limit():
    items = [ig_post("pinned", 60, pinned=True), ig_post("a", 1), ig_post("b", 2), ig_post("old", 20)]
    scraper, runner = make_scraper({POSTS_ACTOR: items})
    since = NOW - timedelta(days=7)

    posts = list(scraper.fetch_posts("kompascom", since, max_posts=30))

    assert [p.post_id for p in posts] == ["a", "b"]
    actor, payload = runner.calls[0]
    assert actor == POSTS_ACTOR
    assert payload["directUrls"] == ["https://www.instagram.com/kompascom/"]
    assert payload["resultsType"] == "posts"
    assert payload["resultsLimit"] == 30
    assert payload["onlyPostsNewerThan"] == "2026-11-17"


def test_fetch_posts_without_since_omits_date_filter_and_respects_max():
    scraper, runner = make_scraper({POSTS_ACTOR: [ig_post(f"p{i}") for i in range(10)]})
    posts = list(scraper.fetch_posts("kompascom", None, max_posts=3))
    assert len(posts) == 3
    assert "onlyPostsNewerThan" not in runner.calls[0][1]


def test_error_item_for_missing_profile_raises_account_not_found():
    scraper, _ = make_scraper({POSTS_ACTOR: [{"url": "https://www.instagram.com/x/", "error": "not_found", "errorDescription": "Page not found"}]})
    with pytest.raises(AccountNotFound):
        list(scraper.fetch_posts("x", None, 10))


def test_other_error_item_raises_scraper_error():
    scraper, _ = make_scraper({POSTS_ACTOR: [{"error": "no_items", "errorDescription": "Empty or private data for provided input"}]})
    with pytest.raises(ScraperError, match="Empty or private"):
        list(scraper.fetch_posts("kompascom", None, 10))


def test_missing_token_gives_actionable_error():
    scraper, _ = make_scraper({POSTS_ACTOR: []}, token=None)
    with pytest.raises(ScrapeBlocked, match="spi apify connect"):
        list(scraper.fetch_posts("kompascom", None, 10))


# ---- comments ---------------------------------------------------------------------
def test_comments_are_batched_once_for_all_posts_and_matched_by_shortcode():
    posts_items = [ig_post("a"), ig_post("b")]
    comments = [ig_comment("1", "a", likes=9, replies=4), ig_comment("2", "a"), ig_comment("3", "a"), ig_comment("4", "b")]
    scraper, runner = make_scraper({POSTS_ACTOR: posts_items, COMMENTS_ACTOR: comments})

    gen = scraper.fetch_posts("kompascom", None, 10)
    first = next(gen)
    got_a = list(scraper.fetch_comments(first, limit=2))
    second = next(gen)
    got_b = list(scraper.fetch_comments(second, limit=2))

    assert [c.comment_id for c in got_a] == ["1", "2"]
    assert got_a[0].replies_count == 4
    assert [c.comment_id for c in got_b] == ["4"]
    calls = [c for c in runner.calls if c[0] == COMMENTS_ACTOR]
    assert len(calls) == 1
    payload = calls[0][1]
    assert set(payload["directUrls"]) == {"https://www.instagram.com/p/a/", "https://www.instagram.com/p/b/"}
    assert payload["resultsLimit"] == 2
    assert payload["includeNestedComments"] is False


def test_comment_error_items_are_skipped():
    comments = [{"error": "restricted", "errorDescription": "x", "postUrl": "https://www.instagram.com/p/a/"}, ig_comment("1", "a")]
    scraper, _ = make_scraper({POSTS_ACTOR: [ig_post("a")], COMMENTS_ACTOR: comments})
    post = next(scraper.fetch_posts("kompascom", None, 10))
    assert [c.comment_id for c in scraper.fetch_comments(post, limit=5)] == ["1"]


def test_fetch_comments_for_unknown_post_runs_single_request():
    scraper, runner = make_scraper({COMMENTS_ACTOR: [ig_comment("1", "zzz")]})
    post = Post(platform="instagram", account="x", post_id="zzz", url="https://www.instagram.com/p/zzz/", created_at=NOW)
    assert [c.comment_id for c in scraper.fetch_comments(post, limit=5)] == ["1"]
    assert runner.calls[0][1]["directUrls"] == [post.url]


# ---- HTTP dipakai bersama dengan TikTok ---------------------------------------------
def test_http_flow_uses_only_the_job_owners_token():
    from tests.apify_fakes import FakeApify

    fake = FakeApify()
    fake.add_account("apify_api_SECRET", "akun_andi")
    fake.items[POSTS_ACTOR] = [ig_post("a")]
    settings = {
        "scrape": ScrapeConfig(),
        "storage": StorageConfig(),
        "env": {},
        "apify": ApifyAccess(token="apify_api_SECRET", user_id=1, credential_id=1),
    }
    scraper = InstagramApifyScraper(settings, http_post=fake.post, http_get=fake.get, retry_sleep=lambda s: None)

    posts = list(scraper.fetch_posts("kompascom", None, 5))

    assert [p.post_id for p in posts] == ["a"]
    assert fake.actor_runs_by("apify_api_SECRET") == ["apify~instagram-scraper"]
    assert fake.tokens_used() == {"apify_api_SECRET"}
    assert all("apify_api_SECRET" not in url for _, url, _ in fake.calls)
