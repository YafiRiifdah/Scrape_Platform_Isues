# Rencana Migrasi: Multi-User BYOK untuk Token Apify

Tanggal: 2026-11-24

Status: fase 1 sudah diimplementasikan dan diuji (lihat bagian 6). Fase 2 belum diputuskan.

## 1. Tujuan

Setiap pengguna menghubungkan token Apify miliknya sendiri (Bring Your Own Key). Kredensial, akun Apify,
kuota, dan tagihan adalah milik dan tanggung jawab pengguna itu. Job seorang pengguna hanya boleh memakai
kredensial pengguna tersebut.

Aturan yang tidak boleh dilanggar:

1. Satu pengguna hanya mengakses kredensial Apify miliknya.
2. Satu job hanya memakai kredensial yang terikat pada pemiliknya.
3. Tidak ada perpindahan otomatis ke token pengguna lain.
4. Tidak ada penggabungan kredit.
5. Tidak ada pembuatan akun otomatis.
6. Tidak ada mekanisme untuk mengakali batas pemakaian Apify.
7. Fungsi aplikasi yang sudah ada tetap berjalan.

## 2. Hasil pemeriksaan arsitektur saat ini

| Aspek | Kondisi sekarang |
|---|---|
| Bentuk aplikasi | CLI lokal (`spi`, Typer) + SQLite satu file. Tidak ada server, API, atau frontend. |
| Integrasi Apify | `src/spi/scrapers/apify_base.py` (`ApifyBase`), dipakai `tiktok_apify.py` dan `instagram_apify.py`. Memakai endpoint sinkron `run-sync-get-dataset-items`. |
| Kredensial | Token teks biasa di `.env` (`APIFY_TOKEN`, `APIFY_TOKENS`). |
| Rotasi token | `ApifyTokenPool` berpindah ke token berikutnya saat kredit habis. **Melanggar aturan 3, 4, dan 6. Harus dihapus.** |
| Model pengguna dan autentikasi | Tidak ada. |
| Skema database | `posts`, `comments`, `post_analysis`, `scrape_runs`, `reports`. Tidak ada kolom pemilik. |
| Alur job | `cli.scrape` -> `runner.run_scrape` -> `registry.build` -> adapter -> `ApifyBase._run_actor_http`. Satu "job" = satu baris-baris `scrape_runs` dengan `run_id` yang sama. |

## 3. Kesenjangan terhadap spesifikasi

| Kebutuhan spesifikasi | Status | Catatan |
|---|---|---|
| Kredensial per pengguna, terenkripsi | Belum ada | Dibangun di fase 1 |
| Validasi token dan penyimpanan identitas akun Apify | Belum ada | Dibangun di fase 1 |
| Putus, sambung ulang, ganti token | Belum ada | Dibangun di fase 1 |
| Job terikat ke pemilik dan kredensialnya | Belum ada | Dibangun di fase 1 |
| Tanpa fallback lintas pengguna | Dilanggar oleh pool | Diperbaiki di fase 1 |
| Pantau status eksekusi Actor | Sebagian (sinkron, tanpa status) | Diganti alur start -> pantau -> ambil dataset di fase 1 |
| Pemakaian dan riwayat job per pengguna | Belum ada | Dibangun di fase 1 |
| Pembedaan rate limit, kredit habis, token tidak sah, izin | Sebagian | Dibangun di fase 1 |
| Token tidak pernah masuk log | Sudah | Dipertahankan |
| "Pengguna terautentikasi", HTTPS, frontend | **Tidak berlaku untuk CLI** | Butuh lapisan web (fase 2, belum diputuskan) |

## 4. Rancangan fase 1 (inti BYOK di arsitektur yang ada)

### 4.1 Identitas pengguna

