"""Provider selection.

The day's acceptance criterion is that swapping either provider is one env var,
which is what this module is: two registries, two lookups, nothing else in the
codebase names a vendor.

Construction is lazy and cached. Lazy because a missing `ANTHROPIC_API_KEY`
must not stop the API from starting — Weeks 1-2 work perfectly well without one
— and cached because building a client per request would discard its connection
pool every time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.services.providers.base import (
    ChatMessage,
    ChatProvider,
    ChatResponse,
    Effort,
    EmbeddingBatch,
    EmbeddingProvider,
    ProviderError,
)
from app.services.providers.stub import HashEmbeddingProvider, StubChatProvider

logger = logging.getLogger(__name__)

__all__ = [
    "ChatMessage",
    "ChatProvider",
    "ChatResponse",
    "Effort",
    "EmbeddingBatch",
    "EmbeddingProvider",
    "ProviderError",
    "chat_provider_names",
    "embedding_provider_names",
    "get_chat_provider",
    "get_embedding_provider",
    "reset_providers",
]


def _anthropic(settings: Settings) -> ChatProvider:
    # Imported inside the factory so the `anthropic` package is only required
    # when it is actually the configured provider.
    from app.services.providers.anthropic_chat import AnthropicChatProvider

    return AnthropicChatProvider(settings)


def _openai_embeddings(settings: Settings) -> EmbeddingProvider:
    from app.services.providers.openai_embeddings import OpenAIEmbeddingProvider

    return OpenAIEmbeddingProvider(settings)


CHAT_PROVIDERS: dict[str, Callable[[Settings], ChatProvider]] = {
    "anthropic": _anthropic,
    "stub": StubChatProvider,
}

EMBEDDING_PROVIDERS: dict[str, Callable[[Settings], EmbeddingProvider]] = {
    "openai": _openai_embeddings,
    "hash": HashEmbeddingProvider,
}


def chat_provider_names() -> list[str]:
    return sorted(CHAT_PROVIDERS)


def embedding_provider_names() -> list[str]:
    return sorted(EMBEDDING_PROVIDERS)


@lru_cache
def get_chat_provider() -> ChatProvider:
    """The provider named by `LLM_PROVIDER`."""
    settings = get_settings()
    factory = CHAT_PROVIDERS.get(settings.llm_provider)
    if factory is None:
        raise ProviderError(
            settings.llm_provider,
            f"unknown LLM_PROVIDER. Available: {', '.join(chat_provider_names())}",
        )
    provider = factory(settings)
    logger.info("Chat provider: %s (%s)", provider.name, provider.model)
    return provider


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    """The provider named by `EMBEDDING_PROVIDER`.

    Independent of `LLM_PROVIDER` on purpose: Anthropic has no embeddings
    endpoint, so `LLM_PROVIDER=anthropic` still needs one of these set.
    """
    settings = get_settings()
    factory = EMBEDDING_PROVIDERS.get(settings.embedding_provider)
    if factory is None:
        raise ProviderError(
            settings.embedding_provider,
            f"unknown EMBEDDING_PROVIDER. Available: {', '.join(embedding_provider_names())}",
        )
    provider = factory(settings)
    logger.info(
        "Embedding provider: %s (%s, %d dimensions)",
        provider.name,
        provider.model,
        provider.dimensions,
    )
    return provider


def reset_providers() -> None:
    """Drop the cached providers. For tests that change the configured names."""
    get_chat_provider.cache_clear()
    get_embedding_provider.cache_clear()
