import base64

import pytest

from spi import crypto

TOKEN = "apify_api_RAHASIA_SEKALI_1234"


def test_generated_master_key_is_32_bytes_base64():
    key_text = crypto.generate_master_key()
    assert len(base64.urlsafe_b64decode(key_text)) == 32
    assert crypto.generate_master_key() != key_text


def test_roundtrip_returns_original_token():
    key = crypto.load_master_key({"SPI_MASTER_KEY": crypto.generate_master_key()})
    nonce, ciphertext = crypto.encrypt_token(key, TOKEN, user_id=1)
    assert crypto.decrypt_token(key, nonce, ciphertext, user_id=1) == TOKEN


def test_ciphertext_does_not_contain_plaintext_and_nonce_is_unique():
    key = crypto.load_master_key({"SPI_MASTER_KEY": crypto.generate_master_key()})
    n1, c1 = crypto.encrypt_token(key, TOKEN, user_id=1)
    n2, c2 = crypto.encrypt_token(key, TOKEN, user_id=1)
    assert TOKEN.encode() not in c1 and TOKEN.encode() not in n1
    assert len(n1) == 12
    assert n1 != n2 and c1 != c2


def test_decrypt_fails_with_another_users_id():
    """Ciphertext terikat ke pemiliknya: disalin ke baris pengguna lain pun tidak bisa dibuka."""
    key = crypto.load_master_key({"SPI_MASTER_KEY": crypto.generate_master_key()})
    nonce, ciphertext = crypto.encrypt_token(key, TOKEN, user_id=1)
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt_token(key, nonce, ciphertext, user_id=2)


def test_decrypt_fails_with_wrong_key_or_tampered_data():
    key = crypto.load_master_key({"SPI_MASTER_KEY": crypto.generate_master_key()})
    other = crypto.load_master_key({"SPI_MASTER_KEY": crypto.generate_master_key()})
    nonce, ciphertext = crypto.encrypt_token(key, TOKEN, user_id=1)
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt_token(other, nonce, ciphertext, user_id=1)
    tampered = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt_token(key, nonce, tampered, user_id=1)


def test_decrypt_error_never_mentions_the_token():
    key = crypto.load_master_key({"SPI_MASTER_KEY": crypto.generate_master_key()})
    nonce, ciphertext = crypto.encrypt_token(key, TOKEN, user_id=1)
    with pytest.raises(crypto.DecryptError) as exc:
        crypto.decrypt_token(key, nonce, ciphertext, user_id=9)
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("value", [None, "", "bukan-base64!!", base64.urlsafe_b64encode(b"pendek").decode()])
def test_missing_or_invalid_master_key_is_rejected_with_hint(value):
    env = {} if value is None else {"SPI_MASTER_KEY": value}
    with pytest.raises(crypto.MasterKeyError, match="spi key init"):
        crypto.load_master_key(env)
