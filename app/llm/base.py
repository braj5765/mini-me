"""Pluggable LLM layer. Swap models by changing LLM_PROVIDER / LLM_MODEL (Fly secrets), no code change.

Privacy guard (5.14 point 6): third-party message text may only go to a provider whose terms are
marked `no_training_verified=True` in PROVIDERS. Free tiers usually are NOT verified, so they can be
used for own / made-up text only. Flip the flag only after you have read the provider's current terms.
"""
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings


class LLMError(RuntimeError):
    pass


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ProviderInfo:
    name: str
    no_training_verified: bool
    note: str


# Verified = you have read the CURRENT terms and confirmed API inputs are not used for training.
PROVIDERS: dict[str, ProviderInfo] = {
    "gemini": ProviderInfo("gemini", False, "Free AI Studio tier may use inputs to improve products. Own/made-up text only."),
    "anthropic": ProviderInfo("anthropic", False, "Paid API. Set True after you confirm current commercial terms."),
    "openai_compat": ProviderInfo("openai_compat", False, "Groq/OpenRouter/Ollama etc. Verify per endpoint; local Ollama is safe."),
}


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, messages: list[Message], *, max_tokens: int = 600,
                 temperature: float = 0.4, contains_third_party_text: bool = False) -> str: ...


class _Base:
    provider = ""

    def __init__(self, model: str):
        self.model = model

    def _guard(self, contains_third_party_text: bool) -> None:
        info = PROVIDERS[self.provider]
        if contains_third_party_text and not info.no_training_verified:
            raise LLMError(
                f"Provider '{self.provider}' is not marked no-training-verified; refusing to send "
                "third-party message text. Use own/made-up text, or verify terms and set the flag."
            )

    def complete(self, messages, *, max_tokens=600, temperature=0.4, contains_third_party_text=False) -> str:
        self._guard(contains_third_party_text)
        return self._call(messages, max_tokens, temperature)

    def _call(self, messages, max_tokens, temperature) -> str:  # pragma: no cover
        raise NotImplementedError


def _post(url: str, headers: dict, json: dict) -> dict:
    try:
        r = httpx.post(url, headers=headers, json=json, timeout=60)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise LLMError(f"LLM request failed: {type(e).__name__}") from None  # never echo key/URL


class GeminiClient(_Base):
    provider = "gemini"

    def _call(self, messages, max_tokens, temperature) -> str:
        s = get_settings()
        if not s.gemini_api_key:
            raise LLMError("GEMINI_API_KEY not set")
        system = "\n".join(m.content for m in messages if m.role == "system")
        contents = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in messages if m.role != "system"
        ]
        body = {"contents": contents,
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        data = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            {"x-goog-api-key": s.gemini_api_key}, body,
        )
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise LLMError("Unexpected Gemini response shape") from None


class AnthropicClient(_Base):
    provider = "anthropic"

    def _call(self, messages, max_tokens, temperature) -> str:
        s = get_settings()
        if not s.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY not set")
        system = "\n".join(m.content for m in messages if m.role == "system")
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]}
        if system:
            body["system"] = system
        data = _post("https://api.anthropic.com/v1/messages",
                     {"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01"}, body)
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError):
            raise LLMError("Unexpected Anthropic response shape") from None


class OpenAICompatClient(_Base):
    provider = "openai_compat"

    def _call(self, messages, max_tokens, temperature) -> str:
        s = get_settings()
        if not s.openai_compat_base_url:
            raise LLMError("OPENAI_COMPAT_BASE_URL not set")
        headers = {"Authorization": f"Bearer {s.openai_compat_api_key}"} if s.openai_compat_api_key else {}
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": m.role, "content": m.content} for m in messages]}
        data = _post(s.openai_compat_base_url.rstrip("/") + "/chat/completions", headers, body)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise LLMError("Unexpected response shape") from None


_ADAPTERS = {"gemini": GeminiClient, "anthropic": AnthropicClient, "openai_compat": OpenAICompatClient}


def get_llm(provider: str | None = None, model: str | None = None) -> LLMClient:
    s = get_settings()
    provider, model = provider or s.llm_provider, model or s.llm_model
    if not provider or not model:
        raise LLMError("LLM_PROVIDER / LLM_MODEL not set. (Not needed until drafting, task D8a.)")
    if provider not in _ADAPTERS:
        raise LLMError(f"Unknown provider '{provider}'. Options: {sorted(_ADAPTERS)}")
    return _ADAPTERS[provider](model)
