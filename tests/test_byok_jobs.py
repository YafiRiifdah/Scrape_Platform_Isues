"""Eksekusi job per pengguna: setiap job hanya memakai token Apify milik pemiliknya."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from spi import crypto
from spi.config import AppConfig
from spi.credentials import CredentialService
from spi.runner import run_scrape
from spi.scrapers import apify_base
from tests.apify_fakes import FakeApify

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
TOKEN_A = "apify_api_ANDI_secret_AAAA"
TOKEN_A2 = "apify_api_ANDI_baru_A2A2"
TOKEN_B = "apify_api_BUDI_secret_BBBB"
ENV_TOKEN = "apify_api_ENV_lama_EEEE"
POSTS_ACTOR = "clockworks~tiktok-scraper"
COMMENTS_ACTOR = "clockworks~tiktok-comments-scraper"


def tiktok_item(vid):
    created = NOW - timedelta(days=1)
    return {
        "id": vid, "text": "Banjir #banjir", "createTimeISO": created.isoformat().replace("+00:00", "Z"),
        "diggCount": 10, "shareCount": 1, "playCount": 100, "commentCount": 2, "hashtags": [{"name": "banjir"}],
        "webVideoUrl": f"https://www.tiktok.com/@kompascom/video/{vid}", "authorMeta": {"name": "kompascom"},
    }


def tiktok_comment(cid, vid):
    return {
        "cid": cid, "text": "setuju", "diggCount": 1, "createTimeISO": "2026-11-23T10:00:00.000Z",
        "videoWebUrl": f"https://www.tiktok.com/@kompascom/video/{vid}", "replyCommentTotal": 0,
    }


@pytest.fixture
def fake(monkeypatch):
    f = FakeApify()
    f.status_sequence = ["SUCCEEDED"]
    f.add_account(TOKEN_A, "akun_andi")
    f.add_account(TOKEN_A2, "akun_andi_baru")
    f.add_account(TOKEN_B, "akun_budi")
    f.add_account(ENV_TOKEN, "akun_env")
    f.items[POSTS_ACTOR] = [tiktok_item("v1"), tiktok_item("v2")]
    f.items[COMMENTS_ACTOR] = [tiktok_comment("c1", "v1"), tiktok_comment("c2", "v2")]
    monkeypatch.setattr(requests, "post", f.post)
    monkeypatch.setattr(requests, "get", f.get)
    monkeypatch.setattr(apify_base, "POLL_PAUSE_SECONDS", 0)
    return f


@pytest.fixture
def env():
    return {crypto.MASTER_KEY_ENV: crypto.generate_master_key()}


@pytest.fixture
def cfg():
    return AppConfig.model_validate(
        {
            "accounts": [{"platform": "tiktok", "username": "kompascom", "label": "Kompas"}],
            "scrape": {"delay_between_requests": [0, 0], "delay_between_accounts": [0, 0], "rate_limit_sleep_minutes": 0},
        }
    )


def scrape(cfg, db, env, user_id, **kw):
    return run_scrape(cfg, db, env, sleep=lambda s: None, now=NOW, user_id=user_id, **kw)


def run_credentials(db, run_id):
    return {r["account_key"]: r["credential_id"] for r in db.conn.execute("SELECT * FROM scrape_runs WHERE run_id = ?", (run_id,))}


# ---- kepemilikan kredensial per job ----------------------------------------------------------
def test_each_job_runs_with_its_owners_token_only(db, fake, env, cfg):
    a, b = db.create_user("andi"), db.create_user("budi")
    svc = CredentialService(db, env)
    cred_a = svc.connect(a, TOKEN_A)["credential_id"]
    cred_b = svc.connect(b, TOKEN_B)["credential_id"]
    fake.calls.clear()

    run_a, summary_a = scrape(cfg, db, env, a)
    assert summary_a["tiktok:kompascom"]["status"] == "ok"
    assert summary_a["tiktok:kompascom"]["posts_found"] == 2
    assert fake.tokens_used() == {TOKEN_A}

    fake.calls.clear()
    run_b, summary_b = scrape(cfg, db, env, b)
    assert summary_b["tiktok:kompascom"]["status"] == "ok"
    assert fake.tokens_used() == {TOKEN_B}

    assert fake.actor_runs_by(TOKEN_A) == [POSTS_ACTOR, COMMENTS_ACTOR]
    assert fake.actor_runs_by(TOKEN_B) == [POSTS_ACTOR, COMMENTS_ACTOR]
    assert db.run_owner(run_a) == a and db.run_owner(run_b) == b
    assert run_credentials(db, run_a) == {"tiktok:kompascom": cred_a}
    assert run_credentials(db, run_b) == {"tiktok:kompascom": cred_b}


def test_every_execution_is_recorded_for_the_originating_user(db, fake, env, cfg):
    a, b = db.create_user("andi"), db.create_user("budi")
    svc = CredentialService(db, env)
    cred_a = svc.connect(a, TOKEN_A)["credential_id"]
    svc.connect(b, TOKEN_B)

    run_a, _ = scrape(cfg, db, env, a)

    rows = db.list_executions(a)
    assert {r["actor"] for r in rows} == {POSTS_ACTOR, COMMENTS_ACTOR}
    assert all(r["user_id"] == a and r["credential_id"] == cred_a and r["run_id"] == run_a for r in rows)
    assert all(r["status"] == "SUCCEEDED" and r["apify_run_id"] and r["items"] == 2 for r in rows)
    assert db.list_executions(b) == []
    assert [r["run_id"] for r in db.list_runs(a)] == [run_a] and db.list_runs(b) == []


# ---- kuota habis: tidak ada perpindahan token -----------------------------------------------------
def test_exhausted_quota_fails_the_job_and_never_uses_another_users_token(db, fake, env, cfg):
    a, b = db.create_user("andi"), db.create_user("budi")
    svc = CredentialService(db, env)
    svc.connect(a, TOKEN_A)
    svc.connect(b, TOKEN_B)  # Budi masih punya kredit penuh
    fake.accounts[TOKEN_A]["no_credits"] = True
    fake.calls.clear()

    _, summary = scrape(cfg, db, {**env, "APIFY_TOKEN": ENV_TOKEN}, a)

    row = summary["tiktok:kompascom"]
    assert row["status"] == "failed" and row["posts_found"] == 0
    assert "ApifyInsufficientCredits" in row["error"]
    assert fake.tokens_used() == {TOKEN_A}
    assert fake.started == []  # tidak ada run yang berhasil dimulai dengan token mana pun
    execution = db.list_executions(a)[0]
    assert execution["error_kind"] == "insufficient_credits" and execution["user_id"] == a
    assert db.list_executions(b) == []


def test_token_rejected_by_apify_fails_the_job_without_fallback(db, fake, env, cfg):
    a, b = db.create_user("andi"), db.create_user("budi")
    svc = CredentialService(db, env)
    svc.connect(a, TOKEN_A)
    svc.connect(b, TOKEN_B)
    del fake.accounts[TOKEN_A]  # token dicabut di sisi Apify
    fake.calls.clear()

    _, summary = scrape(cfg, db, env, a)

    assert summary["tiktok:kompascom"]["status"] == "failed"
    assert "ApifyInvalidToken" in summary["tiktok:kompascom"]["error"]
    assert fake.tokens_used() == {TOKEN_A}
    assert db.list_executions(a)[0]["error_kind"] == "invalid_token"


def test_job_error_message_never_contains_the_token(db, fake, env, cfg):
    a = db.create_user("andi")
    CredentialService(db, env).connect(a, TOKEN_A)
    fake.accounts[TOKEN_A]["forbidden"] = True

    _, summary = scrape(cfg, db, env, a)

    assert "ApifyPermissionDenied" in summary["tiktok:kompascom"]["error"]
    assert TOKEN_A not in "\n".join(db.conn.iterdump())


# ---- pengguna tanpa kredensial ------------------------------------------------------------------------
def test_user_without_credential_cannot_run_apify_jobs_on_others_tokens(db, fake, env, cfg):
    a, b = db.create_user("andi"), db.create_user("budi")
    CredentialService(db, env).connect(a, TOKEN_A)
    fake.calls.clear()

    run_b, summary = scrape(cfg, db, {**env, "APIFY_TOKEN": ENV_TOKEN}, b)

    assert summary["tiktok:kompascom"]["status"] == "failed"
    assert "spi apify connect" in summary["tiktok:kompascom"]["error"]
    assert fake.calls == []  # tidak ada satu pun panggilan ke Apify
    assert run_credentials(db, run_b) == {"tiktok:kompascom": None}


def test_stored_credential_without_master_key_fails_cleanly(db, fake, env, cfg):
    a = db.create_user("andi")
    CredentialService(db, env).connect(a, TOKEN_A)
    fake.calls.clear()

    _, summary = scrape(cfg, db, {}, a)

    assert summary["tiktok:kompascom"]["status"] == "failed"
    assert "spi key init" in summary["tiktok:kompascom"]["error"]
    assert fake.calls == []


# ---- kompatibilitas cara lama ---------------------------------------------------------------------------
def test_default_user_keeps_working_with_legacy_env_token(db, fake, cfg):
    run_id, summary = run_scrape(cfg, db, {"APIFY_TOKEN": ENV_TOKEN}, sleep=lambda s: None, now=NOW)

    assert summary["tiktok:kompascom"]["status"] == "ok"
    assert fake.tokens_used() == {ENV_TOKEN}
    assert db.run_owner(run_id) == db.default_user_id()
    assert run_credentials(db, run_id) == {"tiktok:kompascom": None}
    executions = db.list_executions(db.default_user_id())
    assert len(executions) == 2 and all(e["credential_id"] is None for e in executions)


# ---- melanjutkan run ----------------------------------------------------------------------------------------
def test_resume_is_refused_for_a_run_owned_by_someone_else(db, fake, env, cfg):
    a, b = db.create_user("andi"), db.create_user("budi")
    svc = CredentialService(db, env)
    svc.connect(a, TOKEN_A)
    svc.connect(b, TOKEN_B)
    fake.accounts[TOKEN_A]["rate_limited"] = True
    run_a, first = scrape(cfg, db, env, a)
    assert first["tiktok:kompascom"]["status"] == "rate_limited"
    fake.calls.clear()

    with pytest.raises(ValueError, match="tidak ditemukan untuk pengguna ini"):
        scrape(cfg, db, env, b, resume_run_id=run_a)
    with pytest.raises(ValueError, match="tidak ditemukan untuk pengguna ini"):
        scrape(cfg, db, env, a, resume_run_id="run-yang-tidak-ada")

    assert fake.calls == []
    assert db.run_summary(run_a)["tiktok:kompascom"]["status"] == "rate_limited"


def test_owner_can_resume_and_the_credential_actually_used_is_recorded(db, fake, env, cfg):
    a = db.create_user("andi")
    svc = CredentialService(db, env)
    old = svc.connect(a, TOKEN_A)["credential_id"]
    fake.accounts[TOKEN_A]["rate_limited"] = True
    run_a, _ = scrape(cfg, db, env, a)
    assert run_credentials(db, run_a) == {"tiktok:kompascom": old}

    new = svc.connect(a, TOKEN_A2)["credential_id"]  # pengguna mengganti tokennya sendiri
    fake.calls.clear()
    resumed, summary = scrape(cfg, db, env, a, resume_run_id=run_a)

    assert resumed == run_a and summary["tiktok:kompascom"]["status"] == "ok"
    assert fake.tokens_used() == {TOKEN_A2}
    assert run_credentials(db, run_a) == {"tiktok:kompascom": new}
    by_cred = {e["credential_id"] for e in db.list_executions(a) if e["status"] == "SUCCEEDED"}
    assert by_cred == {new}


def test_run_status_is_monitored_until_finished(db, fake, env, cfg):
    a = db.create_user("andi")
    CredentialService(db, env).connect(a, TOKEN_A)
    fake.status_sequence = ["READY", "RUNNING", "SUCCEEDED"]
    fake.calls.clear()

    _, summary = scrape(cfg, db, env, a)

    assert summary["tiktok:kompascom"]["status"] == "ok"
    polls = [url for method, url, _ in fake.calls if method == "GET" and "/actor-runs/" in url]
    assert len(polls) == 4  # dua kali pantau untuk masing-masing dari dua actor
    assert fake.tokens_used() == {TOKEN_A}
