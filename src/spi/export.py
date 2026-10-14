"""Ekspor CSV (UTF-8 dengan BOM agar Excel membaca Bahasa Indonesia dengan benar)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from spi.storage import Storage

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _write(path: Path, header: list[str], rows: Iterable[dict[str, Any]]) -> Path:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def export_csv(db: Storage, out_dir: str | Path, since: Optional[datetime] = None) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    since = since or EPOCH
    posts = db.get_posts_since(since)

    post_rows = [
        {
            **p.model_dump(exclude={"raw", "hashtags"}),
            "created_at": p.created_at.isoformat(),
            "hashtags": " ".join(p.hashtags),
        }
        for p in posts
    ]
    post_header = ["platform", "account", "post_id", "url", "created_at", "media_type", "caption", "hashtags", "likes", "comments_count", "shares", "views"]

    comment_rows = []
    analysis_rows = []
    for p in posts:
        for c in db.get_comments_for_post(p.platform, p.post_id, limit=10**9):
            comment_rows.append(
                {
                    **c.model_dump(),
                    "account": p.account,
                    "created_at": c.created_at.isoformat() if c.created_at else "",
                    "is_reply": int(c.is_reply),
                }
            )
        a = db.get_analysis(p.platform, p.post_id)
        if a:
            an = a["analysis"]
            sentiment = an.get("sentiment", {})
            analysis_rows.append(
                {
                    "platform": p.platform,
                    "account": p.account,
                    "post_id": p.post_id,
                    "url": p.url,
                    "created_at": p.created_at.isoformat(),
                    "topics": "; ".join(an.get("topics", [])),
                    "issue_summary": an.get("issue_summary", ""),
                    "sentiment_pro": sentiment.get("pro", ""),
                    "sentiment_kontra": sentiment.get("kontra", ""),
                    "sentiment_netral": sentiment.get("netral", ""),
                    "intensity": an.get("intensity", ""),
                    "public_reaction_summary": an.get("public_reaction_summary", ""),
                    "model": a["model"],
                }
            )
    comment_header = ["platform", "account", "post_id", "comment_id", "text", "created_at", "likes", "replies_count", "is_reply", "parent_comment_id"]
    analysis_header = ["platform", "account", "post_id", "url", "created_at", "topics", "issue_summary", "sentiment_pro", "sentiment_kontra", "sentiment_netral", "intensity", "public_reaction_summary", "model"]

    return [
        _write(out / "posts.csv", post_header, post_rows),
        _write(out / "comments.csv", comment_header, comment_rows),
        _write(out / "post_analysis.csv", analysis_header, analysis_rows),
    ]
