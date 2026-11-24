# SPI: Scrape Platform Issues

Alat observasi isu hangat di media sosial. Mengambil postingan dan komentar publik dari akun
media besar Indonesia di **Instagram** dan **TikTok**, menyimpannya ke SQLite lokal, lalu
menyusun **laporan analisis otomatis** dalam Bahasa Indonesia dengan Claude.

Alur: isi `.env` (token Apify + key LLM) → `spi scrape` → `spi report` → baca `reports/*.md`.

## Peringatan

- Scraping otomatis **melanggar Terms of Service** Instagram dan TikTok. Data yang diambil hanya
  data publik. Bila memakai adapter login (`instaloader`), **gunakan akun Instagram sekunder**,
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

## 1. Instagram lewat Apify (default)

Instagram diambil lewat Apify, sama seperti TikTok, jadi **tidak perlu akun Instagram**. Cukup isi
`APIFY_TOKEN` di `.env`. Actor yang dipakai: `apify/instagram-scraper` (post, $2,70 per 1.000) dan
`apify/instagram-comment-scraper` (komentar, $2,60 per 1.000).

Kenapa tidak gratis: saat diuji (November 2026) Instagram menolak akses tanpa login, dan menolak
login dari script Instaloader (`Login error: "fail" status`) meskipun akunnya bisa login normal di
browser. Adapter `instaloader` dan perintah `spi login` tetap ada kalau suatu saat ingin dicoba lagi
(isi `IG_USERNAME`, set `adapter: instaloader` pada akun di `config.yaml`).

Keterbatasan Instagram via Apify:

- Komentar yang dikembalikan adalah yang **terbaru**, bukan yang paling populer. Dengan batas kecil
  (misalnya 5), komentar ter-like bisa terlewat; pakai batas 30-50 agar reaksi publik terwakili.
- **Jumlah balasan per komentar kosong** di paket gratis Apify (fitur balasan khusus paket berbayar),
  sehingga bagian "komentar paling memicu diskusi" hanya terisi untuk TikTok.
- Jumlah share tidak tersedia; jumlah like bisa kosong bila akun menyembunyikannya.

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

### Beberapa token Apify dan sisa kredit

`.env` menerima lebih dari satu token:

```
APIFY_TOKEN=token_utama
APIFY_TOKENS=token_kedua,token_ketiga
```

Urutan pemakaian: `APIFY_TOKEN` dulu, lalu isi `APIFY_TOKENS` dari kiri ke kanan. Cara kerjanya:

- Sebelum tiap run actor, sisa kredit token aktif dicek. Bila di bawah
  `scrape.apify_min_remaining_usd` (default $0,25), token itu dilewati dan token berikutnya dipakai.
- Bila Apify menolak karena kredit habis (HTTP 402/403) atau token tidak sah (401), token itu dilewati
  untuk sisa proses dan run diulang dengan token berikutnya.
- Bila semua token habis, akun yang sedang diproses ditandai gagal dengan pesan yang jelas dan bisa
  dilanjutkan nanti dengan `spi scrape --resume`.
- Token tidak pernah ditulis ke log; yang tampil hanya nomor urut dan empat karakter terakhirnya.

Lihat sisa kredit semua token:

```bash
spi credits
```

Catatan: ketentuan umum Apify membatasi satu akun pribadi per pengguna kecuali diizinkan Apify.
Pemakaian token dari beberapa akun adalah tanggung jawab pemilik akun.

## 3. Scraping

```bash
spi scrape                                  # semua akun, rentang dari config (default 7 hari)
spi scrape --only instagram:kompascom --days 2 --max-posts 5 --max-comments 20   # uji kecil dulu
spi scrape --resume 20261014-120000-ab12cd  # lanjutkan akun yang rate_limited/pending
spi status                                  # ringkasan isi database
```

Perilaku:

- **Batas biaya**: `max_posts_per_account` (post per akun) dan `max_comments_per_post` (komentar per
  post) di `config.yaml`, atau per run lewat `--max-posts`, `--max-comments`, `--days`.
- **Komentar yang dirangkum**: dari komentar yang diambil, hanya `max_comments_to_llm` (default 15)
  dengan **balasan terbanyak** yang dikirim ke LLM (`comment_selection: top_replied`). LLM lalu
  menyaring relevansi: komentar spam, promosi, atau di luar isu postingan diabaikan, dan jumlah
  komentar yang dipakai dicatat di `relevant_comment_count`. Catatan: Apify tidak bisa diminta
  "hanya komentar paling dibalas", jadi penyaringan terjadi setelah kandidat diambil; biaya Apify
  ditentukan `max_comments_per_post`. Untuk Instagram (jumlah balasan tidak tersedia di paket
  gratis Apify) urutannya memakai jumlah like.
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
- **Penyedia LLM bisa diganti** lewat `analysis.provider` di `config.yaml`:
  - `anthropic` (default): Claude, berbayar per token, kualitas terbaik. Butuh `ANTHROPIC_API_KEY`.
  - `openai_compatible`: endpoint berformat OpenAI, cocok untuk tier gratis **Gemini**
    (`https://generativelanguage.googleapis.com/v1beta/openai/`, key dari https://aistudio.google.com/apikey),
    **Groq** (`https://api.groq.com/openai/v1`), **OpenRouter**, atau **Ollama** lokal
    (`http://localhost:11434/v1`, tanpa key). Key diambil dari variabel `.env` yang namanya diatur
    `analysis.api_key_env` (default `LLM_API_KEY`). Atur `request_delay_seconds` (misalnya 4) agar tidak
    kena batas permintaan tier gratis. Contoh lengkap ada di `config.example.yaml`. Catatan: model gratis
    kadang menghasilkan JSON yang tidak valid; scraper mengulang sekali dengan umpan balik, dan post yang
    tetap gagal dicatat lalu dilewati.
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
| `accounts[].adapter` | Mesin scraper per akun. Default `instagram_apify` dan `tiktok_apify`; lihat tabel adapter di bawah |
| `scrape.days` | Rentang hari ke belakang |
| `scrape.max_posts_per_account` | Batas post per akun |
| `scrape.max_comments_per_post` | Batas komentar yang diambil per post |
| `analysis.comment_selection`, `analysis.max_comments_to_llm` | Cara memilih dan jumlah komentar yang dirangkum LLM |
| `scrape.older_posts_tolerance` | Jumlah post pinned (lebih tua) yang ditoleransi sebelum berhenti |
| `analysis.per_post_model`, `analysis.report_model` | Model Claude tahap 1 dan 2 |

## Adapter scraper dan kalau scraper rusak

Setiap platform punya dua adapter dengan antarmuka sama (`src/spi/scrapers/base.py`).
Mengganti mesin cukup lewat `adapter:` per akun di `config.yaml`, tanpa mengubah kode lain.

| Adapter | Platform | Status | Catatan |
|---|---|---|---|
| `instagram_apify` | Instagram | aktif (default) | Layanan Apify berbayar, butuh `APIFY_TOKEN`. Teruji nyata. |
| `instaloader` | Instagram | cadangan, gratis | Perlu sesi login `spi login`. Saat diuji, login dari script ditolak Instagram. |
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
