import json
from datetime import datetime, timezone
from types import SimpleNamespace

from spi.analysis.llm import AnthropicProvider
from spi.analysis.report import build_payload, generate_report
from spi.config import AnalysisConfig
from tests.conftest import make_comment, make_post

SINCE = datetime(2026, 10, 7, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 14, tzinfo=timezone.utc)

ANALYSIS = dict(
    topics=["harga beras"],
    issue_summary="Harga beras naik.",
    sentiment={"pro": 0.1, "kontra": 0.7, "netral": 0.2},
    public_reaction_summary="Netizen kesal.",
    representative_comments=[{"text": "mahal", "stance": "kontra"}],
    intensity="tinggi",
)


def seed(db):
    db.upsert_post(make_post(post_id="p1", account="kompascom", likes=100, created_at=datetime(2026, 10, 10, tzinfo=timezone.utc)))
    db.upsert_post(make_post(post_id="p2", account="detikcom", likes=900, created_at=datetime(2026, 10, 11, tzinfo=timezone.utc)))
    db.upsert_post(make_post(post_id="old", account="detikcom", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    db.upsert_comments([make_comment(post_id="p1", comment_id="c1")])
    for pid in ("p1", "p2", "old"):
        db.save_analysis("instagram", pid, "claude-haiku-5-5", ANALYSIS, "h")


class FakeStream:
    def __init__(self, parent, kwargs):
        self.parent = parent
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="# Laporan Isu Hangat Media Sosial\n\nIsi laporan.")],
            stop_reason=self.parent.stop_reason,
            stop_details=None,
            model="claude-opus-5-5",
            usage=SimpleNamespace(input_tokens=5000, output_tokens=800),
        )


class FakeBetaMessages:
    def __init__(self, stop_reason="end_turn"):
        self.calls = []
        self.stop_reason = stop_reason

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self, kwargs)


class FakeMessages:
    def __init__(self):
        self.count_calls = []

    def count_tokens(self, **kwargs):
        self.count_calls.append(kwargs)
        return SimpleNamespace(input_tokens=4321)


def fake_client(stop_reason="end_turn"):
    return SimpleNamespace(messages=FakeMessages(), beta=SimpleNamespace(messages=FakeBetaMessages(stop_reason)))


def provider_for(client, cfg=None):
    cfg = cfg or AnalysisConfig()
    return AnthropicProvider(client, sleep=lambda s: None, server_side_fallback=cfg.server_side_fallback)


def test_build_payload_groups_by_account_and_filters_period(db):
    seed(db)
    payload = build_payload(db, SINCE, UNTIL, failed_accounts={"tiktok:x": "blocked"})

    assert set(payload["accounts"]) == {"instagram:kompascom", "instagram:detikcom"}
    posts = payload["accounts"]["instagram:detikcom"]["posts"]
    assert [p["post_id"] for p in posts] == ["p2"]
    assert posts[0]["likes"] == 900 and posts[0]["topics"] == ["harga beras"]
    assert payload["accounts"]["instagram:kompascom"]["posts"][0]["comments_collected"] == 1
    assert payload["totals"]["posts"] == 2
    assert payload["failed_accounts"] == {"tiktok:x": "blocked"}
    assert "raw" not in posts[0] and "caption" in posts[0]


def test_generate_report_writes_markdown_with_appendix_and_records(db, tmp_path):
    seed(db)
    client = fake_client()
    cfg = AnalysisConfig()

    result = generate_report(db, cfg, provider_for(client, cfg), SINCE, UNTIL, reports_dir=tmp_path, failed_accounts={})

    path = result["path"]
    text = open(path, encoding="utf-8").read()
    assert text.startswith("# Laporan Isu Hangat Media Sosial")
    assert "## Lampiran: Post Teratas per Akun" in text
    assert "https://www.instagram.com/p/p2/" in text
    assert result["posts_included"] == 2
    assert db.list_reports()[0]["tokens_out"] == 800

    call = client.beta.messages.stream.__self__.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"] == {"effort": "high"}
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["max_tokens"] >= 16000
    body = call["messages"][0]["content"]
    assert "p2" in body and "harga beras" in body


def test_generate_report_without_fallback_when_disabled(db, tmp_path):
    seed(db)
    client = fake_client()
    cfg = AnalysisConfig(server_side_fallback=False)

    generate_report(db, cfg, provider_for(client, cfg), SINCE, UNTIL, reports_dir=tmp_path, failed_accounts={})

    call = client.beta.messages.stream.__self__.calls[0]
    assert "fallbacks" not in call and "betas" not in call


def test_dry_run_counts_tokens_without_generating(db, tmp_path):
    seed(db)
    client = fake_client()

    result = generate_report(db, AnalysisConfig(), provider_for(client), SINCE, UNTIL, reports_dir=tmp_path, failed_accounts={}, dry_run=True)

    assert result["dry_run"] is True
    assert result["estimated_input_tokens"] == 4321
    assert result["posts_included"] == 2
    assert result["estimated_cost_usd"] > 0
    assert client.beta.messages.stream.__self__.calls == []
    assert client.messages.count_calls[0]["model"] == "claude-opus-5-5"


def test_refusal_raises_clear_error(db, tmp_path):
    import pytest

    seed(db)
    client = fake_client(stop_reason="refusal")
    with pytest.raises(RuntimeError, match="menolak"):
        generate_report(db, AnalysisConfig(), provider_for(client), SINCE, UNTIL, reports_dir=tmp_path, failed_accounts={})


def test_report_with_no_analyses_raises(db, tmp_path):
    import pytest

    client = fake_client()
    with pytest.raises(RuntimeError, match="belum ada"):
        generate_report(db, AnalysisConfig(), provider_for(client), SINCE, UNTIL, reports_dir=tmp_path, failed_accounts={})
