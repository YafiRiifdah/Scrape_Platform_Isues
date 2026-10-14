# Rencana: Scraper Instagram & TikTok untuk Observasi Isu Hangat Media Besar

## Konteks

Pengguna ingin mengobservasi dan menginvestigasi isu yang sedang hangat di media sosial dengan cara mengambil postingan dan komentar publik dari 5-15 akun media besar Indonesia (contoh: kompascom, detikcom, narasinewsroom) di Instagram dan TikTok, lalu menghasilkan laporan analisis otomatis dalam Bahasa Indonesia.

Keputusan yang sudah disepakati saat brainstorming:

| Aspek | Keputusan |
|---|---|
| Data | Postingan + komentar publik (tanpa profil komentator) |
| Skala | 5-15 akun, 7 hari terakhir, dijalankan sekali/beberapa kali |
| Pendekatan scraping | **A dulu**: library open-source (Instaloader, TikTokApi) di balik antarmuka adapter; **B (Playwright + login manual)** disiapkan sebagai kerangka adapter dengan antarmuka sama, diisi hanya jika A gagal |
| Login | Akun Instagram sekunder (bukan akun utama); TikTok tanpa login, hanya cookie `ms_token` |
| Analisis | Dua tahap LLM: Claude Haiku 5.5 per post (bulk, murah) lalu Claude Opus 5.5 untuk laporan akhir |
| Output | Laporan Markdown bertanggal di `reports/`, SQLite lokal, ekspor CSV |
| Anggaran | Gratis untuk scraping; biaya API Claude di bawah $1 per laporan |

Folder kerja `F:\work\Scrape_Platform_Isues` kosong, bukan repo git. Tersedia Python 3.13.0 dan Node 20.

**Catatan risiko yang sudah disampaikan ke pengguna**: scraping otomatis melanggar ToS Instagram/TikTok; data publik relatif aman secara hukum (hiQ v. LinkedIn, Meta v. Bright Data), tapi akun sekunder bisa diblokir. Library pihak ketiga sering rusak saat platform berubah, karena itu ada lapisan adapter.

## Dependensi (versi PyPI terkini, diverifikasi 2026-10-14)

| Paket | Versi | Fungsi |
|---|---|---|
| `instaloader` | 4.15.4 | Instagram posts + comments (perlu sesi login) |
| `TikTokApi` | 7.3.3 | TikTok user videos + comments (Playwright di dalamnya, tanpa login) |
| `playwright` | 1.64.0 | Dipakai TikTokApi; juga untuk adapter cadangan B |
| `anthropic` | 1.13.0 | Claude API (berbasis `httpx2`, bukan `httpx`) |
| `pydantic` | 2.14.1 | Skema config dan structured outputs |
| `typer` | 0.27.3 | CLI |
| `pyyaml` | 6.0.3 | File konfigurasi akun |
| `python-dotenv` | terbaru | Baca `.env` |
| `pytest` | 9.1.1 | Unit test |

Setelah install: `python -m playwright install chromium`.

## Struktur Proyek

```
F:\work\Scrape_Platform_Isues\
  pyproject.toml              # paket "spi", entry point CLI `spi`
  README.md                   # cara setup, login, ambil ms_token, jalankan
  config.example.yaml         # contoh daftar akun + parameter
  .env.example                # ANTHROPIC_API_KEY, TIKTOK_MS_TOKEN, IG_USERNAME
  .gitignore                  # data/, .env, reports/*.md (opsional)
  docs/superpowers/specs/2026-10-14-scrape-platform-issues-design.md  # salinan ringkas rancangan ini
  src/spi/
    __init__.py
    cli.py                    # typer: login, scrape, report, export
    config.py                 # pydantic: AccountConfig, ScrapeConfig, AnalysisConfig; loader YAML
    models.py                 # Post, Comment (pydantic) ternormalisasi lintas platform
    storage.py                # SQLite via sqlite3 stdlib: schema, upsert, query, checkpoint
    runner.py                 # orkestrasi per akun: jeda acak, retry, checkpoint, log
    export.py                 # CSV posts/comments
    scrapers/
      __init__.py
      base.py                 # Protocol BaseScraper + exceptions bersama
      registry.py             # pilih adapter dari (platform, adapter_name)
      instagram_instaloader.py
      tiktok_tiktokapi.py
      instagram_playwright.py # kerangka B: NotImplementedError + docstring rencana
      tiktok_playwright.py    # kerangka B: NotImplementedError + docstring rencana
    analysis/
      __init__.py
      schemas.py              # pydantic PostAnalysis (output Haiku)
      prompts.py              # system prompt per-post & laporan (Bahasa Indonesia)
      per_post.py             # Haiku 5.5, messages.parse, simpan ke post_analysis
      report.py               # Opus 5.5 streaming, tulis reports/YYYY-MM-DD_isu-hangat.md
  tests/
    conftest.py               # fixture DB in-memory, fake scraper, fake anthropic client
    test_models.py
    test_storage.py
    test_runner.py
    test_prompts.py
    test_per_post.py
    test_report.py
  data/                       # spi.sqlite, sesi Instaloader (gitignored)
  reports/
```

