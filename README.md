# SPI: Scrape Platform Issues

Alat observasi isu hangat di media sosial. Mengambil postingan dan komentar publik dari akun
media besar Indonesia di **Instagram** dan **TikTok**, menyimpannya ke SQLite lokal, lalu
menyusun **laporan analisis otomatis** dalam Bahasa Indonesia dengan Claude.

Alur: isi `.env` (key LLM) → `spi key init` → `spi apify connect` → `spi scrape` → `spi report` → baca `reports/*.md`.

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
2. Salin `.env.example` → `.env`, isi `ANTHROPIC_API_KEY` (atau `LLM_API_KEY`).
3. Buat kunci enkripsi dan hubungkan token Apify Anda (lihat *Token Apify per pengguna*):

```bash
spi key init
spi apify connect
```

Semua perintah di bawah memakai `.venv\Scripts\spi` (atau aktifkan venv dulu).

## 1. Instagram lewat Apify (default)

Instagram diambil lewat Apify, sama seperti TikTok, jadi **tidak perlu akun Instagram**. Cukup
hubungkan token Apify Anda (`spi apify connect`). Actor yang dipakai: `apify/instagram-scraper` (post, $2,70 per 1.000) dan
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
3. Jalankan `spi apify connect` lalu tempel tokennya (tidak ditampilkan saat diketik).

Actor yang dipakai: `clockworks/tiktok-scraper` (video per profil, mulai ±$1,70 per 1.000 video) dan
`clockworks/tiktok-comments-scraper` (komentar, mulai ±$0,50 per 1.000 komentar; cek harga terkini
di halaman actor). Komentar untuk semua post satu akun dibatch dalam satu run supaya murah.
Perkiraan 15 akun × 7 hari × 200 komentar: sekitar $10-20 per pengambilan penuh.

Adapter cadangan tanpa biaya (`tiktok_playwright`, `tiktokapi`) tetap tersedia lewat `adapter:` di
`config.yaml`; lihat tabel adapter di bawah.

## Token Apify per pengguna (BYOK)

Setiap pengguna memakai **token Apify miliknya sendiri** (Bring Your Own Key). Akun Apify, kuota,
dan tagihan adalah milik dan tanggung jawab pengguna itu.

```bash
spi key init                        # sekali saja: buat kunci enkripsi SPI_MASTER_KEY di .env
spi user add andi                   # tambah pengguna (pengguna `default` sudah ada)
spi --user andi apify connect       # hubungkan atau ganti token; divalidasi ke Apify lalu dienkripsi
spi --user andi apify status        # akun Apify yang terhubung dan pemakaian bulan ini
spi --user andi scrape              # job ini hanya memakai token milik andi
spi --user andi jobs                # riwayat job dan eksekusi Actor milik andi
spi --user andi apify disconnect    # cabut token dan hapus dari database
```

Tanpa `--user`, perintah berjalan sebagai pengguna `default` (bisa diubah lewat variabel `SPI_USER`).

Aturan yang ditegakkan kode dan test:

- Sebuah job hanya memakai kredensial pemiliknya. Id pengguna dan id kredensial dicatat pada setiap
  run dan setiap eksekusi Actor.
- **Tidak ada perpindahan token otomatis dan tidak ada penggabungan kredit.** Bila kuota akun Apify
  pemilik job habis, job itu gagal dengan keterangan `insufficient_credits`. Token pengguna lain
  tidak pernah dicoba.
- Token divalidasi ke Apify saat dihubungkan, identitas akunnya disimpan, lalu token dienkripsi
  dengan AES-256-GCM. Kunci induknya (`SPI_MASTER_KEY`) berada di `.env`, di luar database.
  Ciphertext terikat ke id pemiliknya, jadi tidak bisa dibuka atas nama pengguna lain.
- Token tidak pernah ditampilkan, tidak ditulis ke log, dan dikirim ke Apify lewat HTTPS di header.
- `--resume` dan `jobs --run` hanya berlaku untuk run milik pengguna itu sendiri.

Jenis kegagalan Apify yang dibedakan (kolom Error di `spi jobs`):

| Jenis | Arti | Tindakan |
|---|---|---|
| `rate_limit` | Apify membatasi laju permintaan | Ditunggu lalu dicoba sekali lagi; lanjutkan dengan `--resume` |
| `insufficient_credits` | Kredit atau kuota akun pengguna itu habis | Tunggu siklus berikutnya atau naikkan paket akun itu |
| `invalid_token` | Token salah atau sudah dicabut | `spi apify connect` dengan token baru |
| `permission` | Token tidak punya izin (scope) | Buat token dengan izin yang cukup |
| `run_failed`, `timeout` | Run Actor gagal atau terlalu lama | Kecilkan `--max-posts` / `--max-comments` |
| `no_credential` | Pengguna belum menghubungkan token | `spi apify connect` |

Pindah dari cara lama: bila `APIFY_TOKEN` masih ada di `.env`, pengguna `default` tetap bisa scraping
seperti biasa (dengan peringatan, karena token itu tidak terenkripsi). Pindahkan dengan
`spi key init`, lalu `spi apify connect --from-env`, lalu kosongkan `APIFY_TOKEN` di `.env`.
Variabel `APIFY_TOKENS` dan perintah `spi credits` sudah dihapus.

Batasan yang perlu diketahui: `spi` adalah aplikasi CLI lokal tanpa login. `--user` **memilih**
identitas, tidak membuktikannya, jadi siapa pun yang memegang komputer ini dan file `.env` bisa
bertindak sebagai pengguna mana pun. Isolasi ditegakkan di lapisan layanan dan database supaya siap
dipakai bila nanti ditambah lapisan web dengan login. Data post dan komentar (data publik) masih
dipakai bersama oleh semua pengguna; yang terpisah adalah kredensial, job, dan riwayat eksekusi.
Jangan pernah membagikan `.env`: kunci induk di dalamnya membuka semua token tersimpan.

## 3. Scraping

```bash
spi scrape                                  # semua akun, rentang dari config (default 7 hari)
spi scrape --only instagram:kompascom --days 2 --max-posts 5 --max-comments 20   # uji kecil dulu
spi scrape --resume 20261014-120000-ab12cd  # lanjutkan akun yang rate_limited/pending (hanya run milik sendiri)
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
| `instagram_apify` | Instagram | aktif (default) | Layanan Apify berbayar, memakai token milik pengguna (`spi apify connect`). Teruji nyata. |
| `instaloader` | Instagram | cadangan, gratis | Perlu sesi login `spi login`. Saat diuji, login dari script ditolak Instagram. |
| `instagram_playwright` | Instagram | kerangka | Rencana di docstring modul |
| `tiktok_apify` | TikTok | aktif (default) | Layanan Apify berbayar, memakai token milik pengguna (`spi apify connect`). Paling stabil. |
| `tiktok_playwright` | TikTok | cadangan, gratis | Browser Edge/Chrome sungguhan, profil persisten di `data/browser_profile/tiktok`, membaca JSON internal TikTok saat scroll. Saat diuji: daftar post berhasil (48 post), halaman video untuk komentar diblokir TikTok. |
| `tiktokapi` | TikTok | cadangan, gratis | Library TikTokApi, butuh `TIKTOK_MS_TOKEN`. Saat diuji TikTok membalas data user kosong. |

## Pengembangan

```bash
.venv\Scripts\python -m pytest
```

Struktur: `src/spi/` (config, models, storage, crypto, credentials, runner, scrapers/, analysis/, export, cli),
`tests/`, spesifikasi di `docs/superpowers/specs/`.
