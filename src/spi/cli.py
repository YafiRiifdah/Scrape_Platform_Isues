"""CLI `spi`: login, scrape, report, export, status, serta pengguna dan token Apify milik masing-masing."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from spi.config import AppConfig, load_config
from spi.storage import DEFAULT_USER, Storage

app = typer.Typer(
    help="Scrape Platform Issues: observasi isu hangat dari akun media besar di Instagram dan TikTok.",
    no_args_is_help=True,
    add_completion=False,
)
key_app = typer.Typer(help="Kunci induk untuk mengenkripsi token yang disimpan.", no_args_is_help=True)
user_app = typer.Typer(help="Pengguna. Setiap pengguna memakai token Apify miliknya sendiri.", no_args_is_help=True)
apify_app = typer.Typer(help="Token Apify milik pengguna yang dipilih dengan --user.", no_args_is_help=True)
app.add_typer(key_app, name="key")
app.add_typer(user_app, name="user")
app.add_typer(apify_app, name="apify")


class State:
    def __init__(self, cfg: AppConfig, env: dict[str, str], user_name: str = DEFAULT_USER):
        self.cfg = cfg
        self.env = env
        self.user_name = user_name or DEFAULT_USER

    def open_db(self) -> Storage:
        db = Storage(self.cfg.storage.db_path)
        db.init_db()
        return db

    def user_id(self, db: Storage) -> int:
        """Id pengguna yang dipilih. Pengguna harus dibuat lebih dulu; tidak ada pembuatan otomatis."""
        row = db.get_user(self.user_name)
        if row is None:
            typer.echo(f"Pengguna {self.user_name!r} belum ada. Buat dengan: spi user add {self.user_name}", err=True)
            raise typer.Exit(code=1)
        return int(row["user_id"])


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
    user: str = typer.Option(
        DEFAULT_USER, "--user", "-u", envvar="SPI_USER", help="Pengguna yang menjalankan perintah. Job memakai token Apify miliknya."
    ),
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
    ctx.obj = State(cfg, dict(os.environ), user_name=(user or "").strip())


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
    max_posts: Optional[int] = typer.Option(None, "--max-posts", help="Batas post per akun (mengendalikan biaya Apify)."),
    resume: Optional[str] = typer.Option(None, "--resume", help="Lanjutkan run yang belum selesai (RUN_ID)."),
) -> None:
    """Ambil post dan komentar dari semua akun di config, memakai token Apify milik pengguna (--user)."""
    from spi.runner import run_scrape

    st = _state(ctx)
    if days is not None:
        st.cfg.scrape.days = days
    if max_comments is not None:
        st.cfg.scrape.max_comments_per_post = max_comments
    if max_posts is not None:
        st.cfg.scrape.max_posts_per_account = max_posts

    db = st.open_db()
    try:
        run_id, summary = run_scrape(st.cfg, db, st.env, only=only or None, resume_run_id=resume, user_id=st.user_id(db))
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
        run_id = db.latest_run_id(user_id=st.user_id(db))
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


# ---- key ---------------------------------------------------------------------------
@key_app.command("init")
def key_init(
    ctx: typer.Context,
    env_file: Path = typer.Option(Path(".env"), "--env-file", help="File .env tempat kunci disimpan."),
) -> None:
    """Buat kunci induk enkripsi (SPI_MASTER_KEY) dan simpan di .env. Cukup sekali."""
    from spi.crypto import MASTER_KEY_ENV, generate_master_key

    st = _state(ctx)
    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    filled = re.search(rf"^[ \t]*{MASTER_KEY_ENV}[ \t]*=[ \t]*\S", existing, re.M)
    if filled or (st.env.get(MASTER_KEY_ENV) or "").strip():
        typer.echo(
            f"{MASTER_KEY_ENV} sudah ada dan tidak diubah. Mengganti kunci membuat semua token tersimpan tidak bisa dibuka.",
            err=True,
        )
        raise typer.Exit(code=1)

    line = f"{MASTER_KEY_ENV}={generate_master_key()}"
    empty = re.compile(rf"^[ \t]*{MASTER_KEY_ENV}[ \t]*=[ \t]*$", re.M)
    if empty.search(existing):
        content = empty.sub(lambda m: line, existing, count=1)
    else:
        content = existing + ("" if not existing or existing.endswith("\n") else "\n")
        content += (
            "\n# Kunci induk enkripsi token Apify (AES-256-GCM). Jangan dibagikan dan jangan di-commit.\n"
            "# Bila kunci ini hilang, token tersimpan tidak bisa dibuka dan harus dihubungkan ulang.\n"
            f"{line}\n"
        )
    env_file.write_text(content, encoding="utf-8")
    typer.echo(f"Kunci induk dibuat dan disimpan di {env_file} (tidak ditampilkan). Cadangkan file itu di tempat aman.")


# ---- user --------------------------------------------------------------------------
@user_app.command("add")
def user_add(ctx: typer.Context, name: str = typer.Argument(..., help="Nama pengguna baru.")) -> None:
    """Tambah pengguna. Pengguna baru belum punya token Apify sampai menghubungkannya sendiri."""
    st = _state(ctx)
    db = st.open_db()
    try:
        db.create_user(name)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    finally:
        db.close()
    typer.echo(f"Pengguna {name} dibuat. Hubungkan token Apify miliknya dengan: spi --user {name} apify connect")


@user_app.command("list")
def user_list(ctx: typer.Context) -> None:
    """Daftar pengguna."""
    st = _state(ctx)
    db = st.open_db()
    try:
        for u in db.list_users():
            marker = "  (dipilih)" if u["name"] == st.user_name else ""
            typer.echo(f"{u['name']}{marker}")
    finally:
        db.close()


# ---- apify -------------------------------------------------------------------------
@apify_app.command("connect")
def apify_connect(
    ctx: typer.Context,
    from_env: bool = typer.Option(False, "--from-env", help="Ambil token dari APIFY_TOKEN di .env alih-alih mengetiknya."),
) -> None:
    """Hubungkan atau ganti token Apify milik pengguna ini. Token divalidasi lalu disimpan terenkripsi."""
    from spi.credentials import LEGACY_TOKEN_ENV, CredentialError, CredentialService
    from spi.crypto import MasterKeyError
    from spi.scrapers.base import ScraperError

    st = _state(ctx)
    db = st.open_db()
    try:
        user_id = st.user_id(db)
        if from_env:
            token = (st.env.get(LEGACY_TOKEN_ENV) or "").strip()
            if not token:
                typer.echo(f"{LEGACY_TOKEN_ENV} kosong di .env.", err=True)
                raise typer.Exit(code=1)
        else:
            token = typer.prompt("Token Apify (tidak ditampilkan saat diketik)", hide_input=True)
        try:
            cred = CredentialService(db, st.env).connect(user_id, token)
        except (MasterKeyError, CredentialError, ScraperError) as e:
            typer.echo(f"Token tidak disimpan: {e}", err=True)
            raise typer.Exit(code=1)
    finally:
        db.close()
    typer.echo(
        f"Token Apify terhubung untuk pengguna {st.user_name}: akun {cred['apify_username'] or cred['apify_user_id']} "
        f"(token ...{cred['token_last4']}, tersimpan terenkripsi)."
    )
    if from_env:
        typer.echo(f"Sekarang hapus baris {LEGACY_TOKEN_ENV} dari .env agar token tidak lagi tersimpan sebagai teks biasa.")


@apify_app.command("disconnect")
def apify_disconnect(ctx: typer.Context) -> None:
    """Cabut token Apify milik pengguna ini dan hapus dari database."""
    from spi.credentials import CredentialService

    st = _state(ctx)
    db = st.open_db()
    try:
        removed = CredentialService(db, st.env).disconnect(st.user_id(db))
    finally:
        db.close()
    if removed:
        typer.echo(f"Token Apify pengguna {st.user_name} dicabut dan dihapus dari database.")
        typer.echo("Untuk mematikan token itu sepenuhnya, hapus juga di Apify Console -> Settings -> Integrations.")
    else:
        typer.echo(f"Pengguna {st.user_name} tidak punya token Apify yang tersimpan.")


@apify_app.command("status")
def apify_status(ctx: typer.Context) -> None:
    """Akun Apify yang terhubung dan pemakaian bulan berjalan milik pengguna ini."""
    from spi.credentials import CredentialError, CredentialService
    from spi.crypto import DecryptError, MasterKeyError
    from spi.scrapers.base import ScraperError

    st = _state(ctx)
    db = st.open_db()
    try:
        user_id = st.user_id(db)
        svc = CredentialService(db, st.env)
        cred = svc.status(user_id)
        legacy = svc.uses_legacy_env_token(user_id)
        typer.echo(f"Pengguna   : {st.user_name}")
        if cred:
            typer.echo(f"Akun Apify : {cred['apify_username'] or '?'} (id {cred['apify_user_id']})")
            typer.echo(f"Token      : ...{cred['token_last4']}, terenkripsi, terhubung {cred['created_at'][:10]}")
        elif legacy:
            typer.echo("Token      : APIFY_TOKEN dari .env (cara lama, belum terenkripsi)")
            typer.echo("             Pindahkan dengan: spi key init, lalu spi apify connect --from-env")
        else:
            typer.echo("Token      : belum terhubung. Hubungkan dengan: spi apify connect")
            return
        try:
            usage = svc.usage(user_id)
            typer.echo(
                f"Pemakaian  : ${usage['used_usd']:.2f} dari ${usage['limit_usd']:.2f}, sisa ${usage['remaining_usd']:.2f} "
                f"(siklus berakhir {usage['cycle_end'] or '-'})"
            )
        except (CredentialError, MasterKeyError, DecryptError, ScraperError) as e:
            typer.echo(f"Pemakaian  : tidak bisa dibaca ({getattr(e, 'kind', type(e).__name__)}): {e}")
        revoked = [h for h in svc.history(user_id) if h["status"] == "revoked"]
        if revoked:
            typer.echo(f"Riwayat    : {len(revoked)} token lama sudah dicabut dan dihapus")
    finally:
        db.close()


# ---- jobs --------------------------------------------------------------------------
@app.command()
def jobs(
    ctx: typer.Context,
    run: Optional[str] = typer.Option(None, "--run", help="Tampilkan rincian satu run (RUN_ID)."),
    limit: int = typer.Option(10, "--limit", help="Jumlah run dan eksekusi terbaru yang ditampilkan."),
) -> None:
    """Riwayat job dan eksekusi Actor Apify milik pengguna ini."""
    st = _state(ctx)
    db = st.open_db()
    try:
        user_id = st.user_id(db)
        if run:
            if db.run_owner(run) != user_id:
                typer.echo(f"run {run} tidak ditemukan untuk pengguna ini", err=True)
                raise typer.Exit(code=1)
            typer.echo(f"Run {run} ({st.user_name})\n")
            _echo_summary(db.run_summary(run))
            executions = db.list_executions(user_id, run_id=run, limit=200)
        else:
            runs = db.list_runs(user_id, limit=limit)
            if not runs:
                typer.echo(f"Pengguna {st.user_name} belum punya job.")
                return
            typer.echo(f"Job milik {st.user_name}:")
            typer.echo(f"{'Run':24} {'Mulai':16} {'Akun':>4} {'OK':>3} {'Gagal':>5} {'Post':>6} {'Komentar':>8}  Kredensial")
            for r in runs:
                cred = f"#{r['credential_id']}" if r["credential_id"] else "-"
                typer.echo(
                    f"{r['run_id']:24} {r['started_at'][:16]:16} {r['accounts']:>4} {r['ok']:>3} {r['not_ok']:>5} "
                    f"{r['posts_found']:>6} {r['comments_found']:>8}  {cred}"
                )
            executions = db.list_executions(user_id, limit=limit)
        if executions:
            typer.echo("\nEksekusi Actor Apify (terbaru dulu):")
            typer.echo(f"{'Waktu':16} {'Actor':36} {'Status':11} {'Hasil':>5} {'Biaya*':>8}  Error")
            for e in executions:
                cost = f"${e['usage_usd']:.4f}" if e["usage_usd"] is not None else "-"
                items = e["items"] if e["items"] is not None else "-"
                typer.echo(
                    f"{e['started_at'][:16]:16} {e['actor'][:36]:36} {e['status']:11} {items:>5} {cost:>8}  {e['error_kind'] or ''}"
                )
            typer.echo(
                "\n* Angka yang dilaporkan Apify saat run selesai; biaya per hasil bisa belum termasuk. "
                "Pemakaian sebenarnya: spi apify status"
            )
    finally:
        db.close()


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
        run_id = db.latest_run_id(user_id=st.user_id(db))
        if run_id:
            typer.echo(f"\nRun terakhir ({st.user_name}): {run_id}")
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