- Tabel `users`. Pengguna dipilih lewat opsi global `--user NAMA` atau variabel `SPI_USER`. Tanpa opsi, dipakai pengguna `default`.
- **Keterbatasan yang harus disadari:** CLI lokal tidak punya autentikasi. `--user` adalah *pemilihan* identitas, bukan pembuktian. Isolasi antar pengguna ditegakkan di lapisan layanan dan penyimpanan (setiap fungsi menerima `user_id` dan menyaring dengannya), sehingga lapisan web nanti cukup mengikat `user_id` ke sesi login. Siapa pun yang memegang komputer dan kunci induk tetap bisa bertindak sebagai pengguna mana pun.

### 4.2 Enkripsi

- AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`).
- Kunci induk 32 byte, base64, di variabel `SPI_MASTER_KEY` (di `.env`, di luar database). Dibuat dengan `spi key init`.
- Nonce acak 12 byte per token. Data terautentikasi tambahan (AAD) = `spi:apify:user:<user_id>`, sehingga ciphertext milik satu pengguna tidak bisa dipindah ke baris pengguna lain.
- Kolom `key_version` disiapkan untuk rotasi kunci induk.

### 4.3 Skema baru

```
users(user_id PK, name UNIQUE, created_at)

apify_credentials(
  credential_id PK, user_id FK,
  apify_user_id, apify_username,          -- identitas akun dari Apify
  token_nonce, token_ciphertext,          -- NULL setelah dicabut (penghapusan)
  token_last4, key_version,
  status ('active' | 'revoked'),
  created_at, validated_at, revoked_at)
  -- paling banyak satu kredensial 'active' per pengguna

apify_executions(                          -- satu baris per eksekusi Actor
  execution_id PK, user_id, credential_id, run_id,
  actor, apify_run_id, status, items, usage_usd, error_kind,
  started_at, finished_at)

