# SPI: Scrape Platform Issues

Alat observasi isu hangat di media sosial. Mengambil postingan dan komentar publik dari akun
media besar Indonesia di **Instagram** dan **TikTok**, menyimpannya ke SQLite lokal, lalu
menyusun **laporan analisis otomatis** dalam Bahasa Indonesia dengan Claude.

Alur: isi `.env` → `spi login` (sekali, Instagram) → `spi scrape` → `spi report` → baca `reports/*.md`.

## Peringatan

- Scraping otomatis **melanggar Terms of Service** Instagram dan TikTok. Data yang diambil hanya
  data publik, tetapi akun yang dipakai login bisa diblokir. **Gunakan akun Instagram sekunder**,
  bukan akun utama Anda.
- Library pihak ketiga (Instaloader, TikTokApi) bisa rusak sewaktu-waktu saat platform berubah.
  Lihat bagian *Kalau scraper rusak*.
- Jalankan dari jaringan rumahan. VPN dan IP datacenter diblokir TikTok dan dibatasi ketat Instagram.

## Instalasi (Windows, Python 3.11+)

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m playwright install chromium
```

Lalu:

1. Salin `config.example.yaml` → `config.yaml`, sesuaikan daftar akun.
2. Salin `.env.example` → `.env`, isi `ANTHROPIC_API_KEY`, `IG_USERNAME`, `APIFY_TOKEN`.

Semua perintah di bawah memakai `.venv\Scripts\spi` (atau aktifkan venv dulu).

## 1. Login Instagram (sekali)

```bash
spi login
```

Memakai `IG_USERNAME` dari `.env`, meminta password (dan kode 2FA bila ada), lalu menyimpan sesi
ke `data/ig_session`. Sesi dipakai ulang oleh `spi scrape`.

Kalau Instagram meminta verifikasi (*checkpoint*) dan login lewat terminal gagal:

- Buka instagram.com di browser dengan akun sekunder, selesaikan verifikasi, coba lagi; atau
- Login di Firefox, lalu impor cookie-nya dengan script resmi Instaloader
  [`615_import_firefox_session.py`](https://instaloader.github.io/troubleshooting.html#login-error)
  dan simpan hasilnya sebagai `data/ig_session`.

## 2. TikTok lewat Apify (default)

Scraping TikTok langsung dari laptop diblokir TikTok saat diuji (Oktober 2026): library TikTokApi
menerima data kosong, dan halaman video menolak dibuka untuk komentar. Karena itu TikTok memakai
layanan pihak ketiga **Apify** (berbayar, bayar per hasil):

1. Daftar di https://apify.com (paket gratis memberi kredit ±$5/bulan).
2. Console -> Settings -> Integrations -> *Personal API tokens* -> salin token.
3. Tempel ke `APIFY_TOKEN` di `.env`.

Actor yang dipakai: `clockworks/tiktok-scraper` (video per profil, mulai ±$1,70 per 1.000 video) dan
`clockworks/tiktok-comments-scraper` (komentar, mulai ±$0,50 per 1.000 komentar; cek harga terkini
di halaman actor). Komentar untuk semua post satu akun dibatch dalam satu run supaya murah.
Perkiraan 15 akun × 7 hari × 200 komentar: sekitar $10-20 per pengambilan penuh.

Adapter cadangan tanpa biaya (`tiktok_playwright`, `tiktokapi`) tetap tersedia lewat `adapter:` di
`config.yaml`; lihat tabel adapter di bawah.

## 3. Scraping

```bash
spi scrape                                  # semua akun, rentang dari config (default 7 hari)
spi scrape --only instagram:kompascom --days 1 --max-comments 20   # uji kecil dulu
spi scrape --resume 20261014-120000-ab12cd  # lanjutkan akun yang rate_limited/pending
spi status                                  # ringkasan isi database
```

Perilaku:

- Komentar dibatasi `max_comments_per_post` (default 200), diprioritaskan yang paling banyak like.
- Ada jeda acak antar request dan antar akun. Kalau kena rate limit, script menunggu
  `rate_limit_sleep_minutes` lalu mencoba sekali lagi; kalau tetap gagal, akun ditandai
  `rate_limited` dan bisa dilanjutkan dengan `--resume`.
- Satu akun gagal tidak menghentikan akun lain. Semua dicatat di `data/scrape.log`.
- Scrape ulang bersifat idempoten: data lama diperbarui, tidak duplikat.

## 4. Laporan

```bash
spi report --dry-run     # hitung token & estimasi biaya laporan akhir, tanpa memanggil model
spi report               # tahap 1 (per post, Haiku) + tahap 2 (laporan, Opus)
spi report --skip-analysis   # langsung tahap 2, pakai analisis per post yang sudah ada
```

- **Tahap 1** (`claude-haiku-5-5`): setiap post diringkas menjadi topik, ringkasan isu, proporsi
  sentimen komentar, reaksi publik, 3 kutipan komentar, dan intensitas. Disimpan di tabel
  `post_analysis`, hanya dihitung ulang kalau caption/komentar berubah.
- **Tahap 2** (`claude-opus-5-5`): laporan Markdown `reports/YYYY-MM-DD_HHMM_isu-hangat.md`
  berisi ringkasan eksekutif, 5-10 isu terhangat, perbedaan framing antar media, reaksi publik,
  isu naik/meredup, metodologi, plus lampiran tabel post teratas per akun (dibuat dari database).
- Fallback server-side Claude aktif secara default (`analysis.server_side_fallback`): bila model
  utama menolak suatu konten, API otomatis mengulang dengan model cadangan. Matikan di config bila
  tidak diinginkan.

Perkiraan biaya untuk 15 akun × 7 hari (±450 post × 200 komentar): sekitar **$1-1,5 per laporan**.

## 5. Ekspor

```bash
spi export --out data/export
```

Menghasilkan `posts.csv`, `comments.csv`, `post_analysis.csv` (UTF-8 dengan BOM, aman dibuka di Excel).

## Konfigurasi

Lihat `config.example.yaml`. Bagian penting:

| Kunci | Arti |
|---|---|
| `accounts[].adapter` | Mesin scraper per akun: `instaloader`, `tiktokapi`, atau cadangan `instagram_playwright`, `tiktok_playwright` |
| `scrape.days` | Rentang hari ke belakang |
| `scrape.max_comments_per_post` | Batas komentar per post |
| `scrape.older_posts_tolerance` | Jumlah post pinned (lebih tua) yang ditoleransi sebelum berhenti |
| `analysis.per_post_model`, `analysis.report_model` | Model Claude tahap 1 dan 2 |

## Adapter scraper dan kalau scraper rusak

Setiap platform punya dua adapter dengan antarmuka sama (`src/spi/scrapers/base.py`).
Mengganti mesin cukup lewat `adapter:` per akun di `config.yaml`, tanpa mengubah kode lain.

| Adapter | Platform | Status | Catatan |
|---|---|---|---|
| `instaloader` | Instagram | aktif (default) | Perlu sesi login `spi login` |
| `instagram_playwright` | Instagram | kerangka | Rencana di docstring modul |
| `tiktok_apify` | TikTok | aktif (default) | Layanan Apify berbayar, butuh `APIFY_TOKEN`. Paling stabil. |
| `tiktok_playwright` | TikTok | cadangan, gratis | Browser Edge/Chrome sungguhan, profil persisten di `data/browser_profile/tiktok`, membaca JSON internal TikTok saat scroll. Saat diuji: daftar post berhasil (48 post), halaman video untuk komentar diblokir TikTok. |
| `tiktokapi` | TikTok | cadangan, gratis | Library TikTokApi, butuh `TIKTOK_MS_TOKEN`. Saat diuji TikTok membalas data user kosong. |

## Pengembangan

```bash
.venv\Scripts\python -m pytest
```

Struktur: `src/spi/` (config, models, storage, runner, scrapers/, analysis/, export, cli),
`tests/`, spesifikasi di `docs/superpowers/specs/`.
