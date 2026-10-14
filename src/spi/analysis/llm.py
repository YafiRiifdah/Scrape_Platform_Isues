"""Abstraksi penyedia LLM untuk tahap analisis.

- `AnthropicProvider`: Claude lewat SDK resmi (structured outputs, prompt caching, fallback).
- `OpenAICompatibleProvider`: endpoint berformat OpenAI `chat/completions` (Gemini, Groq,
  OpenRouter, Ollama, dll.). Mode JSON diminta lewat `response_format`, skema disisipkan ke
  system prompt, hasil divalidasi Pydantic dengan satu kali perbaikan bila tidak valid.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

import requests
from pydantic import BaseModel, ValidationError

from spi.config import AnalysisConfig

log = logging.getLogger("spi.analysis.llm")

# harga Anthropic per 1 juta token (input, output), USD; untuk estimasi saja
ANTHROPIC_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """Kegagalan permanen dari penyedia LLM (bukan rate limit)."""


@dataclass
class LLMResult:
    text: str = ""
    parsed: Any = None
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    refusal: Optional[str] = None  # terisi bila model menolak / output tidak valid


class Provider(Protocol):
    name: str

    def structured(self, *, model: str, system: str, user: str, schema: type[BaseModel], max_tokens: int) -> LLMResult: ...

    def generate(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult: ...

    def count_tokens(self, *, model: str, system: str, user: str) -> Optional[int]: ...

    def cost_usd(self, model: str, tokens_in: int, tokens_out: int) -> Optional[float]: ...


# ---------------------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------------------
class AnthropicProvider:
    name = "anthropic"
    MAX_RETRIES = 5

    def __init__(self, client: Any, sleep: Callable[[float], None] = time.sleep, server_side_fallback: bool = True):
        self.client = client
        self._sleep = sleep
        self.server_side_fallback = server_side_fallback

    @staticmethod
    def _system_blocks(system: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _with_retry(self, fn: Callable[[], Any]) -> Any:
        import anthropic

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return fn()
            except anthropic.RateLimitError as e:
                wait = float(e.response.headers.get("retry-after", "30") or 30)
                log.warning("rate limit Claude, menunggu %.0f detik", wait)
                self._sleep(wait)
            except anthropic.APIStatusError as e:
                if e.status_code >= 500 and attempt < self.MAX_RETRIES:
                    log.warning("server error %s, coba lagi", e.status_code)
                    self._sleep(5.0)
                    continue
                raise LLMError(f"API {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"koneksi: {e}") from e
        raise LLMError("gagal setelah beberapa kali rate limit")

    def structured(self, *, model: str, system: str, user: str, schema: type[BaseModel], max_tokens: int) -> LLMResult:
        response = self._with_retry(
            lambda: self.client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                output_config={"effort": "low"},
                system=self._system_blocks(system),
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        )
        usage = response.usage
        result = LLMResult(tokens_in=usage.input_tokens or 0, tokens_out=usage.output_tokens or 0, model=model)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            detail = getattr(getattr(response, "stop_details", None), "category", None)
            result.refusal = f"refusal ({detail})" if response.stop_reason == "refusal" else "output kosong"
            return result
        result.parsed = response.parsed_output
        return result

    def generate(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            output_config={"effort": "high"},
            system=self._system_blocks(system),
            messages=[{"role": "user", "content": user}],
        )
        if self.server_side_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        def run():
            with self.client.beta.messages.stream(**kwargs) as stream:
                return stream.get_final_message()

        message = self._with_retry(run)
        usage = message.usage
        result = LLMResult(tokens_in=usage.input_tokens or 0, tokens_out=usage.output_tokens or 0, model=getattr(message, "model", model))
        if message.stop_reason == "refusal":
            result.refusal = getattr(getattr(message, "stop_details", None), "explanation", "") or "refusal"
            return result
        if message.stop_reason == "max_tokens":
            log.warning("output terpotong karena max_tokens")
        result.text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text").strip()
        return result

    def count_tokens(self, *, model: str, system: str, user: str) -> Optional[int]:
        count = self.client.messages.count_tokens(
            model=model, system=self._system_blocks(system), messages=[{"role": "user", "content": user}]
        )
        return int(count.input_tokens)

    def cost_usd(self, model: str, tokens_in: int, tokens_out: int) -> Optional[float]:
        price_in, price_out = ANTHROPIC_PRICES_PER_MTOK.get(model, ANTHROPIC_PRICES_PER_MTOK["claude-opus-5-5"])
        return tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out


# ---------------------------------------------------------------------------------------
# OpenAI-compatible (Gemini, Groq, OpenRouter, Ollama, ...)
# ---------------------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = _FENCE_RE.sub("", text).strip()
    # kalau masih ada teks di luar objek JSON, ambil dari { pertama sampai } terakhir
    if not text.startswith("{") and "{" in text and "}" in text:
        text = text[text.index("{") : text.rindex("}") + 1]
    return text


class OpenAICompatibleProvider:
    name = "openai_compatible"
    MAX_RATE_LIMIT_RETRIES = 5
    SERVER_ERROR_RETRIES = 2

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        http_post: Callable[..., Any] = requests.post,
        sleep: Callable[[float], None] = time.sleep,
        request_delay: float = 0.0,
        timeout: float = 180.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http_post = http_post
        self._sleep = sleep
        self.request_delay = request_delay
        self.timeout = timeout
        self._calls_made = 0

    # ---- HTTP -----------------------------------------------------------------
    def _chat(self, model: str, messages: list[dict[str, str]], max_tokens: int, json_mode: bool) -> tuple[str, int, int, str]:
        if self.request_delay > 0 and self._calls_made > 0:
            self._sleep(self.request_delay)
        self._calls_made += 1

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        rate_limits = server_errors = 0
        while True:
            try:
                resp = self._http_post(url, headers=headers, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                raise LLMError(f"koneksi ke {self.base_url} gagal: {e}") from e
            status = resp.status_code
            if status == 429:
                rate_limits += 1
                if rate_limits > self.MAX_RATE_LIMIT_RETRIES:
                    raise LLMError("rate limit terus-menerus; perbesar analysis.request_delay_seconds")
                wait = float((getattr(resp, "headers", {}) or {}).get("retry-after", "20") or 20)
                log.warning("rate limit dari %s, menunggu %.0f detik", self.base_url, wait)
                self._sleep(wait)
                continue
            if status >= 500:
                server_errors += 1
                if server_errors > self.SERVER_ERROR_RETRIES:
                    raise LLMError(f"HTTP {status} dari {self.base_url}: {resp.text[:200]}")
                self._sleep(3.0)
                continue
            if status >= 400:
                raise LLMError(f"HTTP {status}: {self._error_message(resp)}")
            break

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"respons tidak dikenal: {str(data)[:200]}") from e
        usage = data.get("usage") or {}
        return content, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), str(data.get("model") or model)

    @staticmethod
    def _error_message(resp: Any) -> str:
        try:
            err = resp.json().get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if err:
                return str(err)
        except Exception:  # noqa: BLE001
            pass
        return str(getattr(resp, "text", ""))[:200]

    # ---- API ------------------------------------------------------------------
    def structured(self, *, model: str, system: str, user: str, schema: type[BaseModel], max_tokens: int) -> LLMResult:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system_full = (
            f"{system}\n\nBalas HANYA dengan satu objek JSON valid yang memenuhi JSON Schema berikut, "
            f"tanpa teks lain dan tanpa blok kode:\n{schema_json}"
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system_full}, {"role": "user", "content": user}]
        result = LLMResult(model=model)
        last_error = ""
        for attempt in range(2):
            content, t_in, t_out, used_model = self._chat(model, messages, max_tokens, json_mode=True)
            result.tokens_in += t_in
            result.tokens_out += t_out
            result.model = used_model
            try:
                result.parsed = schema.model_validate(json.loads(_strip_fences(content)))
                result.text = content
                return result
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = str(e).splitlines()[0][:300]
                log.warning("output JSON tidak valid dari %s (percobaan %d): %s", model, attempt + 1, last_error)
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"JSON tidak valid: {last_error}. Kirim ulang HANYA objek JSON yang memenuhi skema (field wajib: {', '.join(schema.model_fields)})."},
                ]
        result.refusal = f"output tidak valid setelah 2 percobaan: {last_error}"
        return result

    def generate(self, *, model: str, system: str, user: str, max_tokens: int) -> LLMResult:
        content, t_in, t_out, used_model = self._chat(
            model, [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens, json_mode=False
        )
        return LLMResult(text=content.strip(), tokens_in=t_in, tokens_out=t_out, model=used_model)

    def count_tokens(self, *, model: str, system: str, user: str) -> Optional[int]:
        return (len(system) + len(user)) // 4  # perkiraan kasar, endpoint ini tidak punya count_tokens

    def cost_usd(self, model: str, tokens_in: int, tokens_out: int) -> Optional[float]:
        return None  # harga tergantung penyedia; tidak diperkirakan


# ---------------------------------------------------------------------------------------
def make_provider(cfg: AnalysisConfig, env: dict[str, str], sleep: Callable[[float], None] = time.sleep) -> Provider:
    if cfg.provider == "anthropic":
        import anthropic

        return AnthropicProvider(anthropic.Anthropic(), sleep=sleep, server_side_fallback=cfg.server_side_fallback)
    if not cfg.base_url:
        raise ValueError("analysis.base_url wajib diisi untuk provider openai_compatible (contoh: https://generativelanguage.googleapis.com/v1beta/openai/)")
    api_key = env.get(cfg.api_key_env) or None
    if not api_key and "localhost" not in cfg.base_url and "127.0.0.1" not in cfg.base_url:
        log.warning("%s kosong; permintaan ke %s dikirim tanpa API key", cfg.api_key_env, cfg.base_url)
    return OpenAICompatibleProvider(cfg.base_url, api_key, sleep=sleep, request_delay=cfg.request_delay_seconds)
