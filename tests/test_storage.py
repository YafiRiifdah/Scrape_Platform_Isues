from datetime import datetime, timezone

from tests.conftest import make_comment, make_post


def test_upsert_post_is_idempotent_and_updates_metrics(db):
    db.upsert_post(make_post(likes=100))
    db.upsert_post(make_post(likes=250))

    posts = db.get_posts_since(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert len(posts) == 1
    assert posts[0].likes == 250
    assert posts[0].hashtags == ["beras"]


def test_get_posts_since_filters_by_date_and_platform(db):
    db.upsert_post(make_post(post_id="old", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    db.upsert_post(make_post(post_id="new", created_at=datetime(2026, 10, 12, tzinfo=timezone.utc)))
    db.upsert_post(
        make_post(platform="tiktok", post_id="tt1", created_at=datetime(2026, 10, 12, tzinfo=timezone.utc))
    )

    since = datetime(2026, 10, 7, tzinfo=timezone.utc)
    assert {p.post_id for p in db.get_posts_since(since)} == {"new", "tt1"}
    assert {p.post_id for p in db.get_posts_since(since, platform="tiktok")} == {"tt1"}


def test_upsert_comments_is_idempotent(db):
    db.upsert_post(make_post())
    db.upsert_comments([make_comment(comment_id="c1"), make_comment(comment_id="c2")])
    db.upsert_comments([make_comment(comment_id="c2", likes=99)])

    comments = db.get_comments_for_post("instagram", "ABC123", limit=10)
    assert len(comments) == 2
    assert {c.comment_id: c.likes for c in comments}["c2"] == 99


def test_get_comments_for_post_orders_by_likes_desc_and_limits(db):
    db.upsert_post(make_post())
    db.upsert_comments(
        [
            make_comment(comment_id="low", likes=1),
            make_comment(comment_id="high", likes=500),
            make_comment(comment_id="mid", likes=50),
        ]
    )

    comments = db.get_comments_for_post("instagram", "ABC123", limit=2)
    assert [c.comment_id for c in comments] == ["high", "mid"]


def test_replies_count_is_stored_or_derived_from_child_rows(db):
    db.upsert_post(make_post())
    db.upsert_comments(
        [
            make_comment(comment_id="tt", likes=1, replies_count=42),  # TikTok: angka dari Apify
            make_comment(comment_id="ig", likes=1),  # Instagram: dihitung dari balasan tersimpan
            make_comment(comment_id="ig-r1", likes=0, is_reply=True, parent_comment_id="ig"),
            make_comment(comment_id="ig-r2", likes=0, is_reply=True, parent_comment_id="ig"),
            make_comment(comment_id="lonely", likes=1),
        ]
    )

    by_id = {c.comment_id: c for c in db.get_comments_for_post("instagram", "ABC123", limit=10)}
    assert by_id["tt"].replies_count == 42
    assert by_id["ig"].replies_count == 2
    assert by_id["lonely"].replies_count == 0


def test_get_comments_for_llm_guarantees_most_replied_even_with_few_likes(db):
    db.upsert_post(make_post())
    comments = [make_comment(comment_id=f"like{i}", likes=100 - i) for i in range(5)]
    comments.append(make_comment(comment_id="viral_reply", likes=0, replies_count=500))
    db.upsert_comments(comments)

    got = db.get_comments_for_llm("instagram", "ABC123", limit=3, top_replied=1)

    ids = [c.comment_id for c in got]
    assert len(ids) == 3
    assert "viral_reply" in ids
    assert ids[0] == "like0"  # sisanya tetap urut like


def test_init_db_adds_replies_count_to_existing_database(tmp_path):
    import sqlite3

    from spi.storage import Storage

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE comments (platform TEXT, comment_id TEXT, post_id TEXT, text TEXT, created_at TEXT,
            likes INTEGER, is_reply INTEGER, parent_comment_id TEXT, scraped_at TEXT, PRIMARY KEY (platform, comment_id));
        """
    )
    conn.commit()
    conn.close()

    db = Storage(path)
    db.init_db()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(comments)")}
    db.close()
    assert "replies_count" in cols


def test_comment_counts_per_post(db):
    db.upsert_post(make_post(post_id="p1"))
    db.upsert_post(make_post(post_id="p2"))
    db.upsert_comments([make_comment(post_id="p1", comment_id="a"), make_comment(post_id="p1", comment_id="b")])

    assert db.count_comments("instagram", "p1") == 2
    assert db.count_comments("instagram", "p2") == 0


def test_scrape_run_lifecycle_and_pending_accounts(db):
    run_id = db.start_run(["instagram:kompascom", "tiktok:detikcom"])

    assert db.pending_accounts(run_id) == ["instagram:kompascom", "tiktok:detikcom"]

    db.finish_account(run_id, "instagram:kompascom", status="ok", posts_found=5, comments_found=120)
    assert db.pending_accounts(run_id) == ["tiktok:detikcom"]

    db.finish_account(run_id, "tiktok:detikcom", status="failed", error="blocked")
    assert db.pending_accounts(run_id) == []

    summary = db.run_summary(run_id)
    assert summary["instagram:kompascom"]["status"] == "ok"
    assert summary["instagram:kompascom"]["posts_found"] == 5
    assert summary["tiktok:detikcom"]["error"] == "blocked"


def test_rate_limited_account_stays_pending_for_resume(db):
    run_id = db.start_run(["instagram:a"])
    db.finish_account(run_id, "instagram:a", status="rate_limited", error="429")

    assert db.pending_accounts(run_id, include_retryable=True) == ["instagram:a"]
    assert db.pending_accounts(run_id) == []


def test_save_and_get_analysis_with_input_hash(db):
    db.upsert_post(make_post())
    assert db.get_unanalyzed_posts(datetime(2026, 1, 1, tzinfo=timezone.utc)) == [
        ("instagram", "ABC123")
    ]

    db.save_analysis("instagram", "ABC123", model="claude-haiku-5-5", analysis={"topics": ["beras"]}, input_hash="h1", tokens_in=10, tokens_out=5)

    assert db.get_unanalyzed_posts(datetime(2026, 1, 1, tzinfo=timezone.utc)) == []
    stored = db.get_analysis("instagram", "ABC123")
    assert stored["analysis"] == {"topics": ["beras"]}
    assert stored["input_hash"] == "h1"


def test_record_report(db):
    db.record_report(path="reports/x.md", model="claude-opus-5-5", posts_included=12, tokens_in=100, tokens_out=50)
    reports = db.list_reports()
    assert reports[0]["path"] == "reports/x.md"
    assert reports[0]["posts_included"] == 12
