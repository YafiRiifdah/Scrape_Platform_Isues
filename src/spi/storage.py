"""Penyimpanan SQLite untuk post, komentar, hasil analisis, dan checkpoint run."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from spi.models import Comment, Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    platform TEXT NOT NULL,
    post_id TEXT NOT NULL,
    account TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    hashtags TEXT NOT NULL DEFAULT '[]',
    media_type TEXT NOT NULL DEFAULT 'unknown',
    likes INTEGER,
    comments_count INTEGER,
    shares INTEGER,
    views INTEGER,
    raw TEXT,
    scraped_at TEXT NOT NULL,
    PRIMARY KEY (platform, post_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts (created_at);

CREATE TABLE IF NOT EXISTS comments (
    platform TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT,
    likes INTEGER,
    is_reply INTEGER NOT NULL DEFAULT 0,
    parent_comment_id TEXT,
    replies_count INTEGER,
    scraped_at TEXT NOT NULL,
    PRIMARY KEY (platform, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments (platform, post_id);

CREATE TABLE IF NOT EXISTS post_analysis (
    platform TEXT NOT NULL,
    post_id TEXT NOT NULL,
    model TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    analyzed_at TEXT NOT NULL,
    PRIMARY KEY (platform, post_id)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    status TEXT NOT NULL,
    posts_found INTEGER NOT NULL DEFAULT 0,
    comments_found INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (run_id, account_key)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    model TEXT NOT NULL,
    posts_included INTEGER NOT NULL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    created_at TEXT NOT NULL
);
"""

