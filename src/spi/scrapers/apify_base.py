"""Bagian bersama semua adapter Apify: token, panggilan actor sinkron, dan penerjemahan error.

Memakai endpoint `run-sync-get-dataset-items`, jadi satu panggilan HTTP = satu run actor.
Butuh `APIFY_TOKEN` di .env (Apify Console -> Settings -> Integrations -> API token).
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
TOKEN_HINT = "Isi APIFY_TOKEN di .env (Apify Console -> Settings -> Integrations -> Personal API tokens)."
COMMENT_BATCH_SIZE = 50
CONNECTION_RETRY_DELAYS = (5, 20)
URL_LIST_KEYS = ("postURLs", "directUrls")  # tidak dicetak ke log karena panjang

RunActor = Callable[[str, dict[str, Any]], list[dict[str, Any]]]


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


class ApifyBase:
    """Menyediakan `self._run_actor(actor, payload) -> list[dict]` untuk adapter turunan."""

    def __init__(
        self,
        settings: dict[str, Any],
        run_actor: Optional[RunActor] = None,
        http_post: Callable[..., Any] = requests.post,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._run_actor = run_actor or self._run_actor_http
        self._http_post = http_post
        self._retry_sleep = retry_sleep

    def _token(self) -> str:
        token = self.settings.get("env", {}).get("APIFY_TOKEN")
        if not token:
            raise ScrapeBlocked("APIFY_TOKEN belum diisi. " + TOKEN_HINT)
        return token

    def _scrub(self, text: str) -> str:
        token = self.settings.get("env", {}).get("APIFY_TOKEN")
        return text.replace(token, "***") if token else text

    def _run_actor_http(self, actor: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        token = self._token()
        url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items"
        params = {"timeout": SYNC_TIMEOUT_SECONDS, "format": "json", "clean": "true"}
        headers = {"Authorization": f"Bearer {token}"}  # token di header, bukan di URL, agar tidak bocor ke log
        shown = ", ".join(
            f"{k}={len(v)} URL" if k in URL_LIST_KEYS and len(v) > 1 else f"{k}={v!r}" for k, v in payload.items()
        )
        log.info("Apify %s: menjalankan actor (%s)", actor, shown)
        resp = None
        for attempt, delay in enumerate(CONNECTION_RETRY_DELAYS + (None,), start=1):
            try:
                resp = self._http_post(url, params=params, headers=headers, json=payload, timeout=SYNC_TIMEOUT_SECONDS + 30)
                break
            except requests.RequestException as e:
                msg = self._scrub(str(e))
                if delay is None:
                    raise ScraperError(f"koneksi ke Apify gagal setelah {attempt} percobaan: {msg}") from None
                log.warning("koneksi ke Apify gagal (%s), coba lagi dalam %ds", msg, delay)
                self._retry_sleep(delay)
        raise_for_apify_status(resp.status_code, self._scrub(resp.text))
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ScraperError(f"respons Apify bukan JSON: {resp.text[:200]}") from e
        return data if isinstance(data, list) else []