## Komponen

### 1. `config.py`
- `AccountConfig`: `platform: Literal["instagram","tiktok"]`, `username: str`, `label: str` (nama media), `adapter: str | None` (default per platform: `instaloader` / `tiktokapi`).
- `ScrapeConfig`: `days: int = 7`, `max_comments_per_post: int = 200`, `max_posts_per_account: int = 100`, `delay_between_requests: tuple[float,float] = (2, 6)`, `delay_between_accounts: tuple[float,float] = (30, 90)`, `older_posts_tolerance: int = 3` (toleransi post pinned yang lebih tua sebelum berhenti).
- `AnalysisConfig`: `per_post_model = "claude-haiku-5-5"`, `report_model = "claude-opus-5-5"`, `max_comments_to_llm: int = 200`, `language = "id"`.
- `load_config(path) -> AppConfig`.

### 2. `models.py` (ternormalisasi)
- `Post`: `platform`, `account`, `post_id`, `url`, `created_at` (UTC), `caption`, `hashtags: list[str]`, `media_type` (image/video/carousel), `likes`, `comments_count`, `shares`, `views`, `raw: dict | None`.
- `Comment`: `platform`, `post_id`, `comment_id`, `text`, `created_at` (boleh None untuk TikTok jika tidak tersedia), `likes`, `is_reply: bool`, `parent_comment_id: str | None`.

### 3. `storage.py` (sqlite3 stdlib)
Tabel:
- `posts` PK `(platform, post_id)`; semua kolom `Post`; `scraped_at`.
- `comments` PK `(platform, comment_id)`; FK ke posts.
- `post_analysis` PK `(platform, post_id)`; `model`, `analysis_json`, `analyzed_at`, `input_hash` (hash caption+komentar agar re-analisis hanya kalau data berubah).
- `scrape_runs`: `run_id`, `platform`, `account`, `status` (pending/ok/failed/rate_limited), `posts_found`, `comments_found`, `error`, `started_at`, `finished_at`.
- `reports`: `report_id`, `path`, `created_at`, `model`, `posts_included`, `tokens_in`, `tokens_out`.

Fungsi: `init_db`, `upsert_post`, `upsert_comments`, `get_posts_since`, `get_comments_for_post(limit, order by likes desc)`, `get_unanalyzed_posts`, `save_analysis`, `start_run/finish_run`, `pending_accounts(run_id)`.

### 4. `scrapers/base.py`
```python
class BaseScraper(Protocol):
    platform: str
    def fetch_posts(self, username: str, since: datetime, max_posts: int) -> Iterator[Post]: ...
    def fetch_comments(self, post: Post, limit: int) -> Iterator[Comment]: ...
    def close(self) -> None: ...
class RateLimited(Exception)   # tunggu lama lalu lanjut
class ScrapeBlocked(Exception) # login wall / challenge / empty response; akun ditandai failed
class AccountNotFound(Exception)
```

