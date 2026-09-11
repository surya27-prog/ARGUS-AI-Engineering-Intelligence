"""OpenAI embedding provider — the concrete `EmbeddingProvider` behind
`EMBEDDING_PROVIDER=openai`.

This exists because Anthropic has no embeddings endpoint, which is the whole
reason chat and embeddings are configured separately. Nothing here is
Anthropic-adjacent: it is a second vendor, chosen independently, and swapping it
for Voyage or a local model is one new file plus one env var.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import ClassVar

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    RateLimitError,
)

from app.core.config import Settings
from app.services.providers.base import (
    EmbeddingBatch,
    EmbeddingProvider,
    ProviderError,
)

logger = logging.getLogger(__name__)

# Vector width per model. Declared here rather than read from config so a
# mismatched EMBEDDING_DIMENSIONS is caught as a configuration error instead of
# producing a Qdrant collection that silently rejects every write.
KNOWN_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

# The API caps inputs per request; ARGUS embeds thousands of chunks, so the
# batch is chunked rather than assumed to fit.
MAX_BATCH = 256


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name: ClassVar[str] = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ProviderError(self.name, "OPENAI_API_KEY is not set")
        self._model = settings.openai_embedding_model
        self._client = OpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout_seconds,
        )

        self._dimensions = KNOWN_DIMENSIONS.get(self._model, settings.embedding_dimensions)
        configured = settings.embedding_dimensions
        if self._model in KNOWN_DIMENSIONS and configured != self._dimensions:
            raise ProviderError(
                self.name,
                f"EMBEDDING_DIMENSIONS is {configured} but {self._model} returns "
                f"{self._dimensions}. Qdrant creates the collection with this "
                f"size, so a mismatch fails every write.",
            )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(model=self._model, dimensions=self._dimensions)

        # The API rejects an empty string, and a chunk that empty carries no
        # meaning anyway — but dropping it silently would misalign vectors with
        # the chunks they belong to, so substitute rather than skip.
        payload = [text if text.strip() else " " for text in texts]

        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(payload), MAX_BATCH):
            window = payload[start : start + MAX_BATCH]
            try:
                response = self._client.embeddings.create(model=self._model, input=window)
            except Exception as exc:
                raise _translate(exc) from exc
            # Sorted by index rather than trusted in order: the response order
            # is documented but the alignment is the whole contract here.
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
            tokens += response.usage.total_tokens

        if len(vectors) != len(texts):
            raise ProviderError(
                self.name, f"asked for {len(texts)} vectors, got {len(vectors)}"
            )
        return EmbeddingBatch(
            vectors=vectors,
            model=self._model,
            dimensions=self._dimensions,
            input_tokens=tokens,
        )

    def check(self) -> str:
        try:
            self._client.embeddings.create(model=self._model, input=["ping"])
        except Exception as exc:  # noqa: BLE001 - surfaced in the payload, not raised
            return f"down: {type(exc).__name__}"
        return "up"


def _translate(exc: Exception) -> ProviderError:
    name = OpenAIEmbeddingProvider.name
    if isinstance(exc, AuthenticationError):
        return ProviderError(name, "OPENAI_API_KEY was rejected")
    if isinstance(exc, NotFoundError):
        return ProviderError(name, "no such model — check OPENAI_EMBEDDING_MODEL")
    if isinstance(exc, RateLimitError):
        return ProviderError(name, "rate limited")
    if isinstance(exc, BadRequestError):
        return ProviderError(name, f"rejected the request: {exc}")
    if isinstance(exc, APIConnectionError):
        return ProviderError(name, "could not reach the API")
    if isinstance(exc, APIStatusError):
        return ProviderError(name, f"HTTP {exc.status_code}")
    return ProviderError(name, f"{type(exc).__name__}: {exc}")