scrape_runs: tambah kolom user_id, credential_id
```

Migrasi data lama: semua baris `scrape_runs` yang ada diberikan ke pengguna `default`.

### 4.4 Layanan kredensial (`spi/credentials.py`)

- `connect(user_id, token)`: validasi ke Apify (`GET /users/me`), simpan identitas akun, enkripsi, cabut kredensial aktif sebelumnya (ganti token), simpan yang baru.
- `disconnect(user_id)`: tandai dicabut dan hapus ciphertext.
- `status(user_id)`: tampilan publik tanpa token (hanya empat karakter terakhir).
- `resolve_for_job(user_id)`: mendekripsi **hanya** kredensial aktif milik `user_id`. Tidak ada parameter atau jalur untuk mengambil kredensial pengguna lain.
- `usage(user_id)`: pemakaian bulanan akun Apify milik pengguna itu.

Kompatibilitas (aturan 7): bila pengguna `default` belum menghubungkan token dan `APIFY_TOKEN` ada di `.env`, token itu dipakai untuk pengguna `default` saja, dengan peringatan agar dipindahkan lewat `spi apify connect --from-env`. `APIFY_TOKENS` dan perpindahan token dihapus.

### 4.5 Eksekusi job

- `run_scrape(..., user_id)`: job dicatat dengan `user_id` dan `credential_id` pemiliknya. Token didekripsi sekali di awal job dan diteruskan ke adapter Apify milik job itu saja.
- Klien Apify memakai **satu** token. Alurnya: mulai run (`POST /acts/{actor}/runs`), pantau status (`GET /actor-runs/{id}`), ambil dataset (`GET /datasets/{id}/items`), semuanya dengan token yang sama.
- Setiap eksekusi dicatat ke `apify_executions` beserta status, jumlah hasil, biaya (bila dilaporkan Apify), dan jenis error.
- Bila kuota pemilik habis, job gagal dengan jenis error `insufficient_credits`. Tidak ada pencarian token lain.
- `--resume` hanya boleh oleh pemilik run.

### 4.6 Klasifikasi error

| Jenis | Pemicu |
|---|---|
| `rate_limit` | HTTP 429 |
| `insufficient_credits` | HTTP 402, atau 403 dengan pesan batas pemakaian |
| `invalid_token` | HTTP 401 |
| `permission` | HTTP 403 lainnya |
| `timeout` | Run melewati batas waktu (run dihentikan) |
| `run_failed` | Status run FAILED / ABORTED / TIMED-OUT |
| `connection` | Koneksi ke Apify gagal setelah dicoba ulang |
| `no_credential` | Pemilik job belum menghubungkan token |

### 4.7 Perintah CLI

```
spi key init                      buat kunci induk enkripsi
spi user add NAMA | spi user list
spi --user NAMA apify connect     hubungkan atau ganti token (input tersembunyi; --from-env untuk impor)
spi --user NAMA apify disconnect  cabut dan hapus token
spi --user NAMA apify status      identitas akun Apify dan pemakaian
spi --user NAMA jobs              riwayat job dan eksekusi Actor
spi --user NAMA scrape ...        job memakai token pengguna itu
```

### 4.8 Pengujian

- Enkripsi: bolak-balik, nonce unik, gagal bila kunci salah, gagal bila AAD (pengguna) berbeda, token tidak tersimpan sebagai teks biasa.
- Kepemilikan: pengguna A tidak bisa melihat, memakai, mencabut, atau melanjutkan milik pengguna B.
- Eksekusi: job A memakai token A, job B memakai token B; kuota A habis sementara B masih punya kredit -> job A gagal dan token B tidak pernah dipakai.
- Klasifikasi error dan pencatatan eksekusi.
- Token tidak muncul di log maupun keluaran CLI.

## 5. Yang sengaja tidak dikerjakan di fase 1

- **Autentikasi sungguhan, HTTPS, dan frontend.** Butuh lapisan web/API (misalnya FastAPI + login + PostgreSQL/Supabase). Ini keputusan cakupan tersendiri (fase 2).
- **Isolasi data hasil scraping.** `posts` dan `comments` berisi data publik dan masih dipakai bersama oleh semua pengguna lokal. Riwayat job dan eksekusi sudah terpisah per pengguna. Pemisahan data per pengguna layak dikerjakan bersama fase 2.
- **Layanan enkripsi terkelola (KMS).** Fase 1 memakai kunci induk di `.env`.

## 6. Hasil implementasi fase 1

| Bagian | File |
|---|---|
| Enkripsi AES-256-GCM | `src/spi/crypto.py` |
| Skema `users`, `apify_credentials`, `apify_executions`, pemilik run, migrasi | `src/spi/storage.py` |
| Klien Apify satu token, klasifikasi error, pencatatan eksekusi | `src/spi/scrapers/apify_base.py` |
| Layanan kredensial (hubungkan, ganti, cabut, pilih untuk job, pemakaian) | `src/spi/credentials.py` |
| Job terikat pemilik dan kredensialnya | `src/spi/runner.py`, `src/spi/scrapers/registry.py` |
| Perintah `key init`, `user`, `apify`, `jobs`, opsi `--user` | `src/spi/cli.py` |

Yang dihapus: `ApifyTokenPool`, `APIFY_TOKENS`, `scrape.apify_min_remaining_usd`, perintah `spi credits`.

Test: `tests/test_crypto.py` (enkripsi), `tests/test_storage_tenancy.py` dan `tests/test_credentials.py`
(isolasi dan kepemilikan), `tests/test_apify_client.py` (alur eksekusi dan klasifikasi error),
`tests/test_byok_jobs.py` (eksekusi job per pengguna, tanpa perpindahan token), `tests/test_cli.py` (perintah).

Uji nyata ke Apify pada 2026-11-24 di database sementara: jalur lama (`APIFY_TOKEN` untuk pengguna
`default`), jalur terenkripsi (`apify connect --from-env`, `apify status`, `scrape`, `jobs`), pengguna
tanpa token (job gagal tanpa panggilan ke Apify), dan penolakan `--resume` atas run milik pengguna lain.
Migrasi diuji pada salinan database asli: jumlah baris tidak berubah dan semua run lama menjadi milik `default`.
