import logging

import pytest

from spi import crypto
from spi.credentials import CredentialError, CredentialMissing, CredentialService
from spi.scrapers.apify_base import ApifyClient, ApifyInvalidToken
from tests.apify_fakes import FakeApify

TOKEN_A = "apify_api_ANDI_secret_AAAA"
TOKEN_A2 = "apify_api_ANDI_baru_A2A2"
TOKEN_B = "apify_api_BUDI_secret_BBBB"
ENV_TOKEN = "apify_api_ENV_lama_EEEE"


@pytest.fixture
def fake():
    f = FakeApify()
    f.add_account(TOKEN_A, "akun_andi", used=4.9, limit=5.0)
    f.add_account(TOKEN_A2, "akun_andi_baru", used=0.0, limit=5.0)
    f.add_account(TOKEN_B, "akun_budi", used=1.0, limit=5.0)
    f.add_account(ENV_TOKEN, "akun_env", used=0.5, limit=5.0)
    return f


def make_service(db, fake, env=None):
    env = {crypto.MASTER_KEY_ENV: crypto.generate_master_key()} if env is None else env
    factory = lambda token: ApifyClient(token, http_post=fake.post, http_get=fake.get, retry_sleep=lambda s: None)  # noqa: E731
    return CredentialService(db, env, client_factory=factory)


@pytest.fixture
def users(db):
    return db.create_user("andi"), db.create_user("budi")


# ---- hubungkan -----------------------------------------------------------------------
def test_connect_validates_token_and_stores_account_identity(db, fake, users):
    a, _ = users
    svc = make_service(db, fake)

    status = svc.connect(a, f"  {TOKEN_A}\n")

    assert status["apify_username"] == "akun_andi" and status["apify_user_id"] == "id-akun_andi"
    assert status["token_last4"] == "AAAA" and status["status"] == "active"
    assert "token_ciphertext" not in status and TOKEN_A not in str(status)


def test_token_is_encrypted_at_rest(db, fake, users):
    a, _ = users
    make_service(db, fake).connect(a, TOKEN_A)

    dump = "\n".join(db.conn.iterdump())
    assert TOKEN_A not in dump
    row = db.active_credential(a)
    assert TOKEN_A.encode() not in row["token_ciphertext"]
    assert len(row["token_nonce"]) == 12


def test_invalid_token_is_rejected_and_nothing_is_stored(db, fake, users):
    a, _ = users
    svc = make_service(db, fake)
    with pytest.raises(ApifyInvalidToken):
        svc.connect(a, "apify_api_TIDAK_SAH_0000")
    assert svc.status(a) is None and db.list_credentials(a) == []


def test_failed_replacement_keeps_the_working_credential(db, fake, users):
    a, _ = users
    svc = make_service(db, fake)
    svc.connect(a, TOKEN_A)
    with pytest.raises(ApifyInvalidToken):
        svc.connect(a, "apify_api_TIDAK_SAH_0000")
    assert svc.resolve_for_job(a).token == TOKEN_A


def test_connect_requires_master_key_and_does_not_call_apify_without_it(db, fake, users):
    a, _ = users
    svc = make_service(db, fake, env={})
    with pytest.raises(crypto.MasterKeyError):
        svc.connect(a, TOKEN_A)
    assert fake.calls == []


def test_empty_token_is_rejected(db, fake, users):
    with pytest.raises(CredentialError):
        make_service(db, fake).connect(users[0], "   ")


# ---- ganti dan cabut ---------------------------------------------------------------------
def test_replacing_token_revokes_old_one_and_new_token_is_used(db, fake, users):
    a, _ = users
    svc = make_service(db, fake)
    old = svc.connect(a, TOKEN_A)["credential_id"]
    new = svc.connect(a, TOKEN_A2)["credential_id"]

    access = svc.resolve_for_job(a)
    assert access.token == TOKEN_A2 and access.credential_id == new and new != old
    history = {h["credential_id"]: h["status"] for h in svc.history(a)}
    assert history == {old: "revoked", new: "active"}
    assert TOKEN_A not in "\n".join(db.conn.iterdump())


