"""Layanan kredensial Apify per pengguna (Bring Your Own Key).

Setiap fungsi menerima `user_id` pemanggil dan hanya menyentuh data milik pengguna itu. Tidak ada fungsi
yang menerima id kredensial atau memilih token milik pengguna lain, sehingga tidak ada jalur untuk
berpindah token antar pengguna atau menggabungkan kredit.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from spi import crypto
from spi.scrapers.apify_base import CONNECT_HINT, ApifyAccess, ApifyClient
from spi.storage import Storage

log = logging.getLogger("spi.credentials")

LEGACY_TOKEN_ENV = "APIFY_TOKEN"


class CredentialError(Exception):
    """Kredensial tidak bisa disimpan atau dipakai."""


class CredentialMissing(CredentialError):
    """Pengguna ini belum menghubungkan token Apify."""


class CredentialService:
    def __init__(self, db: Storage, env: dict[str, str], client_factory: Optional[Callable[[str], Any]] = None) -> None:
        self.db = db
        self.env = env
        self._client_factory = client_factory or ApifyClient

    # ---- hubungkan, ganti, cabut ---------------------------------------------------
    def connect(self, user_id: int, token: str) -> dict[str, Any]:
        """Validasi token ke Apify, lalu simpan terenkripsi sebagai kredensial aktif milik `user_id`.

        Kredensial aktif sebelumnya (bila ada) dicabut dan tokennya dihapus: inilah cara mengganti token.
        """
        token = (token or "").strip()
        if not token:
            raise CredentialError("Token Apify kosong.")
        key = crypto.load_master_key(self.env)  # gagal lebih awal sebelum menghubungi Apify
        identity = self._client_factory(token).whoami()  # melempar ApifyInvalidToken bila token ditolak
        nonce, ciphertext = crypto.encrypt_token(key, token, user_id)
        credential_id = self.db.replace_credential(
            user_id,
            apify_user_id=identity.get("apify_user_id"),
            apify_username=identity.get("apify_username"),
            nonce=nonce,
            ciphertext=ciphertext,
            last4=token[-4:],
            key_version=crypto.KEY_VERSION,
        )
        log.info(
            "pengguna %s: token Apify terhubung (akun %s, kredensial %s)", user_id, identity.get("apify_username"), credential_id
        )
        status = self.status(user_id)
        assert status is not None
        return status

    def disconnect(self, user_id: int) -> bool:
        """Cabut kredensial aktif milik `user_id` dan hapus token terenkripsinya. True bila ada yang dicabut."""
        revoked = self.db.revoke_credentials(user_id) > 0
        if revoked:
            log.info("pengguna %s: token Apify dicabut dan dihapus", user_id)
        return revoked

    # ---- tampilan tanpa rahasia ------------------------------------------------------
    def status(self, user_id: int) -> Optional[dict[str, Any]]:
        """Kredensial aktif milik `user_id` tanpa token (hanya empat karakter terakhir)."""
        for row in self.db.list_credentials(user_id):
            if row["status"] == "active":
                return row
        return None

    def history(self, user_id: int) -> list[dict[str, Any]]:
        return self.db.list_credentials(user_id)

    def uses_legacy_env_token(self, user_id: int) -> bool:
        """Apakah job pengguna ini akan memakai APIFY_TOKEN lama dari .env (hanya pengguna default)."""
        return (
            self.db.active_credential(user_id) is None
            and user_id == self.db.default_user_id()
            and bool((self.env.get(LEGACY_TOKEN_ENV) or "").strip())
        )

    # ---- pemilihan kredensial untuk job -------------------------------------------------
    def resolve_for_job(self, user_id: int) -> ApifyAccess:
        """Token milik pemilik job. Hanya kredensial aktif `user_id` yang pernah dibuka.

        Kompatibilitas: pengguna default yang belum menghubungkan token masih boleh memakai APIFY_TOKEN
        dari .env (cara lama). Pengguna lain tidak pernah mendapat token itu.
        """
        row = self.db.active_credential(user_id)
        if row is not None:
            key = crypto.load_master_key(self.env)
            token = crypto.decrypt_token(key, row["token_nonce"], row["token_ciphertext"], user_id)
            return ApifyAccess(token=token, user_id=user_id, credential_id=row["credential_id"], source="stored")
        if self.uses_legacy_env_token(user_id):
            log.warning(
                "memakai APIFY_TOKEN dari .env untuk pengguna default (cara lama, tidak terenkripsi). "
                "Pindahkan dengan `spi key init` lalu `spi apify connect --from-env`."
            )
            return ApifyAccess(token=self.env[LEGACY_TOKEN_ENV].strip(), user_id=user_id, credential_id=None, source="env")
        raise CredentialMissing(f"Belum ada token Apify untuk pengguna ini. {CONNECT_HINT}")

    # ---- pemakaian ---------------------------------------------------------------------
    def usage(self, user_id: int) -> dict[str, Any]:
        """Pemakaian bulan berjalan akun Apify milik `user_id` (dibaca dengan token pengguna itu sendiri)."""
        access = self.resolve_for_job(user_id)
        return self._client_factory(access.token).usage()
