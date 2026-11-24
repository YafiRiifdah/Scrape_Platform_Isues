"""Penyimpanan SQLite untuk post, komentar, hasil analisis, checkpoint run, pengguna, dan kredensial Apify.

Semua fungsi yang menyentuh kredensial, run, dan eksekusi menerima `user_id` dan menyaring dengannya.
Tidak ada fungsi yang mengambil kredensial tanpa menyebut pemiliknya.
"""

from __future__ import annotations

import json
import re
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
    user_id INTEGER,
    credential_id INTEGER,
    PRIMARY KEY (run_id, account_key)
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- Token Apify milik pengguna, terenkripsi AES-256-GCM. Setelah dicabut, nonce dan ciphertext dihapus.
CREATE TABLE IF NOT EXISTS apify_credentials (
    credential_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (user_id),
    apify_user_id TEXT,
    apify_username TEXT,
    token_nonce BLOB,
    token_ciphertext BLOB,
    token_last4 TEXT NOT NULL,
    key_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at TEXT NOT NULL,
    validated_at TEXT,
    revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_one_active ON apify_credentials (user_id) WHERE status = 'active';

-- Satu baris per eksekusi Actor Apify, terikat ke pengguna dan kredensial yang dipakai.
CREATE TABLE IF NOT EXISTS apify_executions (
    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (user_id),
    credential_id INTEGER REFERENCES apify_credentials (credential_id),
    run_id TEXT,
    actor TEXT NOT NULL,
    apify_run_id TEXT,
    status TEXT NOT NULL,
    items INTEGER,
    usage_usd REAL,
    error_kind TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_executions_user ON apify_executions (user_id, execution_id);

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
DEFAULT_USER = "default"
_USER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
_CREDENTIAL_PUBLIC_COLUMNS = (
    "credential_id, user_id, apify_user_id, apify_username, token_last4, key_version, status, "
    "created_at, validated_at, revoked_at"
)


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
        self.conn.execute("PRAGMA foreign_keys = ON")

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
        # Multi-user: run lama (sebelum ada pengguna) menjadi milik pengguna default.
        run_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(scrape_runs)")}
        for col in ("user_id", "credential_id"):
            if col not in run_cols:
                self.conn.execute(f"ALTER TABLE scrape_runs ADD COLUMN {col} INTEGER")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON scrape_runs (user_id, started_at)")
        default_id = self.ensure_user(DEFAULT_USER, commit=False)
        self.conn.execute("UPDATE scrape_runs SET user_id = ? WHERE user_id IS NULL", (default_id,))

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

    def get_comments_for_llm(
        self, platform: str, post_id: str, limit: int, top_replied: int = 10, mode: str = "top_liked"
    ) -> list[Comment]:
        """Pilih komentar untuk dirangkum LLM.

        mode "top_replied": hanya komentar level atas dengan balasan terbanyak; like jadi pemecah seri,
        sehingga bila jumlah balasan tidak diketahui (mis. Instagram via Apify gratis) urutannya menurut like.
        mode "top_liked": komentar ter-like, dengan `top_replied` komentar paling dibalas dijamin ikut.
        """
        if mode == "top_replied":
            rows = self.conn.execute(
                self._COMMENT_SELECT
                + " AND c.is_reply = 0 ORDER BY replies_eff DESC, COALESCE(c.likes, 0) DESC, c.created_at ASC LIMIT ?",
                (platform, post_id, limit),
            )
            return [self._row_to_comment(r) for r in rows]
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
    def start_run(self, account_keys: list[str], user_id: Optional[int] = None, credential_id: Optional[int] = None) -> str:
        """Buat run baru milik `user_id` (default: pengguna default) dan catat kredensial pemiliknya."""
        owner = user_id if user_id is not None else self.default_user_id()
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        now = _now()
        self.conn.executemany(
            "INSERT INTO scrape_runs (run_id, account_key, status, started_at, user_id, credential_id) "
            "VALUES (?, ?, 'pending', ?, ?, ?)",
            [(run_id, key, now, owner, credential_id) for key in account_keys],
        )
        self.conn.commit()
        return run_id

    def run_owner(self, run_id: str) -> Optional[int]:
        row = self.conn.execute("SELECT user_id FROM scrape_runs WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
        return row["user_id"] if row else None

    def set_run_credential(self, run_id: str, account_keys: list[str], credential_id: Optional[int]) -> None:
        """Catat kredensial yang benar-benar dipakai untuk akun-akun ini (mis. saat --resume setelah ganti token)."""
        self.conn.executemany(
            "UPDATE scrape_runs SET credential_id = ? WHERE run_id = ? AND account_key = ?",
            [(credential_id, run_id, key) for key in account_keys],
        )
        self.conn.commit()

    def list_runs(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Riwayat run milik `user_id`, terbaru dulu."""
        rows = self.conn.execute(
            """
            SELECT run_id, MIN(started_at) AS started_at, COUNT(*) AS accounts,
                   SUM(status = 'ok') AS ok, SUM(status NOT IN ('ok', 'pending')) AS not_ok,
                   SUM(status = 'pending') AS pending,
                   SUM(posts_found) AS posts_found, SUM(comments_found) AS comments_found,
                   MAX(credential_id) AS credential_id
            FROM scrape_runs WHERE user_id = ?
            GROUP BY run_id ORDER BY MIN(started_at) DESC, MAX(rowid) DESC LIMIT ?
            """,
            (user_id, limit),
        )
        return [dict(r) for r in rows]

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

    def latest_run_id(self, user_id: Optional[int] = None) -> Optional[str]:
        sql, params = "SELECT run_id FROM scrape_runs", []
        if user_id is not None:
            sql, params = sql + " WHERE user_id = ?", [user_id]
        row = self.conn.execute(sql + " ORDER BY started_at DESC, rowid DESC LIMIT 1", params).fetchone()
        return row["run_id"] if row else None

    # ---- pengguna --------------------------------------------------------
    def ensure_user(self, name: str, commit: bool = True) -> int:
        row = self.conn.execute("SELECT user_id FROM users WHERE name = ?", (name,)).fetchone()
        if row:
            return int(row["user_id"])
        cur = self.conn.execute("INSERT INTO users (name, created_at) VALUES (?, ?)", (name, _now()))
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def create_user(self, name: str) -> int:
        if not _USER_NAME_RE.match(name or ""):
            raise ValueError(
                "nama pengguna hanya boleh huruf kecil, angka, titik, garis bawah, atau tanda hubung (maksimal 32 karakter)"
            )
        if self.get_user(name):
            raise ValueError(f"pengguna {name!r} sudah ada")
        return self.ensure_user(name)

    def get_user(self, name: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM users ORDER BY user_id")]

    def default_user_id(self) -> int:
        return self.ensure_user(DEFAULT_USER)

    # ---- kredensial Apify (selalu per pengguna) ---------------------------
    def _revoke_active(self, user_id: int) -> int:
        cur = self.conn.execute(
            "UPDATE apify_credentials SET status = 'revoked', revoked_at = ?, token_nonce = NULL, token_ciphertext = NULL "
            "WHERE user_id = ? AND status = 'active'",
            (_now(), user_id),
        )
        return cur.rowcount

    def replace_credential(
        self,
        user_id: int,
        *,
        apify_user_id: Optional[str],
        apify_username: Optional[str],
        nonce: bytes,
        ciphertext: bytes,
        last4: str,
        key_version: int,
    ) -> int:
        """Simpan kredensial baru milik `user_id`. Kredensial aktif sebelumnya dicabut dan tokennya dihapus."""
        now = _now()
        with self.conn:  # satu transaksi: tidak pernah ada dua kredensial aktif
            self._revoke_active(user_id)
            cur = self.conn.execute(
                """
                INSERT INTO apify_credentials (user_id, apify_user_id, apify_username, token_nonce, token_ciphertext,
                                               token_last4, key_version, status, created_at, validated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (user_id, apify_user_id, apify_username, nonce, ciphertext, last4, key_version, now, now),
            )
        return int(cur.lastrowid)

    def active_credential(self, user_id: int) -> Optional[dict[str, Any]]:
        """Kredensial aktif milik `user_id`, termasuk kolom terenkripsi. Hanya untuk lapisan layanan kredensial."""
        row = self.conn.execute(
            "SELECT * FROM apify_credentials WHERE user_id = ? AND status = 'active'", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def revoke_credentials(self, user_id: int) -> int:
        with self.conn:
            return self._revoke_active(user_id)

    def list_credentials(self, user_id: int) -> list[dict[str, Any]]:
        """Riwayat kredensial milik `user_id` tanpa kolom rahasia."""
        rows = self.conn.execute(
            f"SELECT {_CREDENTIAL_PUBLIC_COLUMNS} FROM apify_credentials WHERE user_id = ? ORDER BY credential_id DESC",
            (user_id,),
        )
        return [dict(r) for r in rows]

    # ---- eksekusi Actor Apify ----------------------------------------------
    def record_execution(
        self,
        *,
        user_id: int,
        credential_id: Optional[int],
        run_id: Optional[str],
        actor: str,
        apify_run_id: Optional[str],
        status: str,
        items: Optional[int],
        usage_usd: Optional[float],
        error_kind: Optional[str],
        started_at: str,
        finished_at: Optional[str],
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO apify_executions (user_id, credential_id, run_id, actor, apify_run_id, status, items,
                                          usage_usd, error_kind, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, credential_id, run_id, actor, apify_run_id, status, items, usage_usd, error_kind, started_at, finished_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_executions(self, user_id: int, run_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        """Eksekusi Actor milik `user_id`, terbaru dulu."""
        sql, params = "SELECT * FROM apify_executions WHERE user_id = ?", [user_id]
        if run_id:
            sql, params = sql + " AND run_id = ?", params + [run_id]
        rows = self.conn.execute(sql + " ORDER BY execution_id DESC LIMIT ?", params + [limit])
        return [dict(r) for r in rows]

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
