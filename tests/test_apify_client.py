import logging

import pytest
import requests

from spi.scrapers.apify_base import (
    ApifyAccess,
    ApifyClient,
    ApifyConnectionError,
    ApifyError,
    ApifyInsufficientCredits,
    ApifyInvalidToken,
    ApifyPermissionDenied,
    ApifyRateLimited,
    ApifyRunFailed,
    ApifyTimeout,
    classify_status,
)
from spi.scrapers.base import RateLimited, ScrapeBlocked, ScraperError
from tests.apify_fakes import FakeApify

TOKEN = "apify_api_SECRET_andi_1234"
ACTOR = "vendor~some-actor"


def make_client(fake, token=TOKEN, **kw):
    records = []
    client = ApifyClient(token, http_post=fake.post, http_get=fake.get, retry_sleep=lambda s: None, on_execution=records.append, **kw)
    return client, records


@pytest.fixture
def fake():
    f = FakeApify()
    f.add_account(TOKEN, "akun_andi", used=2.0, limit=5.0)
    f.items[ACTOR] = [{"id": "1"}, {"id": "2"}]
    return f


# ---- alur eksekusi: mulai -> pantau -> ambil dataset ------------------------------------
def test_run_actor_starts_polls_and_fetches_dataset_with_the_same_token(fake):
    client, records = make_client(fake)

    items = client.run_actor(ACTOR, {"profiles": ["kompascom"]})

    assert items == [{"id": "1"}, {"id": "2"}]
    methods = [(m, url.split("/v2")[1]) for m, url, _ in fake.calls]
    assert methods == [
        ("POST", f"/acts/{ACTOR}/runs"),
        ("GET", "/actor-runs/run1"),
        ("GET", "/datasets/ds-run1/items"),
    ]
    assert fake.tokens_used() == {TOKEN}
    assert fake.started == [(TOKEN, ACTOR, {"profiles": ["kompascom"]})]


def test_token_travels_in_header_never_in_url(fake):
    client, _ = make_client(fake)
    client.run_actor(ACTOR, {})
    assert all(TOKEN not in url for _, url, _ in fake.calls)


def test_execution_is_recorded_with_status_items_and_usage(fake):
    client, records = make_client(fake)
    client.run_actor(ACTOR, {})

    assert len(records) == 1
    rec = records[0]
    assert rec["actor"] == ACTOR and rec["apify_run_id"] == "run1"
    assert rec["status"] == "SUCCEEDED" and rec["items"] == 2
    assert rec["usage_usd"] == pytest.approx(0.0125)
    assert rec["error_kind"] is None and rec["started_at"] and rec["finished_at"]
    assert TOKEN not in str(rec)


def test_failed_run_raises_run_failed_and_is_recorded(fake):
    fake.status_sequence = ["RUNNING", "FAILED"]
    client, records = make_client(fake)

    with pytest.raises(ApifyRunFailed, match="FAILED"):
        client.run_actor(ACTOR, {})

    assert records[0]["status"] == "FAILED" and records[0]["error_kind"] == "run_failed"
    assert not any("/datasets/" in url for _, url, _ in fake.calls)


def test_run_that_never_finishes_is_aborted_and_reported_as_timeout(fake):
    fake.status_sequence = ["RUNNING"]
    ticks = iter(range(0, 10_000, 100))
    client, records = make_client(fake, max_wait_seconds=250, clock=lambda: next(ticks))

    with pytest.raises(ApifyTimeout):
        client.run_actor(ACTOR, {})

    assert fake.aborted == ["run1"]
    assert records[0]["error_kind"] == "timeout"


def test_recorder_failure_does_not_break_the_run(fake):
    def broken(record):
        raise RuntimeError("database terkunci")

    client = ApifyClient(TOKEN, http_post=fake.post, http_get=fake.get, retry_sleep=lambda s: None, on_execution=broken)
    assert len(client.run_actor(ACTOR, {})) == 2


