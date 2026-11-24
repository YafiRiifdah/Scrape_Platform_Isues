"""Bagian bersama semua adapter Apify: kumpulan token, panggilan actor sinkron, dan penerjemahan error.

Memakai endpoint `run-sync-get-dataset-items`, jadi satu panggilan HTTP = satu run actor.

Token dibaca dari .env:
- `APIFY_TOKEN`  : satu token (cara lama, tetap didukung)
- `APIFY_TOKENS` : beberapa token dipisah koma. Bila kredit sebuah token habis atau sisanya di bawah
  `scrape.apify_min_remaining_usd`, pemanggilan berikutnya pindah ke token selanjutnya.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

from spi.scrapers.base import RateLimited, ScrapeBlocked, ScraperError

log = logging.getLogger("spi.scrapers.apify")

APIFY_BASE = "https://api.apify.com/v2"
SYNC_TIMEOUT_SECONDS = 300
TOKEN_HINT = (
    "Isi APIFY_TOKEN (satu token) atau APIFY_TOKENS (beberapa token dipisah koma) di .env "
    "(Apify Console -> Settings -> Integrations -> Personal API tokens)."
)
COMMENT_BATCH_SIZE = 50
CONNECTION_RETRY_DELAYS = (5, 20)
URL_LIST_KEYS = ("postURLs", "directUrls")  # tidak dicetak ke log karena panjang
DEFAULT_MIN_REMAINING_USD = 0.25
_EXHAUSTED_HINTS = ("usage", "limit", "exceeded", "insufficient funds", "credit")

RunActor = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


# ---------------------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------------------
def tokens_from_env(env: dict[str, str]) -> list[str]:
    """APIFY_TOKEN lebih dulu (bila ada), lalu isi APIFY_TOKENS; tanpa duplikat."""
    raw = [env.get("APIFY_TOKEN") or ""] + (env.get("APIFY_TOKENS") or "").split(",")
    tokens: list[str] = []
    for t in (r.strip() for r in raw):
        if t and t not in tokens:
            tokens.append(t)
    return tokens


def mask_token(token: str) -> str:
    return "..." + token[-4:] if len(token) > 8 else "..."


def is_credit_exhausted(status: int, body: str) -> bool:
    """Apakah respons Apify berarti kredit/batas pemakaian akun ini sudah habis."""
    if status == 402:
        return True
    return status == 403 and any(h in (body or "").lower() for h in _EXHAUSTED_HINTS)


class ApifyTokenPool:
    """Daftar token berurutan. Token yang kreditnya habis atau ditolak dilewati sampai proses selesai."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = list(tokens)
        self.exhausted: dict[str, str] = {}  # token -> alasan

    def current(self) -> Optional[str]:
        for t in self.tokens:
            if t not in self.exhausted:
                return t
        return None

    def position(self, token: str) -> str:
        return f"{self.tokens.index(token) + 1}/{len(self.tokens)}"

    def mark_exhausted(self, token: str, reason: str) -> None:
        self.exhausted[token] = reason
        nxt = self.current()
        if nxt:
            log.warning("token Apify %s (%s) dilewati: %s. Pindah ke token %s.", self.position(token), mask_token(token), reason, self.position(nxt))
        else:
            log.error("token Apify %s (%s) dilewati: %s. Tidak ada token lain yang tersisa.", self.position(token), mask_token(token), reason)

    def summary(self) -> str:
        return "; ".join(f"token {self.position(t)}: {self.exhausted.get(t, 'tersedia')}" for t in self.tokens)

    @staticmethod
    def fetch_limits(token: str, http_get: Callable[..., Any]) -> dict[str, Any]:
        """Pemakaian bulan berjalan sebuah token. Melempar exception bila gagal."""
        resp = http_get(f"{APIFY_BASE}/users/me/limits", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code >= 400:
            raise ScraperError(f"HTTP {resp.status_code}")
        data = (resp.json() or {}).get("data") or {}
        used = float((data.get("current") or {}).get("monthlyUsageUsd") or 0)
        limit = float((data.get("limits") or {}).get("maxMonthlyUsageUsd") or 0)
        cycle_end = str((data.get("monthlyUsageCycle") or {}).get("endAt") or "")[:10]
        return {"used_usd": used, "limit_usd": limit, "remaining_usd": limit - used, "cycle_end": cycle_end}

    def report(self, http_get: Callable[..., Any] = requests.get) -> list[dict[str, Any]]:
        """Satu baris per token: akun, pemakaian, sisa kredit. Token ditampilkan tersamar."""
        active = self.current()
        rows = []
        for t in self.tokens:
            row: dict[str, Any] = {
                "position": self.position(t), "token": mask_token(t), "active": t == active, "username": None,
                "used_usd": None, "limit_usd": None, "remaining_usd": None, "cycle_end": None,
                "skipped": self.exhausted.get(t), "error": None,
            }
            try:
                row.update(self.fetch_limits(t, http_get))
                me = http_get(f"{APIFY_BASE}/users/me", headers={"Authorization": f"Bearer {t}"}, timeout=30)
                if me.status_code < 400:
                    row["username"] = ((me.json() or {}).get("data") or {}).get("username")
            except Exception as e:  # noqa: BLE001 - laporan tidak boleh gagal karena satu token
                row["error"] = str(e).replace(t, "***")[:120] or type(e).__name__
            rows.append(row)
        return rows


_POOLS: dict[tuple[str, ...], ApifyTokenPool] = {}


def get_pool(env: dict[str, str]) -> ApifyTokenPool:
    """Pool bersama untuk satu proses, agar token yang sudah habis diingat lintas akun dan platform."""
    key = tuple(tokens_from_env(env))
    if key not in _POOLS:
        _POOLS[key] = ApifyTokenPool(list(key))
    return _POOLS[key]


# ---------------------------------------------------------------------------------------
# Respons
# ---------------------------------------------------------------------------------------
def raise_for_apify_status(status: int, body: str) -> None:
    if status < 400:
        return
    if status == 429:
        raise RateLimited(f"Apify membalas 429 (batas laju): {body[:200]}")
    if status in (401, 403):
        raise ScrapeBlocked(f"Apify menolak token (HTTP {status}). {TOKEN_HINT} Detail: {body[:200]}")
    if status == 402:
        raise ScrapeBlocked(f"Kredit Apify habis (HTTP 402). Isi saldo atau naikkan paket. Detail: {body[:200]}")
    if status == 408:
        raise ScraperError(
            "Run Apify melewati batas waktu sinkron 300 detik. Kecilkan max_posts_per_account atau "
            "max_comments_per_post lalu coba lagi."
        )
    raise ScraperError(f"Apify membalas HTTP {status}: {body[:300]}")


def parse_iso_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------------------
# Klien
# ---------------------------------------------------------------------------------------
class ApifyBase:
    """Menyediakan `self._run_actor(actor, payload) -> list[dict]` untuk adapter turunan."""

    def __init__(
        self,
        settings: dict[str, Any],
        run_actor: Optional[RunActor] = None,
        http_post: Callable[..., Any] = requests.post,
        retry_sleep: Callable[[float], None] = time.sleep,
        http_get: Callable[..., Any] = requests.get,
        pool: Optional[ApifyTokenPool] = None,
    ) -> None:
        self.settings = settings
        self._run_actor = run_actor or self._run_actor_http
        self._http_post = http_post
        self._http_get = http_get
        self._retry_sleep = retry_sleep
        self.pool = pool if pool is not None else get_pool(settings.get("env", {}))

    def _token(self) -> str:
        if not self.pool.tokens:
            raise ScrapeBlocked("APIFY_TOKEN belum diisi. " + TOKEN_HINT)
        token = self.pool.current()
        if token is None:
            raise self._all_exhausted()
        return token

    def _all_exhausted(self) -> ScrapeBlocked:
        return ScrapeBlocked(
            f"Semua token Apify ({len(self.pool.tokens)}) kehabisan kredit atau ditolak. Kredit Apify habis: "
            f"tunggu siklus bulan berikutnya, tambah token di APIFY_TOKENS, atau naikkan paket. Rincian: {self.pool.summary()}"
        )

    def _scrub(self, text: str) -> str:
        for token in self.pool.tokens:
            text = text.replace(token, "***")
        return text

    def _min_remaining(self) -> float:
        return float(getattr(self.settings.get("scrape"), "apify_min_remaining_usd", DEFAULT_MIN_REMAINING_USD))

    def _has_enough_credit(self, token: str) -> bool:
        """Cek sisa kredit sebelum menjalankan actor. Kegagalan pengecekan tidak menghalangi run."""
        try:
            remaining = ApifyTokenPool.fetch_limits(token, self._http_get)["remaining_usd"]
        except Exception as e:  # noqa: BLE001
            log.debug("cek kredit token %s gagal (%s), lanjut tanpa pengecekan", self.pool.position(token), type(e).__name__)
            return True
        if remaining < self._min_remaining():
            self.pool.mark_exhausted(token, f"sisa kredit ${remaining:.2f} di bawah batas ${self._min_remaining():.2f}")
            return False
        return True

    def _post_with_retry(self, url: str, **kwargs: Any) -> Any:
        for attempt, delay in enumerate(CONNECTION_RETRY_DELAYS + (None,), start=1):
            try:
                return self._http_post(url, **kwargs)
            except requests.RequestException as e:
                msg = self._scrub(str(e))
                if delay is None:
                    raise ScraperError(f"koneksi ke Apify gagal setelah {attempt} percobaan: {msg}") from None
                log.warning("koneksi ke Apify gagal (%s), coba lagi dalam %ds", msg, delay)
                self._retry_sleep(delay)
        raise AssertionError("unreachable")

    def _run_actor_http(self, actor: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items"
        params = {"timeout": SYNC_TIMEOUT_SECONDS, "format": "json", "clean": "true"}
        shown = ", ".join(
            f"{k}={len(v)} URL" if k in URL_LIST_KEYS and len(v) > 1 else f"{k}={v!r}" for k, v in payload.items()
        )
        multi = len(self.pool.tokens) > 1
        while True:
            token = self._token()  # melempar ScrapeBlocked bila tidak ada token yang tersisa
            if multi and not self._has_enough_credit(token):
                continue
            log.info("Apify %s: menjalankan actor (%s)%s", actor, shown, f" [token {self.pool.position(token)}]" if multi else "")
            # token di header, bukan di URL, agar tidak bocor ke log
            resp = self._post_with_retry(
                url, params=params, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=SYNC_TIMEOUT_SECONDS + 30
            )
            body = self._scrub(resp.text)
            if is_credit_exhausted(resp.status_code, body):
                self.pool.mark_exhausted(token, f"kredit habis (HTTP {resp.status_code})")
                continue
            if resp.status_code == 401 and multi:
                self.pool.mark_exhausted(token, "token ditolak (HTTP 401)")
                continue
            raise_for_apify_status(resp.status_code, body)
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise ScraperError(f"respons Apify bukan JSON: {body[:200]}") from e
            return data if isinstance(data, list) else []
