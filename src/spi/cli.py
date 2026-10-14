"""CLI `spi`: login, scrape, report, export, status."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from spi.config import AppConfig, load_config
from spi.storage import Storage

app = typer.Typer(
    help="Scrape Platform Issues: observasi isu hangat dari akun media besar di Instagram dan TikTok.",
    no_args_is_help=True,
    add_completion=False,
)


class State:
    def __init__(self, cfg: AppConfig, env: dict[str, str]):
        self.cfg = cfg
        self.env = env

    def open_db(self) -> Storage:
        db = Storage(self.cfg.storage.db_path)
        db.init_db()
        return db


def _setup_logging(cfg: AppConfig, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_dir = Path(cfg.storage.db_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers.append(logging.FileHandler(log_dir / "scrape.log", encoding="utf-8"))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.callback()
def main(
    ctx: typer.Context,
    config: Path = typer.Option("config.yaml", "--config", "-c", help="File konfigurasi akun."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log lebih rinci."),
) -> None:
    load_dotenv()
    if not config.exists():
        typer.echo(
            f"File konfigurasi {config} tidak ditemukan. Salin config.example.yaml ke {config} lalu sesuaikan daftar akun.",
            err=True,
        )
        raise typer.Exit(code=1)
    cfg = load_config(config)
    _setup_logging(cfg, verbose)
    ctx.obj = State(cfg, dict(os.environ))


def _state(ctx: typer.Context) -> State:
    return ctx.obj


# ---- login -----------------------------------------------------------------------
@app.command()
def login(ctx: typer.Context) -> None:
    """Login Instagram dengan akun SEKUNDER dan simpan sesi untuk scraper."""
    import instaloader
    from instaloader import exceptions as ilx

    st = _state(ctx)
    username = st.env.get("IG_USERNAME") or typer.prompt("Username Instagram (akun sekunder)")
    session_file = st.cfg.storage.ig_session_file
    Path(session_file).parent.mkdir(parents=True, exist_ok=True)

    loader = instaloader.Instaloader(quiet=True, download_pictures=False, download_videos=False, save_metadata=False)
    password = typer.prompt("Password", hide_input=True)
    try:
        try:
            loader.login(username, password)
        except ilx.TwoFactorAuthRequiredException:
            code = typer.prompt("Kode 2FA")
            loader.two_factor_login(code)
    except ilx.BadCredentialsException:
        typer.echo("Username/password salah.", err=True)
        raise typer.Exit(code=1)
    except (ilx.LoginException, ilx.ConnectionException) as e:
        typer.echo(
            f"Login gagal: {e}\n"
            "Instagram mungkin meminta verifikasi (checkpoint). Buka instagram.com di browser dengan akun ini, "
            "selesaikan verifikasinya, lalu coba lagi. Alternatif: jalankan `instaloader --login USERNAME` "
            "dan salin file sesinya ke " + session_file + ", atau impor sesi dari Firefox dengan script resmi "
            "Instaloader 615_import_firefox_session.py (lihat README).",
            err=True,
        )
        raise typer.Exit(code=1)
    loader.save_session_to_file(session_file)
    typer.echo(f"Sesi tersimpan di {session_file} untuk @{loader.test_login()}")


# ---- scrape ----------------------------------------------------------------------
@app.command()
def scrape(
    ctx: typer.Context,
    only: Optional[list[str]] = typer.Option(None, "--only", help="Batasi ke akun tertentu, format platform:username. Bisa diulang."),
    days: Optional[int] = typer.Option(None, "--days", help="Ambil post N hari terakhir (default dari config)."),
    max_comments: Optional[int] = typer.Option(None, "--max-comments", help="Batas komentar per post."),
    resume: Optional[str] = typer.Option(None, "--resume", help="Lanjutkan run yang belum selesai (RUN_ID)."),
) -> None:
    """Ambil post dan komentar dari semua akun di config."""
    from spi.runner import run_scrape

    st = _state(ctx)
    if days is not None:
        st.cfg.scrape.days = days
    if max_comments is not None:
        st.cfg.scrape.max_comments_per_post = max_comments

    db = st.open_db()
    try:
        run_id, summary = run_scrape(st.cfg, db, st.env, only=only or None, resume_run_id=resume)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    finally:
        db.close()

    typer.echo(f"\nRun {run_id} selesai.\n")
    _echo_summary(summary)
    not_ok = [k for k, v in summary.items() if v["status"] != "ok"]
    if not_ok:
        typer.echo(f"\nAkun bermasalah: {', '.join(not_ok)}. Lanjutkan yang rate_limited dengan: spi scrape --resume {run_id}")


def _echo_summary(summary: dict) -> None:
    typer.echo(f"{'Akun':32} {'Status':13} {'Post':>6} {'Komentar':>9}  Error")
    for key, row in summary.items():
        typer.echo(f"{key:32} {row['status']:13} {row['posts_found']:>6} {row['comments_found']:>9}  {row['error'] or ''}")


# ---- report ----------------------------------------------------------------------
@app.command()
def report(
    ctx: typer.Context,
    days: Optional[int] = typer.Option(None, "--days", help="Rentang hari ke belakang (default dari config)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Hanya hitung token dan estimasi biaya laporan akhir."),
    skip_analysis: bool = typer.Option(False, "--skip-analysis", help="Lewati analisis per post, langsung laporan."),
) -> None:
    """Analisis per post (Claude Haiku) lalu susun laporan isu hangat (Claude Opus)."""
    from spi.analysis.llm import make_provider
    from spi.analysis.per_post import analyze_pending
    from spi.analysis.report import generate_report

    st = _state(ctx)
    n_days = days if days is not None else st.cfg.scrape.days
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=n_days)
    try:
        provider = make_provider(st.cfg.analysis, st.env)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Penyedia LLM: {provider.name} (per post: {st.cfg.analysis.per_post_model}, laporan: {st.cfg.analysis.report_model})")

    db = st.open_db()
    try:
        if not skip_analysis and not dry_run:
            typer.echo("Tahap 1: analisis per post ...")

            def progress(i, n, post):
                typer.echo(f"  [{i}/{n}] {post.platform}:{post.account} {post.post_id}")

            stats = analyze_pending(db, st.cfg.analysis, provider, since, progress=progress)
            typer.echo(
                f"  selesai: {stats['analyzed']} dianalisis, {stats['skipped']} dilewati (tidak berubah), "
                f"{stats['failed']} gagal; token {stats['tokens_in']} masuk / {stats['tokens_out']} keluar"
            )
            for err in stats["errors"][:10]:
                typer.echo(f"  ! {err}")

        failed_accounts = {}
        run_id = db.latest_run_id()
        if run_id:
            failed_accounts = {k: f"{v['status']}: {v['error']}" for k, v in db.run_summary(run_id).items() if v["status"] != "ok"}

        typer.echo("Tahap 2: laporan akhir ..." if not dry_run else "Estimasi laporan akhir (dry run) ...")
        try:
            result = generate_report(db, st.cfg.analysis, provider, since, until, st.cfg.storage.reports_dir, failed_accounts, dry_run=dry_run)
        except RuntimeError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1)
    finally:
        db.close()

    if dry_run:
        cost = result["estimated_cost_usd"]
        cost_text = f"≈ ${cost:.2f}" if cost is not None else "(biaya tidak diperkirakan untuk penyedia ini)"
        typer.echo(
            f"  {result['posts_included']} post, perkiraan {result['estimated_input_tokens']:,} token masuk "
            f"+ {result['estimated_output_tokens']:,} token keluar dengan {result['model']} {cost_text}"
        )
    else:
        typer.echo(f"  laporan tersimpan: {result['path']} ({result['posts_included']} post; token {result['tokens_in']} / {result['tokens_out']})")


# ---- export ----------------------------------------------------------------------
@app.command()
def export(
    ctx: typer.Context,
    out: Path = typer.Option(Path("data/export"), "--out", help="Folder tujuan CSV."),
    days: Optional[int] = typer.Option(None, "--days", help="Hanya post N hari terakhir (default semua)."),
) -> None:
    """Ekspor posts.csv, comments.csv, post_analysis.csv."""
    from spi.export import export_csv

    st = _state(ctx)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    db = st.open_db()
    try:
        written = export_csv(db, out, since=since)
    finally:
        db.close()
    for p in written:
        typer.echo(f"  {p}")


# ---- status ----------------------------------------------------------------------
@app.command()
def status(ctx: typer.Context) -> None:
    """Ringkasan isi database: post/komentar per akun, run terakhir, laporan."""
    st = _state(ctx)
    db = st.open_db()
    try:
        stats = db.account_stats()
        if not stats:
            typer.echo("Belum ada data. Jalankan `spi login` (untuk Instagram) lalu `spi scrape`.")
            return
        typer.echo(f"{'Akun':32} {'Post':>6} {'Komentar':>9}  Post terbaru")
        for row in stats:
            typer.echo(f"{row['platform'] + ':' + row['account']:32} {row['posts']:>6} {row['comments']:>9}  {row['latest_post'][:10]}")
        run_id = db.latest_run_id()
        if run_id:
            typer.echo(f"\nRun terakhir: {run_id}")
            _echo_summary(db.run_summary(run_id))
        reports = db.list_reports()
        if reports:
            typer.echo("\nLaporan:")
            for r in reports[:5]:
                typer.echo(f"  {r['created_at'][:16]}  {r['path']}  ({r['posts_included']} post)")
    finally:
        db.close()


if __name__ == "__main__":
    app()
