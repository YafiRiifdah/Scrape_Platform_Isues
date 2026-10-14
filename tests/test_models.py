from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from spi.models import Comment, Post


def make_post(**overrides) -> Post:
    data = dict(
        platform="instagram",
        account="kompascom",
        post_id="ABC123",
        url="https://www.instagram.com/p/ABC123/",
        created_at=datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc),
        caption="Harga beras naik #beras #ekonomi",
        hashtags=["beras", "ekonomi"],
        media_type="image",
        likes=1200,
        comments_count=340,
        shares=None,
        views=None,
    )
    data.update(overrides)
    return Post(**data)


def test_post_defaults_optional_metrics_to_none():
    post = Post(
        platform="tiktok",
        account="detikcom",
        post_id="1",
        url="https://www.tiktok.com/@detikcom/video/1",
        created_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
        caption="",
    )
    assert post.hashtags == []
    assert post.likes is None
    assert post.views is None
    assert post.raw is None


def test_post_rejects_unknown_platform():
    with pytest.raises(ValidationError):
        make_post(platform="twitter")


def test_post_created_at_is_converted_to_utc():
    from datetime import timedelta

    wib = timezone(timedelta(hours=7))
    post = make_post(created_at=datetime(2026, 10, 10, 15, 0, tzinfo=wib))
    assert post.created_at == datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)


def test_post_naive_datetime_is_assumed_utc():
    post = make_post(created_at=datetime(2026, 10, 10, 8, 0))
    assert post.created_at.tzinfo == timezone.utc


def test_comment_defaults():
    c = Comment(platform="instagram", post_id="ABC123", comment_id="c1", text="setuju")
    assert c.created_at is None
    assert c.likes is None
    assert c.is_reply is False
    assert c.parent_comment_id is None
