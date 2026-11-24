"""Kumpulan token Apify: urutan, perpindahan saat kredit habis, pengecekan sisa kredit."""

import pytest

from spi.config import ScrapeConfig, StorageConfig
from spi.scrapers.apify_base import (
    ApifyBase,
    ApifyTokenPool,
    get_pool,
    is_credit_exhausted,
    mask_token,
    tokens_from_env,
)
from spi.scrapers.base import ScrapeBlocked


class Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.text = text if text is not None else "[]"

    def json(self):
        return self._payload


def limits(used, maximum=5.0):
    return Resp(payload={"data": {"current": {"monthlyUsageUsd": used}, "limits": {"maxMonthlyUsageUsd": maximum}, "monthlyUsageCycle": {"endAt": "2026-11-30T23:59:59.999Z"}}})


def bearer(kwargs):
    return kwargs["headers"]["Authorization"].removeprefix("Bearer ")


def make(tokens, post, get=None, min_remaining=0.25):
    settings = {
        "scrape": ScrapeConfig(apify_min_remaining_usd=min_remaining),
        "storage": StorageConfig(),
        "env": {"APIFY_TOKENS": ",".join(tokens)},
    }
    pool = ApifyTokenPool(list(tokens))

    def no_get(url, **kw):
        raise AssertionError("http_get tidak boleh dipanggil")

    return ApifyBase(settings, http_post=post, http_get=get or no_get, retry_sleep=lambda s: None, pool=pool), pool


# ---- membaca token dari .env ------------------------------------------------------------
def test_tokens_from_env_reads_list_trims_and_dedupes():
    assert tokens_from_env({"APIFY_TOKENS": " a , b,,a ,c "}) == ["a", "b", "c"]


def test_tokens_from_env_falls_back_to_single_token():
    assert tokens_from_env({"APIFY_TOKEN": "solo"}) == ["solo"]
    assert tokens_from_env({}) == []


def test_tokens_from_env_puts_single_token_first_then_list():
    assert tokens_from_env({"APIFY_TOKEN": "utama", "APIFY_TOKENS": "b,utama,c"}) == ["utama", "b", "c"]


def test_mask_token_shows_only_last_four_characters():
    masked = mask_token("apify_api_ABCDEFGHIJ1234")
    assert masked.endswith("1234") and "ABCDEFGHIJ" not in masked


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (402, "", True),
        (403, '{"error":{"type":"platform-feature-disabled","message":"Monthly usage hard limit exceeded"}}', True),
        (403, '{"error":{"message":"Insufficient permissions for this Actor"}}', False),
        (401, "", False),
        (200, "[]", False),
    ],
)
def test_is_credit_exhausted(status, body, expected):
    assert is_credit_exhausted(status, body) is expected


# ---- perpindahan token ------------------------------------------------------------------
def test_rotates_to_next_token_when_credit_is_exhausted():
    used = []

    def post(url, **kw):
        used.append(bearer(kw))
        return Resp(402, text="kredit habis") if bearer(kw) == "a" else Resp(payload=[{"id": 1}])

    client, pool = make(["a", "b"], post, get=lambda url, **kw: limits(1.0))

    assert client._run_actor("actor", {}) == [{"id": 1}]
    assert used == ["a", "b"]
    assert pool.current() == "b"


def test_exhausted_token_is_not_tried_again():
    used = []

    def post(url, **kw):
        used.append(bearer(kw))
        return Resp(402, text="habis") if bearer(kw) == "a" else Resp(payload=[])

    client, _ = make(["a", "b"], post, get=lambda url, **kw: limits(1.0))
    client._run_actor("actor", {})
    client._run_actor("actor", {})

    assert used == ["a", "b", "b"]


def test_invalid_token_is_skipped_when_others_remain():
    def post(url, **kw):
        return Resp(401, text="token-not-found") if bearer(kw) == "rusak" else Resp(payload=[{"ok": True}])

    client, pool = make(["rusak", "baik"], post, get=lambda url, **kw: limits(0.0))

    assert client._run_actor("actor", {}) == [{"ok": True}]
    assert "401" in pool.exhausted["rusak"]


def test_all_tokens_exhausted_raises_clear_error_without_leaking_tokens():
    client, _ = make(["tokenAAAA1111", "tokenBBBB2222"], lambda url, **kw: Resp(402, text="habis"), get=lambda url, **kw: limits(1.0))

    with pytest.raises(ScrapeBlocked) as exc:
        client._run_actor("actor", {})

    msg = str(exc.value)
    assert "Semua token Apify (2)" in msg
    assert "tokenAAAA1111" not in msg and "tokenBBBB2222" not in msg