RETRYABLE_STATUSES = ("pending", "rate_limited")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class Storage:
    def __init__(self, path: str | Path = "data/spi.sqlite"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    # ---- lifecycle -------------------------------------------------------
    def init_db(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Tambahkan kolom baru ke database lama tanpa menghapus data."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(comments)")}
        if "replies_count" not in cols:
            self.conn.execute("ALTER TABLE comments ADD COLUMN replies_count INTEGER")

    def close(self) -> None:
        self.conn.close()

    # ---- posts -----------------------------------------------------------
    def upsert_post(self, post: Post) -> None:
        self.conn.execute(
            """
            INSERT INTO posts (platform, post_id, account, url, created_at, caption, hashtags,
                               media_type, likes, comments_count, shares, views, raw, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, post_id) DO UPDATE SET
                account=excluded.account, url=excluded.url, created_at=excluded.created_at,
                caption=excluded.caption, hashtags=excluded.hashtags, media_type=excluded.media_type,
                likes=excluded.likes, comments_count=excluded.comments_count, shares=excluded.shares,
                views=excluded.views, raw=COALESCE(excluded.raw, posts.raw), scraped_at=excluded.scraped_at
            """,
            (
                post.platform,
                post.post_id,
                post.account,
                post.url,
                _dt(post.created_at),
                post.caption,
                json.dumps(post.hashtags, ensure_ascii=False),
                post.media_type,
                post.likes,
                post.comments_count,
                post.shares,
                post.views,
                json.dumps(post.raw, ensure_ascii=False, default=str) if post.raw is not None else None,
                _now(),
            ),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_post(row: sqlite3.Row) -> Post:
        return Post(
            platform=row["platform"],
            post_id=row["post_id"],
            account=row["account"],
            url=row["url"],
            created_at=_parse_dt(row["created_at"]),
            caption=row["caption"],
            hashtags=json.loads(row["hashtags"]),
            media_type=row["media_type"],
            likes=row["likes"],
            comments_count=row["comments_count"],
            shares=row["shares"],
            views=row["views"],
            raw=json.loads(row["raw"]) if row["raw"] else None,
        )

    def get_posts_since(self, since: datetime, platform: Optional[str] = None) -> list[Post]:
        sql = "SELECT * FROM posts WHERE created_at >= ?"
        params: list[Any] = [_dt(since)]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        sql += " ORDER BY created_at DESC"
        return [self._row_to_post(r) for r in self.conn.execute(sql, params)]

    def get_post(self, platform: str, post_id: str) -> Optional[Post]:
        row = self.conn.execute(
            "SELECT * FROM posts WHERE platform = ? AND post_id = ?", (platform, post_id)
        ).fetchone()
        return self._row_to_post(row) if row else None

    # ---- comments --------------------------------------------------------
    def upsert_comments(self, comments: Iterable[Comment]) -> int:
        n = 0
        for c in comments:
            self.conn.execute(
                """
                INSERT INTO comments (platform, comment_id, post_id, text, created_at, likes,
                                      is_reply, parent_comment_id, replies_count, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    text=excluded.text, created_at=COALESCE(excluded.created_at, comments.created_at),
                    likes=excluded.likes, is_reply=excluded.is_reply,
                    parent_comment_id=excluded.parent_comment_id,
                    replies_count=COALESCE(excluded.replies_count, comments.replies_count),
                    scraped_at=excluded.scraped_at
                """,
                (
                    c.platform,
                    c.comment_id,
                    c.post_id,
                    c.text,
                    _dt(c.created_at),
                    c.likes,
                    1 if c.is_reply else 0,
                    c.parent_comment_id,
                    c.replies_count,
                    _now(),
                ),
            )
            n += 1
        self.conn.commit()
        return n

    @staticmethod
    def _row_to_comment(row: sqlite3.Row) -> Comment:
        return Comment(
            platform=row["platform"],
            comment_id=row["comment_id"],
            post_id=row["post_id"],
            text=row["text"],
            created_at=_parse_dt(row["created_at"]),
            likes=row["likes"],
            is_reply=bool(row["is_reply"]),
            parent_comment_id=row["parent_comment_id"],
            replies_count=row["replies_eff"] if "replies_eff" in row.keys() else row["replies_count"],
        )

    # replies_count tersimpan (TikTok/Apify) atau dihitung dari balasan tersimpan (Instagram)
    _COMMENT_SELECT = """
        SELECT c.*, COALESCE(c.replies_count,
            (SELECT COUNT(*) FROM comments r WHERE r.platform = c.platform AND r.parent_comment_id = c.comment_id)
        ) AS replies_eff
        FROM comments c WHERE c.platform = ? AND c.post_id = ?
    """

    def get_comments_for_post(self, platform: str, post_id: str, limit: int) -> list[Comment]:
        rows = self.conn.execute(
            self._COMMENT_SELECT + " ORDER BY COALESCE(c.likes, 0) DESC, c.created_at ASC LIMIT ?",
            (platform, post_id, limit),
        )
        return [self._row_to_comment(r) for r in rows]

    def get_comments_for_llm(self, platform: str, post_id: str, limit: int, top_replied: int = 10) -> list[Comment]:
        """Komentar teratas berdasarkan like, dengan jaminan komentar paling banyak dibalas ikut serta."""
        replied = [
            self._row_to_comment(r)
            for r in self.conn.execute(
                self._COMMENT_SELECT + " AND c.is_reply = 0 ORDER BY replies_eff DESC, COALESCE(c.likes, 0) DESC LIMIT ?",
                (platform, post_id, min(top_replied, limit)),
            )
            if r["replies_eff"]
        ]
        chosen = {c.comment_id: c for c in replied}
        for c in self.get_comments_for_post(platform, post_id, limit):
            if len(chosen) >= limit:
                break
            chosen.setdefault(c.comment_id, c)
        return sorted(chosen.values(), key=lambda c: (c.likes or 0, c.replies_count or 0), reverse=True)

    def count_comments(self, platform: str, post_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM comments WHERE platform = ? AND post_id = ?",
            (platform, post_id),
        ).fetchone()
        return int(row["n"])

    # ---- scrape runs -----------------------------------------------------
    def start_run(self, account_keys: list[str]) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        now = _now()
        self.conn.executemany(
            "INSERT INTO scrape_runs (run_id, account_key, status, started_at) VALUES (?, ?, 'pending', ?)",
            [(run_id, key, now) for key in account_keys],
        )
        self.conn.commit()
        return run_id

    def pending_accounts(self, run_id: str, include_retryable: bool = False) -> list[str]:
        statuses = RETRYABLE_STATUSES if include_retryable else ("pending",)
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT account_key FROM scrape_runs WHERE run_id = ? AND status IN ({placeholders}) "
            "ORDER BY rowid",
            (run_id, *statuses),
        )
        return [r["account_key"] for r in rows]

    def finish_account(
        self,
        run_id: str,
        account_key: str,
        status: str,
        posts_found: int = 0,
        comments_found: int = 0,
        error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE scrape_runs SET status = ?, posts_found = ?, comments_found = ?, error = ?, finished_at = ?
            WHERE run_id = ? AND account_key = ?
            """,
            (status, posts_found, comments_found, error, _now(), run_id, account_key),
        )
        self.conn.commit()

    def run_summary(self, run_id: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM scrape_runs WHERE run_id = ? ORDER BY rowid", (run_id,)
        )
        return {
            r["account_key"]: {
                "status": r["status"],
                "posts_found": r["posts_found"],
                "comments_found": r["comments_found"],
                "error": r["error"],
            }
            for r in rows
        }

    def latest_run_id(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT run_id FROM scrape_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row["run_id"] if row else None

    # ---- analysis --------------------------------------------------------
    def get_unanalyzed_posts(self, since: datetime) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT p.platform, p.post_id FROM posts p
            LEFT JOIN post_analysis a ON a.platform = p.platform AND a.post_id = p.post_id
            WHERE p.created_at >= ? AND a.post_id IS NULL
            ORDER BY p.created_at DESC
            """,
            (_dt(since),),
        )
        return [(r["platform"], r["post_id"]) for r in rows]

    def save_analysis(
        self,
        platform: str,
        post_id: str,
        model: str,
        analysis: dict[str, Any],
        input_hash: str,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO post_analysis (platform, post_id, model, analysis_json, input_hash,
                                       tokens_in, tokens_out, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, post_id) DO UPDATE SET
                model=excluded.model, analysis_json=excluded.analysis_json, input_hash=excluded.input_hash,
                tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out, analyzed_at=excluded.analyzed_at
            """,
            (platform, post_id, model, json.dumps(analysis, ensure_ascii=False), input_hash, tokens_in, tokens_out, _now()),
        )
        self.conn.commit()

    def get_analysis(self, platform: str, post_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM post_analysis WHERE platform = ? AND post_id = ?", (platform, post_id)
        ).fetchone()
        if not row:
            return None
        return {
            "analysis": json.loads(row["analysis_json"]),
            "input_hash": row["input_hash"],
            "model": row["model"],
            "tokens_in": row["tokens_in"],
            "tokens_out": row["tokens_out"],
        }

    def get_analyses_since(self, since: datetime) -> list[tuple[Post, dict[str, Any]]]:
        rows = self.conn.execute(
            """
            SELECT p.*, a.analysis_json FROM posts p
            JOIN post_analysis a ON a.platform = p.platform AND a.post_id = p.post_id
            WHERE p.created_at >= ? ORDER BY p.account, p.created_at DESC
            """,
            (_dt(since),),
        )
        return [(self._row_to_post(r), json.loads(r["analysis_json"])) for r in rows]

    # ---- reports ---------------------------------------------------------
    def record_report(
        self, path: str, model: str, posts_included: int, tokens_in: Optional[int], tokens_out: Optional[int]
    ) -> None:
        self.conn.execute(
            "INSERT INTO reports (path, model, posts_included, tokens_in, tokens_out, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (path, model, posts_included, tokens_in, tokens_out, _now()),
        )
        self.conn.commit()

    def list_reports(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM reports ORDER BY report_id DESC")
        return [dict(r) for r in rows]

    # ---- status ----------------------------------------------------------
    def account_stats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT p.platform, p.account, COUNT(DISTINCT p.post_id) AS posts,
                   (SELECT COUNT(*) FROM comments c WHERE c.platform = p.platform
                        AND c.post_id IN (SELECT post_id FROM posts q WHERE q.platform = p.platform AND q.account = p.account)) AS comments,
                   MAX(p.created_at) AS latest_post
            FROM posts p GROUP BY p.platform, p.account ORDER BY p.platform, p.account
            """
        )
        return [dict(r) for r in rows]