# ---- klasifikasi error -------------------------------------------------------------------
@pytest.mark.parametrize(
    "flag, exc, kind, parent",
    [
        ("rate_limited", ApifyRateLimited, "rate_limit", RateLimited),
        ("no_credits", ApifyInsufficientCredits, "insufficient_credits", ScrapeBlocked),
        ("quota_exceeded", ApifyInsufficientCredits, "insufficient_credits", ScrapeBlocked),
        ("forbidden", ApifyPermissionDenied, "permission", ScrapeBlocked),
    ],
)
def test_account_problems_are_distinguished(flag, exc, kind, parent):
    fake = FakeApify()
    fake.add_account(TOKEN, "akun_andi", **{flag: True})
    client, records = make_client(fake)

    with pytest.raises(exc) as info:
        client.run_actor(ACTOR, {})

    assert isinstance(info.value, parent) and isinstance(info.value, ApifyError)
    assert info.value.kind == kind
    assert records[0]["error_kind"] == kind and records[0]["apify_run_id"] is None
    assert TOKEN not in str(info.value)


def test_unknown_token_is_invalid_token(fake):
    client, records = make_client(fake, token="apify_api_SALAH_9999")
    with pytest.raises(ApifyInvalidToken) as info:
        client.run_actor(ACTOR, {})
    assert info.value.kind == "invalid_token"
    assert "apify_api_SALAH_9999" not in str(info.value)
    assert records[0]["error_kind"] == "invalid_token"


@pytest.mark.parametrize(
    "status, body, kind",
    [
        (200, "", None),
        (429, "", "rate_limit"),
        (401, "token-not-found", "invalid_token"),
        (402, "insufficient-funds", "insufficient_credits"),
        (403, "Monthly usage hard limit exceeded", "insufficient_credits"),
        (403, "Insufficient permissions for the Actor", "permission"),
        (500, "", "error"),
    ],
)
def test_classify_status(status, body, kind):
    assert classify_status(status, body) == kind


# ---- akun ---------------------------------------------------------------------------------
def test_whoami_returns_account_identity(fake):
    client, _ = make_client(fake)
    assert client.whoami() == {"apify_user_id": "id-akun_andi", "apify_username": "akun_andi"}


def test_whoami_with_bad_token_raises_invalid_token(fake):
    client, _ = make_client(fake, token="salah")
    with pytest.raises(ApifyInvalidToken):
        client.whoami()


def test_usage_reports_this_accounts_month(fake):
    client, _ = make_client(fake)
    usage = client.usage()
    assert usage == {"used_usd": 2.0, "limit_usd": 5.0, "remaining_usd": 3.0, "cycle_end": "2026-11-30"}


# ---- koneksi dan kerahasiaan ------------------------------------------------------------------
def test_connection_error_is_retried_then_reported_without_token(fake):
    attempts = []

    def flaky_post(url, **kwargs):
        attempts.append(1)
        raise requests.ConnectionError(f"gagal menghubungi ...?token={TOKEN}")

    client = ApifyClient(TOKEN, http_post=flaky_post, http_get=fake.get, retry_sleep=lambda s: None)
    with pytest.raises(ApifyConnectionError) as info:
        client.run_actor(ACTOR, {})

    assert len(attempts) == 3
    assert TOKEN not in str(info.value)
    assert isinstance(info.value, ScraperError)


def test_connection_error_recovers_on_retry(fake):
    state = {"n": 0}

    def post(url, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.ConnectionError("putus sebentar")
        return fake.post(url, **kwargs)

    client = ApifyClient(TOKEN, http_post=post, http_get=fake.get, retry_sleep=lambda s: None)
    assert len(client.run_actor(ACTOR, {})) == 2


def test_token_never_reaches_logs_or_repr(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.status_sequence = ["RUNNING", "FAILED"]
    client, _ = make_client(fake)
    with pytest.raises(ApifyRunFailed):
        client.run_actor(ACTOR, {"profiles": ["kompascom"]})

    assert TOKEN not in caplog.text
    assert TOKEN not in repr(client)
    assert TOKEN not in repr(ApifyAccess(token=TOKEN, user_id=1, credential_id=7))
