import csv
from datetime import datetime, timezone

from spi.export import export_csv
from tests.conftest import make_comment, make_post


def test_export_writes_three_csv_files_with_bom(db, tmp_path):
    db.upsert_post(make_post(post_id="p1", caption="Harga beras naik, petani “senang”"))
    db.upsert_comments([make_comment(post_id="p1", comment_id="c1", text="setuju; mantap", replies_count=7)])
    db.save_analysis("instagram", "p1", "claude-haiku-5-5", {"topics": ["beras"], "sentiment": {"pro": 1.0, "kontra": 0.0, "netral": 0.0}, "intensity": "rendah"}, "h")

    written = export_csv(db, tmp_path, since=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert {p.name for p in written} == {"posts.csv", "comments.csv", "post_analysis.csv"}
    raw = (tmp_path / "posts.csv").read_bytes()
    assert raw.startswith("﻿".encode("utf-8"))

    with open(tmp_path / "posts.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["post_id"] == "p1"
    assert rows[0]["hashtags"] == "beras"
    assert "senang" in rows[0]["caption"]

    with open(tmp_path / "comments.csv", encoding="utf-8-sig", newline="") as f:
        crows = list(csv.DictReader(f))
    assert crows[0]["text"] == "setuju; mantap"
    assert crows[0]["replies_count"] == "7"

    with open(tmp_path / "post_analysis.csv", encoding="utf-8-sig", newline="") as f:
        arows = list(csv.DictReader(f))
    assert arows[0]["topics"] == "beras"
    assert arows[0]["sentiment_pro"] == "1.0"
    assert arows[0]["intensity"] == "rendah"
