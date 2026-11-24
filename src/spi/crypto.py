"""Enkripsi token saat disimpan (AES-256-GCM).

- Kunci induk 32 byte, base64, di variabel lingkungan `SPI_MASTER_KEY` (di luar database).
- Nonce acak 12 byte untuk setiap token.
- Data terautentikasi tambahan (AAD) memuat id pemilik, sehingga ciphertext seorang pengguna tidak bisa
  dibuka bila dipindah ke baris pengguna lain.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MASTER_KEY_ENV = "SPI_MASTER_KEY"
KEY_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12


class MasterKeyError(Exception):
    """Kunci induk tidak ada atau tidak sah."""


class DecryptError(Exception):
    """Token tersimpan tidak bisa dibuka (kunci salah, data rusak, atau bukan milik pengguna ini)."""


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def load_master_key(env: dict[str, str]) -> bytes:
    raw = (env.get(MASTER_KEY_ENV) or "").strip()
    hint = f"Jalankan `spi key init` untuk membuat {MASTER_KEY_ENV} di .env."
    if not raw:
        raise MasterKeyError(f"Kunci induk enkripsi belum ada. {hint}")
    try:
        key = base64.urlsafe_b64decode(raw.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise MasterKeyError(f"{MASTER_KEY_ENV} bukan base64 yang sah. {hint}") from None
    if len(key) != KEY_BYTES:
        raise MasterKeyError(f"{MASTER_KEY_ENV} harus {KEY_BYTES} byte. {hint}")
    return key


def _aad(user_id: int) -> bytes:
    return f"spi:apify:user:{int(user_id)}".encode("ascii")


def encrypt_token(key: bytes, token: str, user_id: int) -> tuple[bytes, bytes]:
    """Mengembalikan (nonce, ciphertext). Ciphertext sudah memuat tag autentikasi GCM."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, token.encode("utf-8"), _aad(user_id))


def decrypt_token(key: bytes, nonce: bytes, ciphertext: bytes, user_id: int) -> str:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, _aad(user_id)).decode("utf-8")
    except (InvalidTag, ValueError):
        raise DecryptError(
            "Token tersimpan tidak bisa dibuka: kunci induk berbeda dari saat token disimpan, atau data rusak. "
            "Hubungkan ulang token dengan `spi apify connect`."
        ) from None
