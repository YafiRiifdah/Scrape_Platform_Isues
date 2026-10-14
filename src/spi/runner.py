"""Orkestrasi scraping per akun: jeda acak, retry rate limit, checkpoint, dan ringkasan."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from spi.config import AccountConfig, AppConfig
from spi.scrapers import registry
from spi.scrapers.base import RateLimited, ScraperError
from spi.storage import Storage

log = logging.getLogger("spi.runner")

SleepFn = Callable[[float], None]


def _select_accounts(
    cfg: AppConfig, db: Storage, only: Optional[list[str]], resume_run_id: Optional[str]
) -> tuple[str, list[AccountConfig]]:
    by_key = {a.key: a for a in cfg.accounts}
    if resume_run_id:
        keys = db.pending_accounts(resume_run_id, include_retryable=True)
        missing = [k for k in keys if k not in by_key]
        if missing:
            log.warning("akun di run %s tidak ada lagi di config, dilewati: %s", resume_run_id, missing)
        return resume_run_id, [by_key[k] for k in keys if k in by_key]

    accounts = cfg.accounts
    if only:
        wanted = set(only)
        unknown = wanted - set(by_key)
        if unknown:
            raise ValueError(f"akun tidak ada di config: {', '.join(sorted(unknown))}")
        accounts = [a for a in accounts if a.key in wanted]
    run_id = db.start_run([a.key for a in accounts])
    return run_id, accounts


def _scrape_account(
    acc: AccountConfig, cfg: AppConfig, db: Storage, env: dict[str, str], since: datetime, sleep: SleepFn
) -> tuple[int, int, Optional[str]]:
    """Ambil post dan komentar satu akun. Melempar ScraperError bila gagal."""
    scraper = registry.build(acc.adapter, acc.platform, cfg, env)
    posts_found = comments_found = comment_failures = 0
    last_comment_error: Optional[str] = None
    lo, hi = cfg.scrape.delay_between_requests
    try:
        for post in scraper.fetch_posts(acc.username, since, cfg.scrape.max_posts_per_account):
            db.upsert_post(post)
            posts_found += 1
            try:
                comments = list(scraper.fetch_comments(post, cfg.scrape.max_comments_per_post))
            except RateLimited:
                raise
            except Exception as e:  # noqa: BLE001 - post tetap tersimpan, komentar dilewati
                comment_failures += 1
                last_comment_error = f"{type(e).__name__}: {e}"
                log.warning("%s post %s: komentar gagal diambil (%s)", acc.key, post.post_id, last_comment_error)
                sleep(random.uniform(lo, hi))
                continue
            comments_found += db.upsert_comments(comments[: cfg.scrape.max_comments_per_post])
            log.info(
                "%s post %s: %d komentar tersimpan", acc.key, post.post_id, min(len(comments), cfg.scrape.max_comments_per_post)
            )
            sleep(random.uniform(lo, hi))
    finally:
        scraper.close()
    note = f"{comment_failures} post tanpa komentar (terakhir: {last_comment_error})" if comment_failures else None
    return posts_found, comments_found, note


def run_scrape(
    cfg: AppConfig,
    db: Storage,
    env: dict[str, str],
    only: Optional[list[str]] = None,
    resume_run_id: Optional[str] = None,
    sleep: SleepFn = time.sleep,
    now: Optional[datetime] = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Jalankan scraping untuk akun di config. Mengembalikan (run_id, ringkasan akun yang diproses)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=cfg.scrape.days)
    run_id, accounts = _select_accounts(cfg, db, only, resume_run_id)
    log.info("run %s: %d akun, post sejak %s", run_id, len(accounts), since.isoformat())

    processed: list[str] = []
    for idx, acc in enumerate(accounts):
        status, error = "ok", None
        posts_found = comments_found = 0
        attempts = 0
        while True:
            attempts += 1
            try:
                posts_found, comments_found, error = _scrape_account(acc, cfg, db, env, since, sleep)
                break
            except RateLimited as e:
                if attempts == 1:
                    wait = cfg.scrape.rate_limit_sleep_minutes * 60
                    log.warning("%s rate limited (%s); menunggu %.0f detik lalu coba lagi", acc.key, e, wait)
                    sleep(wait)
                    continue
                status, error = "rate_limited", str(e)
                log.error("%s tetap rate limited, ditandai untuk --resume", acc.key)
                break
            except ScraperError as e:
                status, error = "failed", f"{type(e).__name__}: {e}"
                log.error("%s gagal: %s", acc.key, error)
                break
            except Exception as e:  # noqa: BLE001 - satu akun tidak boleh mematikan run
                status, error = "failed", f"{type(e).__name__}: {e}"
                log.exception("%s error tak terduga", acc.key)
                break

        db.finish_account(run_id, acc.key, status, posts_found, comments_found, error)
        processed.append(acc.key)
        if idx < len(accounts) - 1:
            sleep(random.uniform(*cfg.scrape.delay_between_accounts))

    summary = db.run_summary(run_id)
    return run_id, {k: summary[k] for k in processed}
