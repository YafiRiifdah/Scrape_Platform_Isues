"""Tahap 2: laporan akhir lintas akun dengan Claude Opus 5.5 (streaming)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from spi.analysis import prompts
from spi.analysis.llm import Provider
from spi.config import AnalysisConfig
from spi.storage import Storage

log = logging.getLogger("spi.analysis.report")

ESTIMATED_OUTPUT_TOKENS = 15_000
REPORT_MAX_TOKENS = 32_000


def _period_label(since: datetime, until: datetime) -> str:
    return f"{since:%d %b %Y} - {until:%d %b %Y} (UTC)"


def build_payload(db: Storage, since: datetime, until: datetime, failed_accounts: dict[str, str]) -> dict[str, Any]:
    """Ringkas semua analisis per post dalam rentang waktu menjadi JSON kompak per akun."""
    accounts: dict[str, dict[str, Any]] = {}
    total_posts = total_comments = 0
    for post, analysis in db.get_analyses_since(since):
        if post.created_at > until:
            continue
        key = f"{post.platform}:{post.account}"
        bucket = accounts.setdefault(key, {"platform": post.platform, "account": post.account, "posts": []})
        n_comments = db.count_comments(post.platform, post.post_id)
        bucket["posts"].append(
            {
                "post_id": post.post_id,
                "url": post.url,
                "created_at": post.created_at.isoformat(),
                "media_type": post.media_type,
                "caption": post.caption[:500],
                "hashtags": post.hashtags,
                "likes": post.likes,
                "comments_count": post.comments_count,
                "shares": post.shares,
                "views": post.views,
                "comments_collected": n_comments,
                **analysis,
            }
        )
        total_posts += 1
        total_comments += n_comments
    for bucket in accounts.values():
        bucket["post_count"] = len(bucket["posts"])
        bucket["total_likes"] = sum(p["likes"] or 0 for p in bucket["posts"])
    return {
        "period": _period_label(since, until),
        "totals": {"accounts": len(accounts), "posts": total_posts, "comments_collected": total_comments},
        "failed_accounts": failed_accounts,
        "accounts": accounts,
    }


def _appendix(payload: dict[str, Any], top_n: int = 5) -> str:
    lines = ["", "---", "", "## Lampiran: Post Teratas per Akun", "", "_Dibuat otomatis dari database, diurutkan berdasarkan like._", ""]
    for key, bucket in sorted(payload["accounts"].items()):
        lines.append(f"### {key} ({bucket['post_count']} post)")
        lines.append("")
        lines.append("| Tanggal | Like | Komentar | Topik | URL |")
        lines.append("|---|---|---|---|---|")
        top = sorted(bucket["posts"], key=lambda p: p["likes"] or 0, reverse=True)[:top_n]
        for p in top:
            topics = ", ".join(p.get("topics", []))
            lines.append(
                f"| {p['created_at'][:10]} | {p['likes'] if p['likes'] is not None else '-'} | "
                f"{p['comments_count'] if p['comments_count'] is not None else '-'} | {topics} | {p['url']} |"
            )
        lines.append("")
    if payload["failed_accounts"]:
        lines.append("### Akun yang gagal diambil")
        lines.append("")
        for key, err in payload["failed_accounts"].items():
            lines.append(f"- `{key}`: {err}")
        lines.append("")
    return "\n".join(lines)


def generate_report(
    db: Storage,
    cfg: AnalysisConfig,
    provider: Provider,
    since: datetime,
    until: datetime,
    reports_dir: str | Path,
    failed_accounts: dict[str, str],
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    payload = build_payload(db, since, until, failed_accounts)
    if payload["totals"]["posts"] == 0:
        raise RuntimeError(
            "belum ada hasil analisis per post dalam rentang ini; jalankan `spi scrape` lalu `spi report` tanpa --skip-analysis."
        )

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    user_message = prompts.build_report_user_message(payload_json, payload["period"])

    if dry_run:
        tokens_in = provider.count_tokens(model=cfg.report_model, system=prompts.SYSTEM_REPORT, user=user_message) or 0
        return {
            "dry_run": True,
            "posts_included": payload["totals"]["posts"],
            "estimated_input_tokens": tokens_in,
            "estimated_output_tokens": ESTIMATED_OUTPUT_TOKENS,
            "estimated_cost_usd": provider.cost_usd(cfg.report_model, tokens_in, ESTIMATED_OUTPUT_TOKENS),
            "model": cfg.report_model,
            "provider": provider.name,
        }

    log.info("menyusun laporan dengan %s (%s) untuk %d post", cfg.report_model, provider.name, payload["totals"]["posts"])
    result = provider.generate(model=cfg.report_model, system=prompts.SYSTEM_REPORT, user=user_message, max_tokens=REPORT_MAX_TOKENS)
    if result.refusal:
        raise RuntimeError(f"model menolak menyusun laporan (refusal): {result.refusal}")
    if not result.text.strip():
        raise RuntimeError("model mengembalikan laporan kosong")

    text = result.text.strip() + "\n" + _appendix(payload)

    now = now or datetime.now(timezone.utc)
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{now:%Y-%m-%d_%H%M}_isu-hangat.md"
    path.write_text(text, encoding="utf-8")

    db.record_report(str(path), result.model or cfg.report_model, payload["totals"]["posts"], result.tokens_in, result.tokens_out)
    return {
        "dry_run": False,
        "path": str(path),
        "posts_included": payload["totals"]["posts"],
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "model": result.model or cfg.report_model,
        "provider": provider.name,
    }