def test_disconnect_deletes_token_and_reconnect_works(db, fake, users):
    a, _ = users
    svc = make_service(db, fake)
    svc.connect(a, TOKEN_A)

    assert svc.disconnect(a) is True
    assert svc.disconnect(a) is False
    assert svc.status(a) is None
    with pytest.raises(CredentialMissing, match="spi apify connect"):
        svc.resolve_for_job(a)
    assert db.conn.execute("SELECT COUNT(*) FROM apify_credentials WHERE token_ciphertext IS NOT NULL").fetchone()[0] == 0

    svc.connect(a, TOKEN_A)
    assert svc.resolve_for_job(a).token == TOKEN_A


# ---- isolasi antar pengguna ----------------------------------------------------------------
def test_each_user_resolves_only_their_own_token(db, fake, users):
    a, b = users
    svc = make_service(db, fake)
    cred_a = svc.connect(a, TOKEN_A)["credential_id"]
    cred_b = svc.connect(b, TOKEN_B)["credential_id"]

    access_a, access_b = svc.resolve_for_job(a), svc.resolve_for_job(b)
    assert (access_a.token, access_a.user_id, access_a.credential_id) == (TOKEN_A, a, cred_a)
    assert (access_b.token, access_b.user_id, access_b.credential_id) == (TOKEN_B, b, cred_b)
    assert svc.status(a)["apify_username"] == "akun_andi"
    assert svc.status(b)["apify_username"] == "akun_budi"


def test_user_without_credential_never_gets_someone_elses_token(db, fake, users):
    a, b = users
    svc = make_service(db, fake)
    svc.connect(a, TOKEN_A)
    with pytest.raises(CredentialMissing):
        svc.resolve_for_job(b)
    assert svc.status(b) is None and svc.history(b) == []


def test_disconnect_does_not_touch_other_users(db, fake, users):
    a, b = users
    svc = make_service(db, fake)
    svc.connect(a, TOKEN_A)
    svc.connect(b, TOKEN_B)
    svc.disconnect(a)
    assert svc.resolve_for_job(b).token == TOKEN_B


def test_ciphertext_moved_to_another_users_row_cannot_be_opened(db, fake, users):
    """Bahkan bila baris database dimanipulasi, token A tidak bisa dipakai atas nama B."""
    a, b = users
    svc = make_service(db, fake)
    svc.connect(a, TOKEN_A)
    db.conn.execute("UPDATE apify_credentials SET user_id = ? WHERE user_id = ?", (b, a))
    db.conn.commit()
    with pytest.raises(crypto.DecryptError):
        svc.resolve_for_job(b)


def test_usage_is_read_with_the_users_own_token(db, fake, users):
    a, b = users
    svc = make_service(db, fake)
    svc.connect(a, TOKEN_A)
    svc.connect(b, TOKEN_B)
    fake.calls.clear()

    usage_a = svc.usage(a)

    assert usage_a["used_usd"] == 4.9 and usage_a["remaining_usd"] == pytest.approx(0.1)
    assert fake.tokens_used() == {TOKEN_A}
    assert svc.usage(b)["used_usd"] == 1.0


# ---- kompatibilitas token lama di .env --------------------------------------------------------
def test_legacy_env_token_is_only_for_the_default_user(db, fake, users):
    a, _ = users
    svc = make_service(db, fake, env={"APIFY_TOKEN": ENV_TOKEN})
    default = db.default_user_id()

    access = svc.resolve_for_job(default)
    assert (access.token, access.credential_id, access.source) == (ENV_TOKEN, None, "env")
    assert svc.uses_legacy_env_token(default) is True
    with pytest.raises(CredentialMissing):
        svc.resolve_for_job(a)
    assert svc.uses_legacy_env_token(a) is False


def test_stored_credential_wins_over_legacy_env_token(db, fake):
    env = {"APIFY_TOKEN": ENV_TOKEN, crypto.MASTER_KEY_ENV: crypto.generate_master_key()}
    svc = make_service(db, fake, env=env)
    default = db.default_user_id()
    svc.connect(default, TOKEN_A)
    assert svc.resolve_for_job(default).token == TOKEN_A
    assert svc.uses_legacy_env_token(default) is False


def test_tokens_never_appear_in_logs(db, fake, users, caplog):
    caplog.set_level(logging.DEBUG)
    a, _ = users
    svc = make_service(db, fake, env={"APIFY_TOKEN": ENV_TOKEN, crypto.MASTER_KEY_ENV: crypto.generate_master_key()})
    svc.connect(a, TOKEN_A)
    svc.resolve_for_job(a)
    svc.resolve_for_job(db.default_user_id())
    svc.disconnect(a)
    assert TOKEN_A not in caplog.text and ENV_TOKEN not in caplog.text
