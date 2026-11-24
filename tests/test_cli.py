import textwrap

from typer.testing import CliRunner

from spi.cli import app

runner = CliRunner()


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


def test_credits_lists_every_token_masked_with_totals(tmp_path, monkeypatch):
    from spi.scrapers import apify_base

    monkeypatch.setenv("APIFY_TOKEN", "")
    monkeypatch.setenv("APIFY_TOKENS", "tokenAAAA1111,tokenBBBB2222")
    rows = [
        {"position": "1/2", "token": "...1111", "active": True, "username": "akun_satu", "used_usd": 4.9, "limit_usd": 5.0,
         "remaining_usd": 0.1, "cycle_end": "2026-11-30", "skipped": None, "error": None},
        {"position": "2/2", "token": "...2222", "active": False, "username": "akun_dua", "used_usd": 0.5, "limit_usd": 5.0,
         "remaining_usd": 4.5, "cycle_end": "2026-12-14", "skipped": None, "error": None},
    ]
    monkeypatch.setattr(apify_base.ApifyTokenPool, "report", lambda self, http_get=None: rows)
    cfg = write_config(tmp_path)

    result = runner.invoke(app, ["--config", str(cfg), "credits"])

    assert result.exit_code == 0, result.output
    assert "akun_satu" in result.output and "akun_dua" in result.output
    assert "...1111" in result.output and "tokenAAAA1111" not in result.output
    assert "$4.50" in result.output and "2026-12-14" in result.output
    assert "di bawah batas" in result.output  # token 1 sisa $0,10 < $0,25
    assert "$4.60" in result.output  # total sisa


def test_credits_without_tokens_explains_how_to_add_them(tmp_path, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "")
    monkeypatch.setenv("APIFY_TOKENS", "")
    cfg = write_config(tmp_path)
    result = runner.invoke(app, ["--config", str(cfg), "credits"])
    assert result.exit_code != 0
    assert "APIFY_TOKENS" in result.output
