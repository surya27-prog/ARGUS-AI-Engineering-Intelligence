"""Deterministic providers that never leave the process.

These are not toys. Weeks 3-5 build chunking, retrieval, prompt assembly and
citation rendering on top of the provider interfaces, and almost none of that
logic needs a real model to be tested — it needs *a* model that returns the same
thing twice. Without these, every test of that code costs money, needs a key in
CI, and fails when a vendor has an outage.

They are selected the same way as any other provider (`LLM_PROVIDER=stub`),
which also makes them the demonstration that the swap really is one env var.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator, Sequence
from typing import ClassVar

from app.core.config import Settings
from app.services.providers.base import (
    ChatMessage,
    ChatProvider,
    ChatResponse,
    Effort,
    EmbeddingBatch,
    EmbeddingProvider,
)

_WORD = re.compile(r"\w+")


class StubChatProvider(ChatProvider):
    """Echoes back what it was asked, with the token counts it implies."""

    name: ClassVar[str] = "stub"

    def __init__(self, settings: Settings | None = None) -> None:
        self._model = "stub-chat"

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
        text = "".join(self.stream(messages, system=system))
        return ChatResponse(
            text=text,
            model=self._model,
            input_tokens=self.count_tokens(messages, system=system),
            output_tokens=len(_WORD.findall(text)),
            stop_reason="end_turn",
        )

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
    ) -> Iterator[str]:
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        # Chunked rather than yielded whole, so consumers that assemble a
        # stream are exercised the way a real one would exercise them.
        for word in f"stub answer to: {last}".split(" "):
            yield word + " "

    def count_tokens(
        self, messages: Sequence[ChatMessage], *, system: str | None = None
    ) -> int:
        text = " ".join([system or "", *(m.content for m in messages)])
        return len(_WORD.findall(text))

    def check(self) -> str:
        return "up"


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic vectors from a hash of the text.

    Not semantic — nothing here understands language. What it does give is the
    two properties retrieval code actually needs to be testable: the same text
    always produces the same vector, and different text produces a different
    one. Similarity scores are meaningless, so tests assert on *plumbing*
    (dimensions, ordering, batching, collection wiring), never on rank quality.
    """

    name: ClassVar[str] = "hash"

    def __init__(self, settings: Settings | None = None) -> None:
        self._dimensions = settings.embedding_dimensions if settings else 1536

    @property
    def model(self) -> str:
        return "hash-embed"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            vectors=[self._vector(text) for text in texts],
            model=self.model,
            dimensions=self._dimensions,
            input_tokens=sum(len(_WORD.findall(t)) for t in texts),
        )

    def _vector(self, text: str) -> list[float]:
        # A hash stream stretched to the configured width, then normalised to
        # unit length so cosine distance behaves the way Qdrant expects.
        raw = bytearray()
        counter = 0
        seed = text.encode("utf-8")
        while len(raw) < self._dimensions * 2:
            raw += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            counter += 1

        values = [
            int.from_bytes(raw[i * 2 : i * 2 + 2], "big") / 32768.0 - 1.0
            for i in range(self._dimensions)
        ]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    def check(self) -> str:
        return "up"
