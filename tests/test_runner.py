from datetime import datetime, timezone

from spi.config import AppConfig
from spi.runner import run_scrape
from spi.scrapers import registry
from spi.scrapers.base import RateLimited, ScrapeBlocked
from tests.conftest import make_comment, make_post


class FakeScraper:
    """Scraper palsu: perilaku dikendalikan lewat dict `behaviour` per username."""

    platform = "instagram"
    instances: list["FakeScraper"] = []

    def __init__(self, settings):
        self.settings = settings
        self.closed = False
        FakeScraper.instances.append(self)

    def fetch_posts(self, username, since, max_posts):
        action = self.settings["behaviour"].get(username, "ok")
        if action == "rate_limited_once":
            self.settings["behaviour"][username] = "ok"
            raise RateLimited("429")
        if action == "rate_limited_always":
            raise RateLimited("429")
        if action == "blocked":
            raise ScrapeBlocked("checkpoint")
        if action == "crash":
            raise RuntimeError("boom")
        yield make_post(account=username, post_id=f"{username}-1")
        yield make_post(account=username, post_id=f"{username}-2")

    def fetch_comments(self, post, limit):
        for i in range(min(limit, 3)):
            yield make_comment(post_id=post.post_id, comment_id=f"{post.post_id}-c{i}")

    def close(self):
        self.closed = True


def make_config(usernames):
    return AppConfig.model_validate(
        {
            "accounts": [
                {"platform": "instagram", "username": u, "label": u, "adapter": "fake"} for u in usernames
            ],
            "scrape": {
                "delay_between_requests": [0, 0],
                "delay_between_accounts": [0, 0],
                "rate_limit_sleep_minutes": 0,
                "max_comments_per_post": 2,
            },
        }
    )


def _install_fake(monkeypatch, behaviour):
    FakeScraper.instances = []
    settings = {"behaviour": behaviour}
    monkeypatch.setattr(
        registry,
        "build",
        lambda adapter, platform, cfg, env: FakeScraper(settings),
    )
    return settings


def test_run_scrape_stores_posts_and_comments_and_closes_scraper(db, monkeypatch):
    _install_fake(monkeypatch, {})
    cfg = make_config(["kompascom"])

    run_id, summary = run_scrape(cfg, db, env={}, sleep=lambda s: None)

    posts = db.get_posts_since(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert {p.post_id for p in posts} == {"kompascom-1", "kompascom-2"}
    assert db.count_comments("instagram", "kompascom-1") == 2  # dibatasi max_comments_per_post
    assert summary["instagram:kompascom"]["status"] == "ok"
    assert summary["instagram:kompascom"]["posts_found"] == 2
    assert summary["instagram:kompascom"]["comments_found"] == 4
    assert all(s.closed for s in FakeScraper.instances)


def test_blocked_account_does_not_stop_other_accounts(db, monkeypatch):
    _install_fake(monkeypatch, {"a": "blocked", "c": "crash"})
    cfg = make_config(["a", "b", "c"])

    _, summary = run_scrape(cfg, db, env={}, sleep=lambda s: None)

    assert summary["instagram:a"]["status"] == "failed"
    assert "checkpoint" in summary["instagram:a"]["error"]
    assert summary["instagram:b"]["status"] == "ok"
    assert summary["instagram:c"]["status"] == "failed"
    assert "boom" in summary["instagram:c"]["error"]


def test_rate_limited_account_is_retried_once_after_sleep(db, monkeypatch):
    _install_fake(monkeypatch, {"a": "rate_limited_once"})
    cfg = make_config(["a"])
    cfg.scrape.rate_limit_sleep_minutes = 15
    slept = []

    _, summary = run_scrape(cfg, db, env={}, sleep=slept.append)

    assert summary["instagram:a"]["status"] == "ok"
    assert 15 * 60 in slept


def test_persistent_rate_limit_marks_account_rate_limited(db, monkeypatch):
    _install_fake(monkeypatch, {"a": "rate_limited_always"})
    cfg = make_config(["a"])

    _, summary = run_scrape(cfg, db, env={}, sleep=lambda s: None)

    assert summary["instagram:a"]["status"] == "rate_limited"


def test_only_filter_limits_accounts(db, monkeypatch):
    _install_fake(monkeypatch, {})
    cfg = make_config(["a", "b"])

    _, summary = run_scrape(cfg, db, env={}, only=["instagram:b"], sleep=lambda s: None)

    assert list(summary) == ["instagram:b"]


def test_resume_only_processes_pending_and_rate_limited_accounts(db, monkeypatch):
    settings = _install_fake(monkeypatch, {"b": "rate_limited_always", "c": "blocked"})
    cfg = make_config(["a", "b", "c"])
    run_id, first = run_scrape(cfg, db, env={}, sleep=lambda s: None)
    assert first["instagram:b"]["status"] == "rate_limited"

    settings["behaviour"]["b"] = "ok"
    resumed_id, second = run_scrape(cfg, db, env={}, resume_run_id=run_id, sleep=lambda s: None)

    assert resumed_id == run_id
    assert list(second) == ["instagram:b"]
    assert second["instagram:b"]["status"] == "ok"
