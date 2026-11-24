import textwrap

import pytest
import requests
from typer.testing import CliRunner

import spi.cli as cli_mod
from spi import crypto
from spi.cli import app
from spi.scrapers import apify_base
from spi.storage import Storage
from tests.apify_fakes import FakeApify

runner = CliRunner()

TOKEN_A = "apify_api_ANDI_secret_AAAA"
TOKEN_B = "apify_api_BUDI_secret_BBBB"
ENV_TOKEN = "apify_api_ENV_lama_EEEE"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Test CLI tidak boleh membaca .env sungguhan maupun menghubungi Apify sungguhan."""
    monkeypatch.setattr(cli_mod, "load_dotenv", lambda *a, **k: None)
    for name in ("APIFY_TOKEN", "SPI_MASTER_KEY", "SPI_USER"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*a, **k):
        raise AssertionError("test mencoba menghubungi jaringan sungguhan")

    monkeypatch.setattr(requests, "post", no_network)
    monkeypatch.setattr(requests, "get", no_network)


@pytest.fixture
def fake(monkeypatch):
    f = FakeApify()
    f.status_sequence = ["SUCCEEDED"]
    f.add_account(TOKEN_A, "akun_andi", used=2.25, limit=5.0)
    f.add_account(TOKEN_B, "akun_budi", used=0.5, limit=5.0)
    f.add_account(ENV_TOKEN, "akun_env", used=1.0, limit=5.0)
    monkeypatch.setattr(requests, "post", f.post)
    monkeypatch.setattr(requests, "get", f.get)
    monkeypatch.setattr(apify_base, "POLL_PAUSE_SECONDS", 0)
    return f


@pytest.fixture
def master_key(monkeypatch):
    monkeypatch.setenv("SPI_MASTER_KEY", crypto.generate_master_key())


def invoke(cfg, *args, **kw):
    return runner.invoke(app, ["--config", str(cfg), *args], **kw)


def db_dump(tmp_path) -> str:
    db = Storage(tmp_path / "spi.sqlite")
    try:
        return "\n".join(db.conn.iterdump())
    finally:
        db.close()


def write_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            accounts:
              - platform: instagram
                username: kompascom
                label: Kompas
            storage:
              db_path: {(tmp_path / 'spi.sqlite').as_posix()}
              reports_dir: {(tmp_path / 'reports').as_posix()}
            """
        ),
        encoding="utf-8",
    )
    return cfg


def test_status_on_empty_db(tmp_path):
    cfg = write_config(tmp_path)
    result = runner.invoke(app, ["--config", str(cfg), "status"])
    assert result.exit_code == 0, result.output
    assert "belum ada data" in result.output.lower()


def test_export_creates_files(tmp_path):
    cfg = write_config(tmp_path)
    out = tmp_path / "export"
    result = runner.invoke(app, ["--config", str(cfg), "export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "posts.csv").exists()


def test_scrape_rejects_unknown_only_account(tmp_path):
    cfg = write_config(tmp_path)
    result = runner.invoke(app, ["--config", str(cfg), "scrape", "--only", "tiktok:nggak_ada"])
    assert result.exit_code != 0
    assert "tidak ada di config" in result.output


def test_missing_config_gives_helpful_message(tmp_path):
    result = runner.invoke(app, ["--config", str(tmp_path / "x.yaml"), "status"])
    assert result.exit_code != 0
    assert "config.example.yaml" in result.output


def test_scrape_max_posts_and_max_comments_override_config(tmp_path, monkeypatch):
    import spi.runner as runner_mod

    captured = {}

    def fake_run_scrape(cfg, db, env, only=None, resume_run_id=None, **kw):
        captured["max_posts"] = cfg.scrape.max_posts_per_account
        captured["max_comments"] = cfg.scrape.max_comments_per_post
        captured["days"] = cfg.scrape.days
        return "run-x", {}

    monkeypatch.setattr(runner_mod, "run_scrape", fake_run_scrape)
    cfg = write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(cfg), "scrape", "--max-posts", "5", "--max-comments", "7", "--days", "2"])

    assert result.exit_code == 0, result.output
    assert captured == {"max_posts": 5, "max_comments": 7, "days": 2}


