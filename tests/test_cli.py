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
