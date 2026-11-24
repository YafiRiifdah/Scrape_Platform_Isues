"""Bagian bersama semua adapter Apify: klien HTTP, klasifikasi error, dan akses per pengguna.

Aturan BYOK yang ditegakkan di sini:
- Satu klien memegang SATU token, milik pemilik job. Tidak ada daftar token dan tidak ada perpindahan token.
- Bila kuota akun itu habis atau tokennya ditolak, job gagal dengan jenis error yang jelas.
- Token dikirim di header Authorization, tidak pernah di URL, dan disaring dari semua pesan error.

Alur satu eksekusi Actor: mulai run -> pantau status -> ambil dataset, semuanya dengan token yang sama.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

from spi.scrapers.base import RateLimited, ScrapeBlocked, ScraperError

log = logging.getLogger("spi.scrapers.apify")

APIFY_BASE = "https://api.apify.com/v2"
RUN_MAX_WAIT_SECONDS = 600  # batas tunggu satu run sebelum dihentikan
POLL_WAIT_SECONDS = 60  # nilai maksimum waitForFinish di API Apify
POLL_PAUSE_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = POLL_WAIT_SECONDS + 30
COMMENT_BATCH_SIZE = 50
CONNECTION_RETRY_DELAYS = (5, 20)
URL_LIST_KEYS = ("postURLs", "directUrls")  # tidak dicetak ke log karena panjang
RUN_OK = "SUCCEEDED"
RUN_FAILED_STATUSES = ("FAILED", "ABORTED", "TIMED-OUT")
CONNECT_HINT = (
    "Hubungkan token Apify milik pengguna ini dengan `spi apify connect` "
    "(Apify Console -> Settings -> Integrations -> Personal API tokens)."
)
_CREDIT_HINTS = ("usage", "exceeded", "insufficient funds", "insufficient-funds", "credit", "hard limit", "not-enough")

RunActor = Callable[[str, dict[str, Any]], list[dict[str, Any]]]
ExecutionHook = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------------------
class ApifyError(ScraperError):
    """Error dari Apify. `kind` membedakan penyebabnya untuk riwayat eksekusi dan pesan ke pengguna."""

    kind = "error"


class ApifyRateLimited(ApifyError, RateLimited):
    kind = "rate_limit"


class ApifyInsufficientCredits(ApifyError, ScrapeBlocked):
    kind = "insufficient_credits"


class ApifyInvalidToken(ApifyError, ScrapeBlocked):
    kind = "invalid_token"


class ApifyPermissionDenied(ApifyError, ScrapeBlocked):
    kind = "permission"


class ApifyCredentialMissing(ApifyError, ScrapeBlocked):
    kind = "no_credential"


class ApifyRunFailed(ApifyError):
    kind = "run_failed"


class ApifyTimeout(ApifyError):
    kind = "timeout"


class ApifyConnectionError(ApifyError):
    kind = "connection"


def classify_status(status: int, body: str) -> Optional[str]:
    """Jenis error untuk sebuah respons HTTP Apify, atau None bila sukses."""
    if status < 400:
        return None
    if status == 429:
        return "rate_limit"
    if status == 401:
        return "invalid_token"
    if status == 402:
        return "insufficient_credits"
    if status == 403:
        return "insufficient_credits" if any(h in (body or "").lower() for h in _CREDIT_HINTS) else "permission"
    return "error"


def raise_for_apify_status(status: int, body: str) -> None:
    kind = classify_status(status, body)
    detail = (body or "")[:200]
    if kind is None:
        return
    if kind == "rate_limit":
        raise ApifyRateLimited(f"Apify membalas 429 (batas laju): {detail}")
    if kind == "invalid_token":
        raise ApifyInvalidToken(
            f"Apify menolak token (HTTP 401): token salah atau sudah dicabut. Ganti dengan `spi apify connect`. Detail: {detail}"
        )
    if kind == "insufficient_credits":
        raise ApifyInsufficientCredits(
            f"Kredit atau kuota akun Apify pengguna ini habis (HTTP {status}). Tunggu siklus tagihan berikutnya "
            f"atau naikkan paket akun tersebut. Detail: {detail}"
        )
    if kind == "permission":
        raise ApifyPermissionDenied(
            f"Token Apify tidak punya izin untuk tindakan ini (HTTP 403). Periksa cakupan (scope) token. Detail: {detail}"
        )
    raise ApifyError(f"Apify membalas HTTP {status}: {(body or '')[:300]}")


def parse_iso_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------
# Akses milik satu pengguna
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ApifyAccess:
    """Token milik satu pengguna untuk satu job. Token tidak ikut tercetak di repr maupun log."""

    token: str = field(repr=False)
    user_id: Optional[int] = None
    credential_id: Optional[int] = None  # None = token lama dari .env (hanya pengguna default)
    source: str = "stored"  # "stored" | "env"
    on_execution: Optional[ExecutionHook] = field(default=None, repr=False, compare=False)


# ---------------------------------------------------------------------------------------
# Klien: satu token
# ---------------------------------------------------------------------------------------
class ApifyClient:
    """Klien API Apify untuk tepat satu token. Tidak punya cadangan token lain."""

    def __init__(
        self,
        token: str,
        http_post: Optional[Callable[..., Any]] = None,
        http_get: Optional[Callable[..., Any]] = None,
        retry_sleep: Callable[[float], None] = time.sleep,
        on_execution: Optional[ExecutionHook] = None,
        max_wait_seconds: float = RUN_MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._http_post = http_post or requests.post
        self._http_get = http_get or requests.get
        self._retry_sleep = retry_sleep
        self._on_execution = on_execution
        self._max_wait = max_wait_seconds
        self._clock = clock

    def __repr__(self) -> str:  # jangan pernah mencetak token
        return "ApifyClient(token=***)"

    # ---- HTTP ---------------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _request(self, fn: Callable[..., Any], path: str, **kwargs: Any) -> Any:
        """Panggilan HTTP dengan token di header. Gangguan koneksi dicoba ulang; status HTTP diperiksa."""
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
        headers = {"Authorization": f"Bearer {self._token}"}
        for attempt, delay in enumerate(CONNECTION_RETRY_DELAYS + (None,), start=1):
            try:
                resp = fn(f"{APIFY_BASE}{path}", headers=headers, **kwargs)
                break
            except requests.RequestException as e:
                msg = self._scrub(str(e))
                if delay is None:
                    raise ApifyConnectionError(f"koneksi ke Apify gagal setelah {attempt} percobaan: {msg}") from None
                log.warning("koneksi ke Apify gagal (%s), coba lagi dalam %ds", msg, delay)
                self._retry_sleep(delay)
        raise_for_apify_status(resp.status_code, self._scrub(getattr(resp, "text", "") or ""))
        return resp

    def _json(self, resp: Any) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise ApifyError(f"respons Apify bukan JSON: {self._scrub(getattr(resp, 'text', '') or '')[:200]}") from e

    def _data(self, resp: Any) -> dict[str, Any]:
        body = self._json(resp)
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else {}

    # ---- akun ---------------------------------------------------------------------
    def whoami(self) -> dict[str, Any]:
        """Identitas akun Apify pemilik token. Dipakai untuk memvalidasi token saat dihubungkan."""
        data = self._data(self._request(self._http_get, "/users/me", timeout=30))
        if not data.get("id"):
            raise ApifyInvalidToken("Apify tidak mengembalikan identitas akun untuk token ini.")
        return {"apify_user_id": str(data["id"]), "apify_username": data.get("username")}

    def usage(self) -> dict[str, Any]:
        """Pemakaian bulan berjalan akun pemilik token."""
        data = self._data(self._request(self._http_get, "/users/me/limits", timeout=30))
        used = float((data.get("current") or {}).get("monthlyUsageUsd") or 0)
        limit = float((data.get("limits") or {}).get("maxMonthlyUsageUsd") or 0)
        cycle_end = str((data.get("monthlyUsageCycle") or {}).get("endAt") or "")[:10]
        return {"used_usd": used, "limit_usd": limit, "remaining_usd": limit - used, "cycle_end": cycle_end}

    # ---- eksekusi Actor -------------------------------------------------------------
    def run_actor(self, actor: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Jalankan Actor sampai selesai dan kembalikan isi datasetnya. Selalu mencatat satu eksekusi."""
        shown = ", ".join(
            f"{k}={len(v)} URL" if k in URL_LIST_KEYS and len(v) > 1 else f"{k}={v!r}" for k, v in payload.items()
        )
        log.info("Apify %s: menjalankan actor (%s)", actor, shown)
        record: dict[str, Any] = {
            "actor": actor, "apify_run_id": None, "status": "NOT_STARTED", "items": None,
            "usage_usd": None, "error_kind": None, "started_at": _now_iso(), "finished_at": None,
        }
        try:
            run = self._data(
                self._request(self._http_post, f"/acts/{actor}/runs", params={"waitForFinish": POLL_WAIT_SECONDS}, json=payload)
            )
            run_id = str(run.get("id") or "")
            if not run_id:
                raise ApifyError("Apify tidak mengembalikan id run.")
            record["apify_run_id"] = run_id
            run = self._wait_until_finished(run, record)
            if run.get("status") != RUN_OK:
                raise ApifyRunFailed(
                    f"Run Apify {run_id} berakhir dengan status {run.get('status')}: "
                    f"{str(run.get('statusMessage') or 'tanpa keterangan')[:200]}"
                )
            items = self._json(
                self._request(
                    self._http_get, f"/datasets/{run.get('defaultDatasetId')}/items", params={"format": "json", "clean": "true"}
                )
            )
            items = items if isinstance(items, list) else []
            record["items"] = len(items)
            return items
        except ApifyError as e:
            record["error_kind"] = e.kind
            raise
        finally:
            record["finished_at"] = _now_iso()
            self._emit(record)

    def _wait_until_finished(self, run: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        run_id = record["apify_run_id"]
        deadline = self._clock() + self._max_wait
        while True:
            record["status"] = str(run.get("status") or "UNKNOWN")
            if run.get("usageTotalUsd") is not None:
                record["usage_usd"] = float(run["usageTotalUsd"])
            if record["status"] == RUN_OK or record["status"] in RUN_FAILED_STATUSES:
                return run
            if self._clock() >= deadline:
                self._abort(run_id)
                raise ApifyTimeout(
                    f"Run Apify {run_id} belum selesai setelah {self._max_wait:.0f} detik dan dihentikan. "
                    "Kecilkan max_posts_per_account atau max_comments_per_post lalu coba lagi."
                )
            self._retry_sleep(POLL_PAUSE_SECONDS)
            log.debug("Apify run %s: status %s, menunggu", run_id, record["status"])
            run = self._data(self._request(self._http_get, f"/actor-runs/{run_id}", params={"waitForFinish": POLL_WAIT_SECONDS}))

    def _abort(self, run_id: str) -> None:
        try:
            self._request(self._http_post, f"/actor-runs/{run_id}/abort", timeout=30)
        except ScraperError as e:  # penghentian hanya upaya terbaik
            log.warning("gagal menghentikan run Apify %s: %s", run_id, e)

    def _emit(self, record: dict[str, Any]) -> None:
        if self._on_execution is None:
            return
        try:
            self._on_execution(dict(record))
        except Exception:  # noqa: BLE001 - pencatatan riwayat tidak boleh menggagalkan scraping
            log.exception("gagal mencatat eksekusi Apify")


# ---------------------------------------------------------------------------------------
# Dasar adapter
# ---------------------------------------------------------------------------------------
class ApifyBase:
    """Menyediakan `self._run_actor(actor, payload) -> list[dict]` untuk adapter turunan.

    Token hanya datang dari `settings["apify"]` (ApifyAccess milik pemilik job). Adapter tidak membaca
    token dari tempat lain, sehingga job tanpa kredensial pemiliknya gagal alih-alih meminjam token lain.
    """

    def __init__(
        self,
        settings: dict[str, Any],
        run_actor: Optional[RunActor] = None,
        http_post: Optional[Callable[..., Any]] = None,
        retry_sleep: Callable[[float], None] = time.sleep,
        http_get: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.settings = settings
        self.access: Optional[ApifyAccess] = settings.get("apify")
        self._client: Optional[ApifyClient] = None
        if self.access is not None:
            self._client = ApifyClient(
                self.access.token,
                http_post=http_post,
                http_get=http_get,
                retry_sleep=retry_sleep,
                on_execution=self.access.on_execution,
            )
        self._run_actor = run_actor or self._run_actor_http

    def _require_access(self) -> ApifyAccess:
        if self.access is None:
            raise ApifyCredentialMissing(self.settings.get("apify_error") or f"Belum ada token Apify untuk pengguna ini. {CONNECT_HINT}")
        return self.access

    def _run_actor_http(self, actor: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self._require_access()
        assert self._client is not None
        return self._client.run_actor(actor, payload)