# ---- kunci induk -------------------------------------------------------------------------
def test_key_init_writes_key_without_printing_it_and_refuses_to_overwrite(tmp_path):
    cfg = write_config(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("APIFY_TOKEN=\n", encoding="utf-8")

    result = invoke(cfg, "key", "init", "--env-file", str(env_file))

    assert result.exit_code == 0, result.output
    content = env_file.read_text(encoding="utf-8")
    key_line = [ln for ln in content.splitlines() if ln.startswith("SPI_MASTER_KEY=")][0]
    key = key_line.split("=", 1)[1]
    assert len(crypto.load_master_key({"SPI_MASTER_KEY": key})) == 32
    assert key not in result.output
    assert content.startswith("APIFY_TOKEN=\n")

    again = invoke(cfg, "key", "init", "--env-file", str(env_file))
    assert again.exit_code != 0 and "sudah ada" in again.output
    assert env_file.read_text(encoding="utf-8") == content


def test_key_init_fills_an_empty_placeholder_line(tmp_path):
    cfg = write_config(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=x\nSPI_MASTER_KEY=\nIG_USERNAME=y\n", encoding="utf-8")

    result = invoke(cfg, "key", "init", "--env-file", str(env_file))

    assert result.exit_code == 0, result.output
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "LLM_API_KEY=x" and lines[2] == "IG_USERNAME=y"
    assert lines[1].startswith("SPI_MASTER_KEY=") and len(lines[1]) > 40
    assert len(lines) == 3


# ---- pengguna -------------------------------------------------------------------------------
def test_user_add_and_list(tmp_path):
    cfg = write_config(tmp_path)
    assert invoke(cfg, "user", "add", "andi").exit_code == 0
    dup = invoke(cfg, "user", "add", "andi")
    assert dup.exit_code != 0 and "sudah ada" in dup.output

    listing = invoke(cfg, "--user", "andi", "user", "list")
    assert listing.exit_code == 0
    assert "default" in listing.output and "andi  (dipilih)" in listing.output


def test_unknown_user_is_rejected_not_created(tmp_path):
    cfg = write_config(tmp_path)
    result = invoke(cfg, "--user", "siapa", "apify", "status")
    assert result.exit_code != 0
    assert "spi user add siapa" in result.output
    assert "siapa" not in invoke(cfg, "user", "list").output


# ---- token Apify ------------------------------------------------------------------------------
def test_apify_connect_validates_stores_encrypted_and_never_echoes_token(tmp_path, fake, master_key):
    cfg = write_config(tmp_path)
    invoke(cfg, "user", "add", "andi")

    result = invoke(cfg, "--user", "andi", "apify", "connect", input=TOKEN_A + "\n")

    assert result.exit_code == 0, result.output
    assert "akun_andi" in result.output and "...AAAA" in result.output
    assert TOKEN_A not in result.output
    assert TOKEN_A not in db_dump(tmp_path)
    assert TOKEN_A not in (tmp_path / "scrape.log").read_text(encoding="utf-8")


def test_apify_status_shows_only_this_users_account_and_usage(tmp_path, fake, master_key):
    cfg = write_config(tmp_path)
    for name, token in (("andi", TOKEN_A), ("budi", TOKEN_B)):
        invoke(cfg, "user", "add", name)
        assert invoke(cfg, "--user", name, "apify", "connect", input=token + "\n").exit_code == 0
    fake.calls.clear()

    result = invoke(cfg, "--user", "andi", "apify", "status")

    assert result.exit_code == 0, result.output
    assert "akun_andi" in result.output and "akun_budi" not in result.output
    assert "$2.25" in result.output and "$2.75" in result.output and "2026-11-30" in result.output
    assert TOKEN_A not in result.output and "...AAAA" in result.output
    assert fake.tokens_used() == {TOKEN_A}


def test_apify_connect_without_master_key_explains_key_init(tmp_path, fake):
    cfg = write_config(tmp_path)
    result = invoke(cfg, "apify", "connect", input=TOKEN_A + "\n")
    assert result.exit_code != 0
    assert "spi key init" in result.output
    assert fake.calls == []


def test_apify_connect_rejects_invalid_token(tmp_path, fake, master_key):
    cfg = write_config(tmp_path)
    result = invoke(cfg, "apify", "connect", input="apify_api_SALAH_0000\n")
    assert result.exit_code != 0
    assert "Token tidak disimpan" in result.output
    assert "apify_api_SALAH_0000" not in result.output
    assert "belum terhubung" in invoke(cfg, "apify", "status").output


def test_apify_connect_from_env_imports_legacy_token(tmp_path, fake, master_key, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", ENV_TOKEN)
    cfg = write_config(tmp_path)

    before = invoke(cfg, "apify", "status")
    assert "cara lama" in before.output and ENV_TOKEN not in before.output

    result = invoke(cfg, "apify", "connect", "--from-env")
    assert result.exit_code == 0, result.output
    assert "akun_env" in result.output and "hapus baris APIFY_TOKEN" in result.output
    assert ENV_TOKEN not in result.output and ENV_TOKEN not in db_dump(tmp_path)
    assert "terenkripsi" in invoke(cfg, "apify", "status").output


def test_legacy_env_token_is_not_offered_to_other_users(tmp_path, fake, master_key, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", ENV_TOKEN)
    cfg = write_config(tmp_path)
    invoke(cfg, "user", "add", "budi")
    fake.calls.clear()

    result = invoke(cfg, "--user", "budi", "apify", "status")

    assert "belum terhubung" in result.output and "cara lama" not in result.output
    assert fake.calls == []


def test_apify_disconnect_removes_token(tmp_path, fake, master_key):
    cfg = write_config(tmp_path)
    invoke(cfg, "apify", "connect", input=TOKEN_A + "\n")

    result = invoke(cfg, "apify", "disconnect")

    assert result.exit_code == 0 and "dicabut" in result.output
    assert "belum terhubung" in invoke(cfg, "apify", "status").output
    assert "tidak punya token" in invoke(cfg, "apify", "disconnect").output


# ---- job per pengguna ----------------------------------------------------------------------------
def test_scrape_runs_as_the_selected_user(tmp_path, monkeypatch):
    import spi.runner as runner_mod

    captured = {}

    def fake_run_scrape(cfg, db, env, only=None, resume_run_id=None, user_id=None, **kw):
        captured["user_id"] = user_id
        return "run-x", {}

    monkeypatch.setattr(runner_mod, "run_scrape", fake_run_scrape)
    cfg = write_config(tmp_path)
    invoke(cfg, "user", "add", "andi")

    assert invoke(cfg, "--user", "andi", "scrape").exit_code == 0
    db = Storage(tmp_path / "spi.sqlite")
    try:
        assert captured["user_id"] == db.get_user("andi")["user_id"]
    finally:
        db.close()

    monkeypatch.setenv("SPI_USER", "andi")
    captured.clear()
    assert invoke(cfg, "scrape").exit_code == 0
    assert captured["user_id"] is not None


def test_jobs_lists_only_own_runs_and_refuses_other_users_run(tmp_path):
    cfg = write_config(tmp_path)
    invoke(cfg, "user", "add", "andi")
    invoke(cfg, "user", "add", "budi")
    db = Storage(tmp_path / "spi.sqlite")
    db.init_db()
    a, b = db.get_user("andi")["user_id"], db.get_user("budi")["user_id"]
    run_a = db.start_run(["instagram:kompascom"], user_id=a)
    run_b = db.start_run(["instagram:kompascom"], user_id=b)
    db.record_execution(
        user_id=a, credential_id=None, run_id=run_a, actor="apify~instagram-scraper", apify_run_id="r1", status="SUCCEEDED",
        items=5, usage_usd=0.0135, error_kind=None, started_at="2026-11-24T10:00:00+00:00", finished_at="2026-11-24T10:01:00+00:00",
    )
    db.record_execution(
        user_id=b, credential_id=None, run_id=run_b, actor="clockworks~tiktok-scraper", apify_run_id=None, status="NOT_STARTED",
        items=None, usage_usd=None, error_kind="insufficient_credits", started_at="2026-11-24T11:00:00+00:00", finished_at=None,
    )
    db.close()

    mine = invoke(cfg, "--user", "andi", "jobs")
    assert mine.exit_code == 0, mine.output
    assert run_a in mine.output and run_b not in mine.output
    assert "apify~instagram-scraper" in mine.output and "$0.0135" in mine.output
    assert "insufficient_credits" not in mine.output

    theirs = invoke(cfg, "--user", "budi", "jobs")
    assert run_b in theirs.output and "insufficient_credits" in theirs.output

    denied = invoke(cfg, "--user", "andi", "jobs", "--run", run_b)
    assert denied.exit_code != 0 and "tidak ditemukan untuk pengguna ini" in denied.output
    detail = invoke(cfg, "--user", "andi", "jobs", "--run", run_a)
    assert detail.exit_code == 0 and "instagram:kompascom" in detail.output


def test_credits_command_is_gone(tmp_path):
    cfg = write_config(tmp_path)
    assert invoke(cfg, "credits").exit_code != 0