### 5. `scrapers/instagram_instaloader.py`
- Konstruktor: `Instaloader(sleep=True, quiet=True, request_timeout=60, download_pictures=False, download_videos=False, ...)`, lalu `load_session_from_file(IG_USERNAME, data/ig_session)`. Jika file tidak ada: raise error yang menyuruh jalankan `spi login`.
- `fetch_posts`: `Profile.from_username(L.context, username)` → iterasi `profile.get_posts()`; map `Post` dari `shortcode`, `date_utc`, `caption`, `caption_hashtags`, `likes`, `comments`, `video_view_count`, `typename` (GraphImage/GraphVideo/GraphSidecar), URL `https://www.instagram.com/p/{shortcode}/`. Berhenti setelah `older_posts_tolerance` post berturut-turut lebih tua dari `since` (menangani pinned post).
- `fetch_comments`: `post.get_comments()` → `PostComment(id, created_at_utc, text, likes_count, answers)`; jawaban (`PostCommentAnswer`) dimasukkan sebagai `is_reply=True`. Berhenti di `limit`.
- Mapping exception: `TooManyRequestsException`/`ConnectionException` (429) → `RateLimited`; `LoginRequiredException`/`QueryReturnedBadRequestException` (checkpoint) → `ScrapeBlocked`; `ProfileNotExistsException` → `AccountNotFound`.
- Login (`spi login`): `L.interactive_login(IG_USERNAME)`; tangani `TwoFactorAuthRequiredException` dengan prompt kode → `two_factor_login`; `save_session_to_file(data/ig_session)`. Alternatif yang didokumentasikan di README: `instaloader --login USER` lalu salin file sesi, atau impor dari Firefox dengan script resmi Instaloader `615_import_firefox_session.py` jika login script kena challenge.

### 6. `scrapers/tiktok_tiktokapi.py`
- TikTokApi bersifat async; adapter membungkusnya menjadi sinkron dengan `asyncio.run` per akun (loop event tersendiri), supaya `runner` tetap sederhana dan sama untuk kedua platform.
- Sesi: `async with TikTokApi() as api: await api.create_sessions(ms_tokens=[TIKTOK_MS_TOKEN], num_sessions=1, sleep_after=3, browser="chromium", headless=False)`. **Verifikasi saat implementasi**: parameter `headless` di `create_sessions` v7.3.3 (ada di kode sumber, tidak di docs). Non-headless lebih sulit terdeteksi bot.
- `fetch_posts`: `api.user(username=...).videos(count=max_posts)` → `Video.id`, `Video.create_time`, `Video.stats` (`diggCount`, `commentCount`, `shareCount`, `playCount`; sumber `statsV2` lalu `stats`), `as_dict["desc"]` sebagai caption, hashtags dari `as_dict["challenges"][*]["title"]`, URL `https://www.tiktok.com/@{username}/video/{id}`. Aturan berhenti sama (toleransi pinned).
- `fetch_comments`: `video.comments(count=limit)` → `Comment.id`, `.text`, `.likes_count`; `created_at` dari `as_dict.get("create_time")` (epoch) bila ada. Perhatikan: library selalu minta 20 per halaman dan bisa melebihi `count`; adapter memotong di `limit`.
- Exception: `EmptyResponseException`/`InvalidResponseException` → retry 3x dengan backoff 10/30/60 detik, lalu `ScrapeBlocked`. Pesan error menjelaskan cara memperbarui `ms_token` (buka tiktok.com di browser, DevTools → Application → Cookies → `msToken`).

### 7. `scrapers/*_playwright.py` (kerangka B)
Kelas dengan antarmuka `BaseScraper`, semua metode `raise NotImplementedError`. Docstring memuat rencana ringkas: `launch_persistent_context` ke `data/browser_profile_{platform}`, login manual sekali, buka halaman profil, scroll, tangkap respons JSON via `page.on("response")` untuk endpoint `graphql/query` (Instagram) dan `/api/post/item_list/` + `/api/comment/list/` (TikTok). `registry.py` sudah mengenali nama adapter `instagram_playwright`/`tiktok_playwright` agar penggantian cukup lewat `adapter:` di YAML.

