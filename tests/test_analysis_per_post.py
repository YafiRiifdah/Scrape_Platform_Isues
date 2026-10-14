from datetime import datetime, timezone
from types import SimpleNamespace

import anthropic
import pytest
from pydantic import ValidationError

from spi.analysis import prompts
from spi.analysis.llm import AnthropicProvider
from spi.analysis.per_post import analyze_pending
from spi.analysis.schemas import PostAnalysis
from spi.config import AnalysisConfig
from tests.conftest import make_comment, make_post

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)

VALID_ANALYSIS = dict(
    topics=["harga beras", "kebijakan impor"],
    issue_summary="Pemerintah membuka impor beras untuk menekan harga.",
    sentiment={"pro": 0.2, "kontra": 0.6, "netral": 0.2},
    public_reaction_summary="Mayoritas komentar menyalahkan pemerintah.",
    representative_comments=[{"text": "petani makin susah", "stance": "kontra"}],
    intensity="tinggi",
)


# ---- schema -----------------------------------------------------------------
def test_post_analysis_accepts_valid_payload():
    a = PostAnalysis.model_validate(VALID_ANALYSIS)
    assert a.intensity == "tinggi"
    assert a.representative_comments[0].stance == "kontra"


def test_post_analysis_requires_sentiment_keys_and_sum():
    bad = dict(VALID_ANALYSIS, sentiment={"pro": 0.9, "kontra": 0.9, "netral": 0.0})
    with pytest.raises(ValidationError):
        PostAnalysis.model_validate(bad)


def test_post_analysis_limits_topics_and_comments():
    with pytest.raises(ValidationError):
        PostAnalysis.model_validate(dict(VALID_ANALYSIS, topics=[]))
    with pytest.raises(ValidationError):
        PostAnalysis.model_validate(dict(VALID_ANALYSIS, representative_comments=[{"text": "x", "stance": "pro"}] * 4))


# ---- prompts ------------------------------------------------------------------
def test_build_post_message_contains_metadata_caption_and_truncated_comments():
    post = make_post(caption="Caption uji", likes=10, comments_count=3)
    comments = [make_comment(comment_id="1", text="a" * 500, likes=9), make_comment(comment_id="2", text="pendek", likes=1)]

    msg = prompts.build_post_user_message(post, comments, max_chars=300)

    assert "instagram" in msg and "kompascom" in msg and "Caption uji" in msg
    assert "pendek" in msg
    assert "a" * 300 in msg and "a" * 301 not in msg
    assert "[9 like, 0 balasan]" in msg


def test_build_post_message_shows_reply_counts():
    post = make_post()
    msg = prompts.build_post_user_message(post, [make_comment(comment_id="1", text="ramai", likes=3, replies_count=58)])
    assert "[3 like, 58 balasan]" in msg


def test_post_analysis_accepts_discussion_triggers():
    a = PostAnalysis.model_validate(
        dict(VALID_ANALYSIS, discussion_triggers=[{"text": "petani makin susah", "replies_count": 58, "why": "memicu debat impor"}])
    )
    assert a.discussion_triggers[0].replies_count == 58


def test_post_analysis_discussion_triggers_default_empty_and_max_two():
    assert PostAnalysis.model_validate(VALID_ANALYSIS).discussion_triggers == []
    with pytest.raises(ValidationError):
        PostAnalysis.model_validate(dict(VALID_ANALYSIS, discussion_triggers=[{"text": "x", "replies_count": 1, "why": "y"}] * 3))


def test_analyze_pending_uses_llm_selection_with_top_replied(db):
    db.upsert_post(make_post(post_id="p0"))
    comments = [make_comment(post_id="p0", comment_id=f"l{i}", likes=50 - i) for i in range(5)]
    comments.append(make_comment(post_id="p0", comment_id="hot", likes=0, replies_count=99))
    db.upsert_comments(comments)
    client = fake_client()

    run(db, AnalysisConfig(max_comments_to_llm=3), client)

    body = client.messages.calls[0]["messages"][0]["content"]
    assert "99 balasan" in body


def test_input_hash_changes_when_comments_change():
    post = make_post()
    h1 = prompts.input_hash(post, [make_comment(comment_id="1", text="a")])
    h2 = prompts.input_hash(post, [make_comment(comment_id="1", text="a"), make_comment(comment_id="2", text="b")])
    h1_again = prompts.input_hash(post, [make_comment(comment_id="1", text="a")])
    assert h1 != h2 and h1 == h1_again


