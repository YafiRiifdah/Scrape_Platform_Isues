"""Adapter Instagram berbasis Instaloader (pendekatan A).

Membutuhkan sesi login akun sekunder yang disimpan lewat `spi login`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from instaloader import Instaloader, Profile
from instaloader import Post as IlPost
from instaloader import exceptions as ilx

from spi.models import Comment, Post
from spi.scrapers.base import AccountNotFound, RateLimited, ScrapeBlocked, ScraperError, should_stop

log = logging.getLogger("spi.scrapers.instagram")

_MEDIA_TYPES = {"GraphImage": "image", "GraphVideo": "video", "GraphSidecar": "carousel"}


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def map_post(p: Any, account: str) -> Post:
    """Petakan instaloader.Post (atau objek serupa) ke model Post."""
    typename = getattr(p, "typename", "")
    return Post(
        platform="instagram",
        account=account,
        post_id=p.shortcode,
        url=f"https://www.instagram.com/p/{p.shortcode}/",
        created_at=_utc(p.date_utc),
        caption=p.caption or "",
        hashtags=[h.lower() for h in (p.caption_hashtags or [])],
        media_type=_MEDIA_TYPES.get(typename, "unknown"),
        likes=p.likes,
        comments_count=p.comments,
        shares=None,
        views=p.video_view_count if typename == "GraphVideo" else None,
    )


def map_comment(c: Any, post_id: str, parent_id: Optional[str] = None) -> Comment:
    return Comment(
        platform="instagram",
        post_id=post_id,
        comment_id=str(c.id),
        text=c.text or "",
        created_at=_utc(c.created_at_utc) if c.created_at_utc else None,
        likes=getattr(c, "likes_count", None),
        is_reply=parent_id is not None,
        parent_comment_id=parent_id,
    )


@contextmanager
def translate_errors():
    """Ubah exception Instaloader menjadi exception yang dipahami runner."""
    try:
        yield
    except ilx.TooManyRequestsException as e:
        raise RateLimited(str(e)) from e
    except ilx.ConnectionException as e:
        msg = str(e)
        if "429" in msg or "Too Many Requests" in msg or "Please wait" in msg:
            raise RateLimited(msg) from e
        raise ScraperError(f"koneksi gagal: {msg}") from e
    except (ilx.LoginRequiredException, ilx.QueryReturnedBadRequestException, ilx.QueryReturnedForbiddenException) as e:
        raise ScrapeBlocked(
            f"Instagram memblokir akses ({type(e).__name__}: {e}). Login ulang dengan `spi login` "
            "atau selesaikan challenge di browser untuk akun sekunder."
        ) from e
    except (ilx.ProfileNotExistsException, ilx.PrivateProfileNotFollowedException, ilx.QueryReturnedNotFoundException) as e:
        raise AccountNotFound(str(e)) from e


class InstaloaderScraper:
    platform = "instagram"

    def __init__(
        self,
        settings: dict[str, Any],
        loader_factory: Optional[Callable[[], Any]] = None,
        profile_factory: Optional[Callable[[Any, str], Any]] = None,
    ) -> None:
        self.settings = settings
        self._loader_factory = loader_factory or self._default_loader
        self._profile_factory = profile_factory or (lambda ctx, username: Profile.from_username(ctx, username))
        self._loader: Any = None
        self._post_cache: dict[str, Any] = {}

    # ---- setup -----------------------------------------------------------
    def _default_loader(self) -> Instaloader:
        env = self.settings.get("env", {})
        username = env.get("IG_USERNAME")
        session_file = self.settings["storage"].ig_session_file
        if not username:
            raise ScrapeBlocked("IG_USERNAME belum diisi di .env; jalankan `spi login` setelah mengisinya.")
        if not Path(session_file).exists():
            raise ScrapeBlocked(
                f"file sesi Instagram {session_file} tidak ditemukan; jalankan `spi login` dulu."
            )
        loader = Instaloader(
            sleep=True,
            quiet=True,
            request_timeout=60.0,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
            max_connection_attempts=2,
        )
        with translate_errors():
            loader.load_session_from_file(username, session_file)
        log.info("sesi Instagram dimuat untuk %s", username)
        return loader

    @property
    def loader(self) -> Any:
        if self._loader is None:
            self._loader = self._loader_factory()
        return self._loader

    # ---- scraping --------------------------------------------------------
    def fetch_posts(self, username: str, since: Optional[datetime], max_posts: int) -> Iterator[Post]:
        tolerance = self.settings["scrape"].older_posts_tolerance
        older_seen = 0
        yielded = 0
        with translate_errors():
            profile = self._profile_factory(self.loader.context, username)
            for il_post in profile.get_posts():
                post = map_post(il_post, username)
                stop, older_seen = should_stop(post.created_at, since, older_seen, tolerance)
                if stop:
                    break
                if older_seen:
                    continue  # post lebih tua (kemungkinan pinned), dilewati
                self._post_cache[post.post_id] = il_post
                yield post
                yielded += 1
                if yielded >= max_posts:
                    break

    def _il_post(self, post: Post) -> Any:
        cached = self._post_cache.get(post.post_id)
        if cached is not None:
            return cached
        with translate_errors():
            return IlPost.from_shortcode(self.loader.context, post.post_id)

    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]:
        if limit <= 0:
            return
        il_post = self._il_post(post)
        n = 0
        with translate_errors():
            for c in il_post.get_comments():
                yield map_comment(c, post.post_id)
                n += 1
                if n >= limit:
                    return
                for a in c.answers or ():
                    yield map_comment(a, post.post_id, parent_id=str(c.id))
                    n += 1
                    if n >= limit:
                        return

    def close(self) -> None:
        self._post_cache.clear()
        self._loader = None