### 8. `runner.py`
- `run_scrape(config, db, only: list[str] | None, resume_run_id: str | None)`.
- Untuk setiap akun pending: buat scraper lewat registry, `fetch_posts` → upsert, lalu untuk tiap post `fetch_comments` → upsert, jeda acak antar post dan antar akun (`random.uniform`).
- `RateLimited` → tidur 15 menit (konfigurasi), coba ulang akun 1x, kalau gagal lagi tandai `rate_limited` dan lanjut akun berikut.
- `ScrapeBlocked`/`AccountNotFound`/exception lain → catat `failed` + pesan, lanjut akun berikut. Tidak pernah menghentikan seluruh run karena satu akun.
- Logging ke konsol dan `data/scrape.log` (modul `logging`).
- Ringkasan akhir: tabel akun → status, jumlah post, jumlah komentar.

### 9. `analysis/schemas.py`
```python
class RepresentativeComment(BaseModel): text: str; stance: Literal["pro","kontra","netral"]
class PostAnalysis(BaseModel):
    topics: list[str]                 # 1-3 topik, frasa pendek Bahasa Indonesia
    issue_summary: str                # 1-2 kalimat isi postingan
    sentiment: dict[str, float]       # {"pro":..,"kontra":..,"netral":..} jumlah 1.0
    public_reaction_summary: str      # 2-3 kalimat reaksi netizen
    representative_comments: list[RepresentativeComment]  # maks 3
    intensity: Literal["rendah","sedang","tinggi"]        # seberapa panas perdebatan
```

### 10. `analysis/per_post.py`
- `client = anthropic.Anthropic()` (API key dari env).
- Untuk tiap post belum teranalisis (atau `input_hash` berubah): susun user message berisi metadata post, caption, dan hingga `max_comments_to_llm` komentar terurut like tertinggi (teks dipotong 300 karakter per komentar).
- `client.messages.parse(model="claude-haiku-5-5", max_tokens=2048, output_config={"effort": "low"}, system=[{..., "cache_control": {"type":"ephemeral"}}], messages=[...], output_format=PostAnalysis)`. Cek `response.stop_reason == "refusal"` → simpan status gagal, lanjut.
- Error chain: `RateLimitError` (tunggu `retry-after`) → `APIStatusError` 5xx (retry) → 4xx lain (catat, lanjut) → `APIConnectionError`.
- Simpan `analysis_json`, usage tokens. Progress bar sederhana di konsol.

### 11. `analysis/report.py`
- Input: semua `PostAnalysis` + metrik engagement dalam rentang tanggal, dikelompokkan per akun, diringkas menjadi JSON kompak (sekitar 50-150K token untuk 15 akun × 7 hari).
- `client.beta.messages.stream(model="claude-opus-5-5", max_tokens=32000, output_config={"effort": "high"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default", system=[...cache_control...], messages=[...])` → `get_final_message()`. Fallback server-side diaktifkan sesuai panduan skill claude-api (beri tahu pengguna; mudah dimatikan di config).
- System prompt (Bahasa Indonesia) meminta struktur: Ringkasan eksekutif; 5-10 isu terhangat lintas media (dengan skor engagement gabungan dan akun pendorong); Perbedaan framing antar media per isu; Reaksi publik dan sentimen dominan; Isu yang sedang naik vs. meredup; Metodologi dan keterbatasan (jumlah post/komentar, akun gagal).
- Tulis `reports/YYYY-MM-DD_HHMM_isu-hangat.md`; sertakan lampiran tabel post teratas per akun yang dibuat dari SQL, bukan dari LLM. Catat ke tabel `reports`.
- `--dry-run`: hitung token dengan `client.messages.count_tokens` dan tampilkan estimasi biaya tanpa memanggil model.

### 12. `cli.py` (typer)
| Perintah | Fungsi |
|---|---|
| `spi login` | Login Instagram interaktif, simpan sesi |
| `spi scrape [--config config.yaml] [--only instagram:kompascom ...] [--days 7] [--max-comments 200] [--resume RUN_ID]` | Jalankan scraping |
| `spi report [--days 7] [--dry-run] [--skip-analysis]` | Analisis per post (yang belum) lalu laporan akhir |
| `spi export [--out data/export/]` | CSV `posts.csv`, `comments.csv`, `post_analysis.csv` |
| `spi status` | Ringkasan DB: post/komentar per akun, akun gagal di run terakhir |

