import sqlite3

import pytest

from spi.storage import DEFAULT_USER, Storage


def add_cred(db, user_id, last4="1111", username="akun"):
    return db.replace_credential(
        user_id,
        apify_user_id=f"id-{username}",
        apify_username=username,
        nonce=b"n" * 12,
        ciphertext=b"cipher-" + last4.encode(),
        last4=last4,
        key_version=1,
    )


# ---- pengguna ----------------------------------------------------------------------
def test_default_user_exists_after_init(db):
    assert db.get_user(DEFAULT_USER)["user_id"] == db.default_user_id()
    assert [u["name"] for u in db.list_users()] == [DEFAULT_USER]


def test_create_user_rejects_duplicates_and_bad_names(db):
    uid = db.create_user("andi")
    assert db.get_user("andi")["user_id"] == uid
    with pytest.raises(ValueError, match="sudah ada"):
        db.create_user("andi")
    for bad in ["", "Andi Besar", "a/b", "x" * 40]:
        with pytest.raises(ValueError):
            db.create_user(bad)
    assert db.get_user("tidak_ada") is None


# ---- kredensial --------------------------------------------------------------------
def test_each_user_only_sees_their_own_credential(db):
    a, b = db.create_user("andi"), db.create_user("budi")
    cred_a = add_cred(db, a, "AAAA", "akun_andi")
    cred_b = add_cred(db, b, "BBBB", "akun_budi")

    assert db.active_credential(a)["credential_id"] == cred_a
    assert db.active_credential(a)["apify_username"] == "akun_andi"
    assert db.active_credential(b)["credential_id"] == cred_b
    assert [c["credential_id"] for c in db.list_credentials(a)] == [cred_a]
    assert db.active_credential(db.default_user_id()) is None


def test_replacing_a_credential_revokes_and_wipes_the_old_one(db):
    a = db.create_user("andi")
    old = add_cred(db, a, "OLD1")
    new = add_cred(db, a, "NEW2")

    assert db.active_credential(a)["credential_id"] == new
    rows = {r["credential_id"]: r for r in db.conn.execute("SELECT * FROM apify_credentials")}
    assert rows[old]["status"] == "revoked" and rows[old]["revoked_at"]
    assert rows[old]["token_ciphertext"] is None and rows[old]["token_nonce"] is None
    assert rows[new]["status"] == "active"


def test_revoke_only_touches_the_given_user(db):
    a, b = db.create_user("andi"), db.create_user("budi")
    add_cred(db, a, "AAAA")
    cred_b = add_cred(db, b, "BBBB")

    assert db.revoke_credentials(a) == 1
    assert db.revoke_credentials(a) == 0

    assert db.active_credential(a) is None
    assert db.active_credential(b)["credential_id"] == cred_b
    assert db.active_credential(b)["token_ciphertext"] == b"cipher-BBBB"


def test_database_refuses_two_active_credentials_for_one_user(db):
    a = db.create_user("andi")
    add_cred(db, a)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO apify_credentials (user_id, token_last4, status, created_at) VALUES (?, 'x', 'active', 'now')",
            (a,),
        )


def test_list_credentials_never_returns_secret_columns(db):
    a = db.create_user("andi")
    add_cred(db, a)
    row = db.list_credentials(a)[0]
    assert "token_ciphertext" not in row and "token_nonce" not in row
    assert row["token_last4"] == "1111"


# ---- run dan eksekusi ----------------------------------------------------------------
def test_run_is_owned_by_the_user_who_started_it(db):
    a, b = db.create_user("andi"), db.create_user("budi")
    cred_a = add_cred(db, a)
    run_a = db.start_run(["tiktok:x", "tiktok:y"], user_id=a, credential_id=cred_a)
    run_b = db.start_run(["tiktok:x"], user_id=b)
    run_default = db.start_run(["tiktok:z"])

    assert db.run_owner(run_a) == a and db.run_owner(run_b) == b
    assert db.run_owner(run_default) == db.default_user_id()
    assert db.run_owner("tidak-ada") is None
    assert [r["run_id"] for r in db.list_runs(a)] == [run_a]
    assert db.list_runs(a)[0]["accounts"] == 2
    assert db.latest_run_id(user_id=b) == run_b
    creds = {r["credential_id"] for r in db.conn.execute("SELECT credential_id FROM scrape_runs WHERE run_id = ?", (run_a,))}
    assert creds == {cred_a}


def test_set_run_credential_updates_only_named_accounts(db):
    a = db.create_user("andi")
    old = add_cred(db, a, "OLD1")
    run = db.start_run(["tiktok:x", "tiktok:y"], user_id=a, credential_id=old)
    new = add_cred(db, a, "NEW2")

    db.set_run_credential(run, ["tiktok:y"], new)

    got = {r["account_key"]: r["credential_id"] for r in db.conn.execute("SELECT * FROM scrape_runs WHERE run_id = ?", (run,))}
    assert got == {"tiktok:x": old, "tiktok:y": new}


def test_executions_are_listed_per_user(db):
    a, b = db.create_user("andi"), db.create_user("budi")
    cred_a, cred_b = add_cred(db, a, "AAAA"), add_cred(db, b, "BBBB")
    common = dict(apify_run_id="r1", status="SUCCEEDED", items=3, usage_usd=0.01, error_kind=None, started_at="t0", finished_at="t1")
    db.record_execution(user_id=a, credential_id=cred_a, run_id="run-a", actor="actor~posts", **common)
    db.record_execution(user_id=b, credential_id=cred_b, run_id="run-b", actor="actor~posts", **common)

    rows_a = db.list_executions(a)
    assert [(r["run_id"], r["credential_id"]) for r in rows_a] == [("run-a", cred_a)]
    assert db.list_executions(b, run_id="run-a") == []
    assert db.list_executions(db.default_user_id()) == []


# ---- migrasi dari database lama --------------------------------------------------------
def test_migration_assigns_existing_runs_to_default_user(tmp_path):
    path = tmp_path / "lama.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE scrape_runs (
            run_id TEXT NOT NULL, account_key TEXT NOT NULL, status TEXT NOT NULL,
            posts_found INTEGER NOT NULL DEFAULT 0, comments_found INTEGER NOT NULL DEFAULT 0,
            error TEXT, started_at TEXT NOT NULL, finished_at TEXT,
            PRIMARY KEY (run_id, account_key));
        INSERT INTO scrape_runs (run_id, account_key, status, posts_found, started_at)
            VALUES ('run-lama', 'tiktok:kompascom', 'ok', 30, '2026-10-14T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    db = Storage(path)
    db.init_db()
    db.init_db()  # aman dijalankan berulang
    try:
        assert db.run_owner("run-lama") == db.default_user_id()
        assert db.run_summary("run-lama")["tiktok:kompascom"]["posts_found"] == 30
        assert db.list_runs(db.default_user_id())[0]["run_id"] == "run-lama"
    finally:
        db.close()