def test_system_prompts_are_in_indonesian_and_mention_schema_fields():
    assert "Bahasa Indonesia" in prompts.SYSTEM_PER_POST
    assert "topics" in prompts.SYSTEM_PER_POST and "sentiment" in prompts.SYSTEM_PER_POST
    assert "Bahasa Indonesia" in prompts.SYSTEM_REPORT


# ---- per_post pipeline ----------------------------------------------------------
class FakeMessages:
    def __init__(self, script):
        self.script = list(script)  # setiap item: "ok" | "refusal" | exception instance
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        action = self.script.pop(0) if self.script else "ok"
        if isinstance(action, Exception):
            raise action
        if action == "refusal":
            return SimpleNamespace(
                parsed_output=None,
                stop_reason="refusal",
                stop_details=SimpleNamespace(category="general_harms", explanation="x"),
                usage=SimpleNamespace(input_tokens=100, output_tokens=0),
            )
        return SimpleNamespace(
            parsed_output=PostAnalysis.model_validate(VALID_ANALYSIS),
            stop_reason="end_turn",
            stop_details=None,
            usage=SimpleNamespace(input_tokens=100, output_tokens=40),
        )


def fake_client(script=()):
    return SimpleNamespace(messages=FakeMessages(script))


def run(db, cfg, client, sleep=lambda s: None):
    return analyze_pending(db, cfg, AnthropicProvider(client, sleep=sleep), SINCE)


def seed(db, n=2):
    for i in range(n):
        db.upsert_post(make_post(post_id=f"p{i}"))
        db.upsert_comments([make_comment(post_id=f"p{i}", comment_id=f"p{i}-c", text="komentar", likes=i)])


def test_analyze_pending_saves_analysis_and_tokens(db):
    seed(db, 2)
    client = fake_client()

    stats = run(db, AnalysisConfig(), client)

    assert stats["analyzed"] == 2 and stats["failed"] == 0
    assert stats["tokens_in"] == 200 and stats["tokens_out"] == 80
    stored = db.get_analysis("instagram", "p0")
    assert stored["analysis"]["topics"] == ["harga beras", "kebijakan impor"]
    assert stored["model"] == "claude-haiku-5-5"
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_format"] is PostAnalysis
    assert call["output_config"] == {"effort": "low"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_analyze_pending_skips_posts_with_unchanged_hash(db):
    seed(db, 1)
    client = fake_client()
    run(db, AnalysisConfig(), client)

    stats = run(db, AnalysisConfig(), client)

    assert stats["analyzed"] == 0 and stats["skipped"] == 1
    assert len(client.messages.calls) == 1


def test_analyze_pending_reanalyzes_when_comments_changed(db):
    seed(db, 1)
    client = fake_client()
    run(db, AnalysisConfig(), client)
    db.upsert_comments([make_comment(post_id="p0", comment_id="baru", text="komentar baru", likes=50)])

    stats = run(db, AnalysisConfig(), client)

    assert stats["analyzed"] == 1
    assert len(client.messages.calls) == 2


def test_refusal_is_recorded_as_failed_and_pipeline_continues(db):
    seed(db, 2)
    client = fake_client(["refusal", "ok"])

    stats = run(db, AnalysisConfig(), client)

    assert stats["analyzed"] == 1 and stats["failed"] == 1
    assert "refusal" in stats["errors"][0]


def test_rate_limit_waits_and_retries(db):
    seed(db, 1)
    response = SimpleNamespace(headers={"retry-after": "7"}, status_code=429, request=None)
    err = anthropic.RateLimitError("rate", response=response, body=None)
    client = fake_client([err, "ok"])
    slept = []

    stats = run(db, AnalysisConfig(), client, sleep=slept.append)

    assert stats["analyzed"] == 1
    assert slept == [7.0]


def test_bad_request_is_failed_without_retry(db):
    seed(db, 1)
    response = SimpleNamespace(headers={}, status_code=400, request=None)
    err = anthropic.BadRequestError("bad", response=response, body=None)
    client = fake_client([err])

    stats = run(db, AnalysisConfig(), client)

    assert stats["failed"] == 1 and len(client.messages.calls) == 1


def test_post_without_comments_is_still_analyzed(db):
    db.upsert_post(make_post(post_id="solo"))
    client = fake_client()
    stats = run(db, AnalysisConfig(), client)
    assert stats["analyzed"] == 1
    assert "belum ada komentar" in client.messages.calls[0]["messages"][0]["content"]