### 13. `export.py`
`csv` stdlib, UTF-8 dengan BOM agar Excel membaca Bahasa Indonesia dengan benar.

## Urutan Implementasi (TDD per langkah)

1. Scaffold: `pyproject.toml`, struktur folder, `.gitignore`, `git init`, salinan spec ke `docs/superpowers/specs/`.
2. `models.py` + `config.py` + test.
3. `storage.py` + test (DB in-memory, upsert idempoten, query komentar terurut like).
4. `scrapers/base.py`, `registry.py`, kerangka Playwright + test registry.
5. `runner.py` + test dengan `FakeScraper` (sukses, RateLimited lalu sukses, ScrapeBlocked, resume).
6. `instagram_instaloader.py` (test unit untuk fungsi mapping dengan objek tiruan; uji integrasi manual).
7. `tiktok_tiktokapi.py` (sama).
8. `analysis/schemas.py`, `prompts.py`, `per_post.py` + test dengan client tiruan (`parsed_output`, refusal, RateLimitError).
9. `analysis/report.py` + test pembentukan input dan penulisan file dengan client tiruan.
10. `export.py`, `cli.py`, `README.md`, `config.example.yaml`, `.env.example`.
11. Uji integrasi nyata (lihat Verifikasi), perbaiki mapping field sesuai data sungguhan (`as_dict` TikTok sering berubah).

## Verifikasi

1. **Unit test**: `pytest -q` hijau, tanpa jaringan.
2. **Login Instagram**: `spi login` dengan akun sekunder; `data/ig_session` terbentuk; `Instaloader.test_login()` mengembalikan username.
3. **Smoke scrape kecil**: `spi scrape --only instagram:kompascom --days 1 --max-comments 20`, lalu `spi scrape --only tiktok:kompascom --days 1 --max-comments 20`. Cek `spi status` menunjukkan post dan komentar > 0, dan jalankan ulang perintah yang sama → jumlah baris tidak bertambah (idempoten).
4. **Scrape penuh**: 5-15 akun, 7 hari; amati log untuk rate limit; pastikan akun gagal tidak menghentikan run; `--resume` melanjutkan akun pending saja.
5. **Analisis**: `spi report --dry-run` menampilkan jumlah post, estimasi token, dan biaya. `spi report` menghasilkan file di `reports/`; cek `post_analysis` terisi, jalankan ulang `spi report --skip-analysis` tidak memanggil Haiku lagi.
6. **Kualitas laporan**: baca manual; isu yang disebut harus bisa ditelusuri ke post di `posts.csv`.
7. **Ekspor**: `spi export` menghasilkan CSV yang terbuka rapi di Excel.

## Estimasi Biaya API per Laporan (15 akun × 7 hari ≈ 450 post × 200 komentar)

| Tahap | Model | Token masuk | Token keluar | Biaya |
|---|---|---|---|---|
| Per post | Haiku 5.5 | ≈ 2,5 juta | ≈ 0,3 juta | ≈ $0,40 |
| Laporan | Opus 5.5 | ≈ 150 ribu | ≈ 15 ribu | ≈ $0,90 |
| **Total** | | | | **≈ $1,30** |

## Risiko dan Mitigasi

- **Instaloader/TikTokApi rusak**: adapter B siap diisi; `adapter:` di YAML memindahkan satu platform tanpa mengubah kode lain.
- **Akun IG sekunder kena challenge**: README menyediakan jalur impor sesi dari Firefox.
- **TikTok `EmptyResponseException`**: jalankan non-headless dari IP rumahan; perbarui `ms_token`; jika tetap gagal, adapter B.
- **Struktur `as_dict` TikTok berubah**: semua akses field lewat satu fungsi mapping dengan `.get()` dan default, raw JSON disimpan di kolom `raw` untuk pemetaan ulang tanpa scrape ulang.
- **Komentar sangat banyak**: dibatasi `max_comments_per_post`, diprioritaskan like tertinggi.
