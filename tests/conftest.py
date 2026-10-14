from datetime import datetime, timezone

import pytest

from spi.models import Comment, Post
from spi.storage import Storage


@pytest.fixture
def db() -> Storage:
    storage = Storage(":memory:")
    storage.init_db()
    yield storage
    storage.close()


def make_post(**overrides) -> Post:
    data = dict(
        platform="instagram",
        account="kompascom",
        post_id="ABC123",
        url="https://www.instagram.com/p/ABC123/",
        created_at=datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc),
        caption="Harga beras naik #beras",
        hashtags=["beras"],
        media_type="image",
        likes=1200,
        comments_count=340,
    )
    data.update(overrides)
    return Post(**data)


def make_comment(**overrides) -> Comment:
    data = dict(
        platform="instagram",
        post_id="ABC123",
        comment_id="c1",
        text="setuju",
        created_at=datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc),
        likes=5,
    )
    data.update(overrides)
    return Comment(**data)