def test_single_exhausted_token_reports_credit_exhausted():
    client, _ = make(["solo"], lambda url, **kw: Resp(402, text="habis"))
    with pytest.raises(ScrapeBlocked, match="Kredit Apify habis"):
        client._run_actor("actor", {})


# ---- pengecekan sisa kredit sebelum menjalankan actor --------------------------------------
def test_token_below_minimum_remaining_is_skipped_before_running():
    posted = []

    def post(url, **kw):
        posted.append(bearer(kw))
        return Resp(payload=[])

    def get(url, **kw):
        return limits(4.9) if bearer(kw) == "a" else limits(1.0)  # a: sisa $0,10; b: sisa $4,00

    client, pool = make(["a", "b"], post, get=get, min_remaining=0.25)
    client._run_actor("actor", {})

    assert posted == ["b"]
    assert "sisa" in pool.exhausted["a"]


def test_failed_credit_check_does_not_block_the_run():
    def get(url, **kw):
        raise RuntimeError("jaringan putus")

    client, _ = make(["a", "b"], lambda url, **kw: Resp(payload=[{"id": 1}]), get=get)
    assert client._run_actor("actor", {}) == [{"id": 1}]


def test_single_token_makes_no_credit_check_call():
    client, _ = make(["solo"], lambda url, **kw: Resp(payload=[{"id": 1}]))  # get default melempar bila dipanggil
    assert client._run_actor("actor", {}) == [{"id": 1}]


def test_error_text_scrubs_every_token():
    client, _ = make(["tokenAAAA1111", "tokenBBBB2222"], lambda url, **kw: Resp(500, text="gagal untuk tokenBBBB2222 dan tokenAAAA1111"), get=lambda url, **kw: limits(0.0))
    with pytest.raises(Exception) as exc:
        client._run_actor("actor", {})
    assert "tokenAAAA1111" not in str(exc.value) and "tokenBBBB2222" not in str(exc.value)


def test_missing_tokens_gives_actionable_error():
    settings = {"scrape": ScrapeConfig(), "storage": StorageConfig(), "env": {}}
    client = ApifyBase(settings, http_post=lambda url, **kw: Resp(), pool=ApifyTokenPool([]))
    with pytest.raises(ScrapeBlocked, match="APIFY_TOKEN"):
        client._run_actor("actor", {})


# ---- pool dipakai bersama antar scraper -------------------------------------------------
def test_get_pool_is_shared_so_exhaustion_is_remembered_across_scrapers():
    env = {"APIFY_TOKENS": "bersama-1,bersama-2"}
    first, second = get_pool(env), get_pool(env)
    assert first is second
    first.mark_exhausted("bersama-1", "kredit habis")
    assert second.current() == "bersama-2"


def test_base_uses_shared_pool_from_env_by_default():
    settings = {"scrape": ScrapeConfig(), "storage": StorageConfig(), "env": {"APIFY_TOKENS": "env-x,env-y"}}
    assert ApifyBase(settings).pool is get_pool(settings["env"])


# ---- laporan kredit ---------------------------------------------------------------------
def test_report_lists_usage_per_token_with_masked_token():
    def get(url, **kw):
        if url.endswith("/users/me"):
            return Resp(payload={"data": {"username": f"akun_{bearer(kw)[-1]}"}})
        return limits(2.31) if bearer(kw).endswith("1") else limits(0.5)

    pool = ApifyTokenPool(["tokenAAAA1111", "tokenBBBB2222"])
    rows = pool.report(get)

    assert [r["username"] for r in rows] == ["akun_1", "akun_2"]
    assert rows[0]["used_usd"] == 2.31 and rows[0]["limit_usd"] == 5.0 and rows[0]["remaining_usd"] == pytest.approx(2.69)
    assert rows[0]["cycle_end"] == "2026-11-30"
    assert rows[0]["token"].endswith("1111") and "AAAA" not in rows[0]["token"]
    assert rows[1]["active"] is False and rows[0]["active"] is True


def test_report_marks_unreachable_token():
    def get(url, **kw):
        return Resp(401, text="token-not-found")

    rows = ApifyTokenPool(["tokenAAAA1111"]).report(get)
    assert rows[0]["error"] and rows[0]["remaining_usd"] is None
