from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from instaloader import exceptions as ilx

from spi.config import ScrapeConfig, StorageConfig
from spi.scrapers.base import AccountNotFound, RateLimited, ScrapeBlocked
from spi.scrapers.instagram_instaloader import InstaloaderScraper, map_comment, map_post

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def il_post(shortcode="ABC", days_ago=1, typename="GraphImage", comments=()):
    date_utc = (NOW - timedelta(days=days_ago)).replace(tzinfo=None)  # instaloader memberi naive UTC
    return SimpleNamespace(
        shortcode=shortcode,
        mediaid=123,
        owner_username="kompascom",
        date_utc=date_utc,
        caption="Harga beras naik #beras #Ekonomi",
        caption_hashtags=["beras", "ekonomi"],
        likes=1000,
        comments=50,
        video_view_count=None if typename != "GraphVideo" else 9000,
        typename=typename,
        is_pinned=False,
        get_comments=lambda: iter(comments),
    )


def il_comment(cid, text, likes=3, answers=()):
    return SimpleNamespace(
        id=cid,
        text=text,
        created_at_utc=datetime(2026, 10, 13, 10, 0),
        likes_count=likes,
        owner=SimpleNamespace(username="x"),
        answers=iter(answers),
    )


def make_scraper(posts, raise_on_profile=None, raise_on_posts=None):
    class FakeProfile:
        def get_posts(self):
            if raise_on_posts:
                raise raise_on_posts
            return iter(posts)

    def profile_factory(context, username):
        if raise_on_profile:
            raise raise_on_profile
        return FakeProfile()

    settings = {
        "scrape": ScrapeConfig(older_posts_tolerance=1),
        "storage": StorageConfig(),
        "env": {"IG_USERNAME": "akun_sekunder"},
    }
    return InstaloaderScraper(
        settings,
        loader_factory=lambda: SimpleNamespace(context=object()),
        profile_factory=profile_factory,
    )


def test_map_post_normalizes_fields():
    post = map_post(il_post(typename="GraphSidecar"), account="kompascom")
    assert post.platform == "instagram"
    assert post.post_id == "ABC"
    assert post.url == "https://www.instagram.com/p/ABC/"
    assert post.created_at.tzinfo == timezone.utc
    assert post.hashtags == ["beras", "ekonomi"]
    assert post.media_type == "carousel"
    assert post.likes == 1000 and post.comments_count == 50
    assert post.views is None


def test_map_post_video_has_views():
    post = map_post(il_post(typename="GraphVideo"), account="kompascom")
    assert post.media_type == "video"
    assert post.views == 9000


def test_map_comment_and_reply():
    c = map_comment(il_comment(11, "setuju"), post_id="ABC")
    assert c.comment_id == "11" and c.is_reply is False and c.likes == 3
    r = map_comment(il_comment(12, "tidak"), post_id="ABC", parent_id="11")
    assert r.is_reply is True and r.parent_comment_id == "11"


def test_fetch_posts_stops_after_tolerated_older_posts():
    posts = [
        il_post("pinned_old", days_ago=30),  # pinned, lebih tua: ditoleransi
        il_post("new1", days_ago=1),
        il_post("new2", days_ago=2),
        il_post("old1", days_ago=20),  # toleransi habis -> berhenti
        il_post("old2", days_ago=21),
        il_post("would_not_reach", days_ago=1),
    ]
    scraper = make_scraper(posts)
    since = NOW - timedelta(days=7)

    got = [p.post_id for p in scraper.fetch_posts("kompascom", since, max_posts=100)]

    assert got == ["new1", "new2"]


def test_fetch_posts_respects_max_posts():
    posts = [il_post(f"p{i}", days_ago=1) for i in range(10)]
    scraper = make_scraper(posts)
    got = list(scraper.fetch_posts("kompascom", NOW - timedelta(days=7), max_posts=3))
    assert len(got) == 3


def test_fetch_comments_includes_replies_and_respects_limit():
    comments = [
        il_comment(1, "a", answers=[il_comment(2, "balas a")]),
        il_comment(3, "b"),
        il_comment(4, "c"),
    ]
    scraper = make_scraper([il_post("ABC", comments=comments)])
    post = next(scraper.fetch_posts("kompascom", None, 10))

    got = list(scraper.fetch_comments(post, limit=3))

    assert [c.comment_id for c in got] == ["1", "2", "3"]
    assert got[1].is_reply and got[1].parent_comment_id == "1"


@pytest.mark.parametrize(
    "exc, expected",
    [
        (ilx.TooManyRequestsException("429"), RateLimited),
        (ilx.ConnectionException("HTTP error code 429 Too Many Requests"), RateLimited),
        (ilx.LoginRequiredException("login"), ScrapeBlocked),
        (ilx.QueryReturnedBadRequestException("checkpoint_required"), ScrapeBlocked),
        (ilx.ProfileNotExistsException("x"), AccountNotFound),
        (ilx.PrivateProfileNotFollowedException("x"), AccountNotFound),
    ],
)
def test_instaloader_exceptions_are_translated(exc, expected):
    scraper = make_scraper([], raise_on_profile=exc)
    with pytest.raises(expected):
        list(scraper.fetch_posts("kompascom", None, 10))


def test_missing_session_file_gives_actionable_error(tmp_path):
    settings = {
        "scrape": ScrapeConfig(),
        "storage": StorageConfig(ig_session_file=str(tmp_path / "tidak_ada")),
        "env": {"IG_USERNAME": "akun_sekunder"},
    }
    scraper = InstaloaderScraper(settings)
    with pytest.raises(ScrapeBlocked, match="spi login"):
        list(scraper.fetch_posts("kompascom", None, 10))
