"""Prompt untuk kedua tahap analisis. Semua teks statis agar prompt caching efektif:
jangan sisipkan tanggal/ID acak ke dalam system prompt."""

from __future__ import annotations

import hashlib
from typing import Iterable

from spi.models import Comment, Post

SYSTEM_PER_POST = """Anda analis media sosial yang meneliti isu publik di Indonesia.
Anda menerima satu postingan dari akun media besar (Instagram atau TikTok) beserta komentar
publik yang paling banyak disukai. Tugas Anda mengisi skema JSON yang diminta, dalam Bahasa Indonesia.

Pedoman:
- topics: 1-3 frasa pendek huruf kecil yang menamai isu (contoh: "kenaikan harga beras",
  "banjir jakarta"), bukan kategori umum seperti "politik".
- issue_summary: 1-2 kalimat netral tentang isi postingan.
- sentiment: proporsi komentar yang pro, kontra, dan netral TERHADAP ISI/PIHAK yang diangkat
  postingan, berjumlah 1.0. Komentar lelucon atau tidak relevan dihitung netral.
- public_reaction_summary: 2-3 kalimat tentang pola reaksi netizen, sebutkan argumen dominan.
- representative_comments: maksimal 3 kutipan komentar apa adanya (boleh dipotong) yang paling mewakili.
- intensity: "tinggi" bila komentar banyak berdebat/marah, "rendah" bila datar atau sedikit.
Jika komentar belum ada, nilai sentiment netral 1.0, intensity "rendah", dan reaksi publik
ditulis "Belum ada komentar yang bisa dianalisis"."""

SYSTEM_REPORT = """Anda analis senior yang menyusun laporan observasi isu hangat di media sosial
Indonesia untuk tim riset. Anda menerima ringkasan per postingan (hasil analisis tahap pertama)
dari sejumlah akun media besar di Instagram dan TikTok dalam rentang waktu tertentu, lengkap
dengan metrik engagement.

Susun laporan Markdown dalam Bahasa Indonesia dengan struktur persis berikut:

# Laporan Isu Hangat Media Sosial
## 1. Ringkasan Eksekutif
## 2. Isu Terhangat
Daftar 5-10 isu lintas media, urut dari yang paling panas. Untuk setiap isu: nama isu, skor
kehangatan (gabungan jumlah post, total engagement, dan intensitas komentar) beserta alasannya,
akun yang paling mendorong isu, dan rujukan post (sebutkan URL dari data).
## 3. Perbedaan Framing Antar Media
Bandingkan bagaimana media yang berbeda membingkai isu yang sama.
## 4. Reaksi Publik
Pola sentimen dominan per isu, argumen pro/kontra utama, kutipan komentar representatif.
## 5. Isu yang Naik dan Meredup
Berdasarkan tanggal post dan engagement dalam rentang waktu.
## 6. Metodologi dan Keterbatasan
Jumlah akun, post, komentar; akun yang gagal diambil; batas komentar per post; bias sampel.

Aturan: hanya gunakan informasi dari data yang diberikan, jangan mengarang fakta di luar data.
Setiap klaim tentang isu harus bisa ditelusuri ke post yang dirujuk. Gunakan angka dari data,
bukan perkiraan."""


def _fmt_int(v) -> str:
    return "-" if v is None else f"{v:,}".replace(",", ".")


def build_post_user_message(post: Post, comments: Iterable[Comment], max_chars: int = 300) -> str:
    lines = [
        f"Platform: {post.platform}",
        f"Akun: {post.account}",
        f"URL: {post.url}",
        f"Tanggal (UTC): {post.created_at.isoformat()}",
        f"Tipe media: {post.media_type}",
        f"Like: {_fmt_int(post.likes)} | Komentar: {_fmt_int(post.comments_count)} | "
        f"Share: {_fmt_int(post.shares)} | View: {_fmt_int(post.views)}",
        f"Hashtag: {', '.join(post.hashtags) if post.hashtags else '-'}",
        "",
        "Caption:",
        post.caption.strip() or "(tanpa caption)",
        "",
    ]
    comments = list(comments)
    if not comments:
        lines.append("Komentar: belum ada komentar yang terambil.")
    else:
        lines.append(f"Komentar ({len(comments)} teratas berdasarkan like):")
        for c in comments:
            text = " ".join(c.text.split())
            if len(text) > max_chars:
                text = text[:max_chars]
            prefix = "  ↳ " if c.is_reply else "- "
            lines.append(f"{prefix}[{c.likes or 0} like] {text}")
    return "\n".join(lines)


def input_hash(post: Post, comments: Iterable[Comment]) -> str:
    h = hashlib.sha256()
    h.update(post.caption.encode("utf-8"))
    for c in sorted(comments, key=lambda c: c.comment_id):
        h.update(b"\0")
        h.update(c.comment_id.encode("utf-8"))
        h.update(c.text.encode("utf-8"))
    return h.hexdigest()[:16]


def build_report_user_message(payload_json: str, period_label: str) -> str:
    return (
        f"Rentang observasi: {period_label}\n\n"
        "Berikut data ringkasan per post dalam JSON. Susun laporan sesuai struktur di instruksi.\n\n"
        f"```json\n{payload_json}\n```"
    )
