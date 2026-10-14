"""Test penyedia LLM: OpenAI-compatible (Gemini/Groq/OpenRouter/Ollama) dan pembungkus Anthropic."""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from spi.analysis.llm import AnthropicProvider, LLMError, LLMResult, OpenAICompatibleProvider, make_provider
from spi.config import AnalysisConfig


class Mini(BaseModel):
    topic: str
    score: int


class FakeHttp:
    """Mengembalikan respons berurutan dari `script`; tiap item: dict payload | (status, text)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, tuple):
            status, text = item
            return SimpleNamespace(status_code=status, text=text, headers={"retry-after": "3"}, json=lambda: json.loads(text) if text else {})
        return SimpleNamespace(status_code=200, text=json.dumps(item), headers={}, json=lambda: item)


def chat_response(content, prompt_tokens=100, completion_tokens=20, model="gemini-2.5-flash"):
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def make_openai(script, base_url="https://example.com/v1/", api_key="k", delay=0.0):
    http = FakeHttp(script)
    slept = []
    p = OpenAICompatibleProvider(base_url=base_url, api_key=api_key, http_post=http, sleep=slept.append, request_delay=delay)
    return p, http, slept


def test_structured_sends_json_mode_and_parses_schema():
    p, http, _ = make_openai([chat_response('```json\n{"topic": "beras", "score": 7}\n```')])

    r = p.structured(model="gemini-2.5-flash", system="SYS", user="USER", schema=Mini, max_tokens=512)

    assert isinstance(r, LLMResult)
    assert r.parsed == Mini(topic="beras", score=7)
    assert (r.tokens_in, r.tokens_out) == (100, 20)
    url, kw = http.calls[0]
    assert url == "https://example.com/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer k"
    body = kw["json"]
    assert body["model"] == "gemini-2.5-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system" and "SYS" in body["messages"][0]["content"]
    assert "topic" in body["messages"][0]["content"]  # skema JSON disisipkan ke system prompt
    assert body["messages"][1] == {"role": "user", "content": "USER"}
    assert body["max_tokens"] == 512


def test_structured_retries_once_with_validation_feedback_then_fails():
    p, http, _ = make_openai([chat_response('{"topic": "beras"}'), chat_response("bukan json")])

    r = p.structured(model="m", system="S", user="U", schema=Mini, max_tokens=100)

    assert r.parsed is None and r.refusal is not None
    assert len(http.calls) == 2
    retry_msgs = http.calls[1][1]["json"]["messages"]
    assert retry_msgs[-1]["role"] == "user" and "score" in retry_msgs[-1]["content"]


def test_structured_recovers_on_retry():
    p, http, _ = make_openai([chat_response("oops"), chat_response('{"topic": "x", "score": 1}')])
    r = p.structured(model="m", system="S", user="U", schema=Mini, max_tokens=100)
    assert r.parsed == Mini(topic="x", score=1)
    assert r.tokens_in == 200  # usage kedua percobaan dijumlahkan


def test_rate_limit_waits_retry_after_then_succeeds():
    p, http, slept = make_openai([(429, '{"error": {"message": "quota"}}'), chat_response('{"topic": "x", "score": 1}')])
    r = p.structured(model="m", system="S", user="U", schema=Mini, max_tokens=100)
    assert r.parsed is not None
    assert slept == [3.0]


def test_server_error_retried_then_raises_llm_error():
    p, http, _ = make_openai([(500, "boom"), (503, "down"), (502, "bad")])
    with pytest.raises(LLMError, match="HTTP 502"):
        p.generate(model="m", system="S", user="U", max_tokens=100)
    assert len(http.calls) == 3


def test_client_error_raises_immediately():
    p, http, _ = make_openai([(400, '{"error": {"message": "model tidak ada"}}')])
    with pytest.raises(LLMError, match="model tidak ada"):
        p.generate(model="m", system="S", user="U", max_tokens=100)
    assert len(http.calls) == 1


def test_generate_returns_text_and_model():
    p, _, _ = make_openai([chat_response("# Laporan\n\nisi", model="llama-3.3-70b")])
    r = p.generate(model="llama-3.3-70b", system="S", user="U", max_tokens=100)
    assert r.text.startswith("# Laporan") and r.model == "llama-3.3-70b"


def test_request_delay_paces_free_tier_calls():
    p, _, slept = make_openai([chat_response("a"), chat_response("b")], delay=4.0)
    p.generate(model="m", system="S", user="U", max_tokens=10)
    p.generate(model="m", system="S", user="U", max_tokens=10)
    assert slept == [4.0]  # jeda hanya di antara dua panggilan


def test_no_api_key_allowed_for_local_endpoint():
    p, http, _ = make_openai([chat_response("ok")], base_url="http://localhost:11434/v1", api_key=None)
    p.generate(model="llama3", system="S", user="U", max_tokens=10)
    assert "Authorization" not in http.calls[0][1]["headers"]


def test_count_tokens_is_estimated_and_cost_unknown():
    p, _, _ = make_openai([])
    assert p.count_tokens(model="m", system="a" * 400, user="b" * 400) == 200
    assert p.cost_usd("m", 1000, 1000) is None


# ---- factory -----------------------------------------------------------------------
def test_make_provider_openai_compatible_reads_key_from_configured_env():
    cfg = AnalysisConfig(provider="openai_compatible", base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_KEY")
    p = make_provider(cfg, {"GROQ_KEY": "abc"})
    assert isinstance(p, OpenAICompatibleProvider) and p.api_key == "abc"


def test_make_provider_openai_compatible_requires_base_url():
    cfg = AnalysisConfig(provider="openai_compatible")
    with pytest.raises(ValueError, match="base_url"):
        make_provider(cfg, {})


def test_make_provider_anthropic_default(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda: SimpleNamespace(name="fake"))
    p = make_provider(AnalysisConfig(), {})
    assert isinstance(p, AnthropicProvider)


# ---- AnthropicProvider -------------------------------------------------------------------
def test_anthropic_provider_structured_uses_parse_and_reports_refusal():
    calls = []

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(parsed_output=None, stop_reason="refusal", stop_details=SimpleNamespace(category="x"), usage=SimpleNamespace(input_tokens=5, output_tokens=0))

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    p = AnthropicProvider(client, sleep=lambda s: None)
    r = p.structured(model="claude-haiku-5-5", system="S", user="U", schema=Mini, max_tokens=100)
    assert r.parsed is None and "refusal" in r.refusal
    assert calls[0]["output_format"] is Mini and calls[0]["output_config"] == {"effort": "low"}
    assert calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_provider_cost_table():
    p = AnthropicProvider(SimpleNamespace(), sleep=lambda s: None)
    assert p.cost_usd("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert p.cost_usd("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.0)
