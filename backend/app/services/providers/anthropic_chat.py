"""Anthropic chat provider — the concrete `ChatProvider` behind `LLM_PROVIDER=anthropic`.

Uses the official `anthropic` SDK. Three details of the current API shape are
load-bearing here and are easy to get wrong from memory:

1. `temperature` / `top_p` / `top_k` are **removed** on Claude Opus 5 — sending
   any of them returns a 400. `output_config.effort` replaces them.
2. Thinking is **on by default** on Opus 5, and `max_tokens` caps thinking plus
   answer text together. `{"type": "adaptive"}` is passed explicitly because it
   documents the intent, not because it changes the default.
3. Token counting is a server call (`messages.count_tokens`). A local GPT
   tokenizer is wrong for Claude by 15-20% on prose and worse on code — which
   is precisely what ARGUS feeds it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

import anthropic

from app.core.config import Settings
from app.services.providers.base import (
    ChatMessage,
    ChatProvider,
    ChatResponse,
    Effort,
    ProviderError,
)

logger = logging.getLogger(__name__)

# Non-streaming requests are held open until the whole answer is built, so the
# ceiling is set by the SDK's HTTP timeout rather than the model's 128K cap.
DEFAULT_MAX_TOKENS = 16_000
# Streaming has no such constraint, so the model gets room to think and answer.
STREAM_MAX_TOKENS = 64_000


class AnthropicChatProvider(ChatProvider):
    name: ClassVar[str] = "anthropic"

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise ProviderError(self.name, "ANTHROPIC_API_KEY is not set")
        self._model = settings.anthropic_model
        self._effort: Effort = settings.llm_effort  # type: ignore[assignment]
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
        )

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
    ) -> ChatResponse:
        try:
            response = self._client.messages.create(
                **self._request(messages, system, max_tokens or DEFAULT_MAX_TOKENS, effort)
            )
        except Exception as exc:
            raise _translate(exc) from exc

        # A safety refusal is a 200 with an empty content list, so indexing
        # content[0] unconditionally would raise on a successful response.
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return ChatResponse(
            text=text,
            model=response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            stop_reason=response.stop_reason,
            cached_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
    ) -> Iterator[str]:
        request = self._request(messages, system, max_tokens or STREAM_MAX_TOKENS, effort)
        try:
            with self._client.messages.stream(**request) as stream:
                yield from stream.text_stream
        except Exception as exc:
            raise _translate(exc) from exc

    def count_tokens(
        self, messages: Sequence[ChatMessage], *, system: str | None = None
    ) -> int:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [m.as_dict() for m in messages],
        }
        if system:
            request["system"] = system
        try:
            return self._client.messages.count_tokens(**request).input_tokens
        except Exception as exc:
            raise _translate(exc) from exc

    def check(self) -> str:
        try:
            # One token against the real endpoint: cheap, and unlike a bare
            # credential check it proves the configured *model* is reachable.
            self._client.messages.count_tokens(
                model=self._model, messages=[{"role": "user", "content": "ping"}]
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the payload, not raised
            return f"down: {type(exc).__name__}"
        return "up"

    def _request(
        self,
        messages: Sequence[ChatMessage],
        system: str | None,
        max_tokens: int,
        effort: Effort | None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [m.as_dict() for m in messages],
            # Explicit rather than implicit: adaptive is the default on Opus 5,
            # but stating it means a future model change cannot silently turn
            # thinking off.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort or self._effort},
        }
        if system:
            # A list block rather than a bare string so Week 3 Day 2 can attach
            # cache_control here without reshaping the request: the system
            # prompt plus retrieved context is the stable prefix worth caching.
            request["system"] = [{"type": "text", "text": system}]
        return request


def _translate(exc: Exception) -> ProviderError:
    """Turn an SDK exception into one carrying an actionable message.

    Ordered most-specific first: a single `except APIStatusError` would lose the
    distinction between "your key is wrong" and "back off and retry".
    """
    name = AnthropicChatProvider.name
    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderError(name, "ANTHROPIC_API_KEY was rejected")
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(name, "no such model — check ANTHROPIC_MODEL")
    if isinstance(exc, anthropic.RateLimitError):
        retry = exc.response.headers.get("retry-after", "?")
        return ProviderError(name, f"rate limited, retry after {retry}s")
    if isinstance(exc, anthropic.BadRequestError):
        return ProviderError(name, f"rejected the request: {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(name, "could not reach the API")
    if isinstance(exc, anthropic.APIStatusError):
        return ProviderError(name, f"HTTP {exc.status_code}: {exc.message}")
    return ProviderError(name, f"{type(exc).__name__}: {exc}")
