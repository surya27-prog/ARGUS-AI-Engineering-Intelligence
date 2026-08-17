"""The two provider interfaces ARGUS talks to models through.

**Two interfaces, not one.** Anthropic has no embeddings endpoint, so a single
`LLMProvider` with `complete()` and `embed()` on it would be a shape no real
provider can fill — every Anthropic implementation would have to raise on half
its own interface. Chat and embeddings are independent choices, selected by
independent env vars.

Neither interface knows about FastAPI, SQLAlchemy or Neo4j: they take strings
and return strings and floats, which is what lets Week 3's retrieval and chat
code be tested against a stub with no network at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Literal

# Effort is Anthropic's cost/quality dial. It is named here rather than in the
# Anthropic implementation because it is the *replacement* for the knob most
# provider interfaces expose — see ChatProvider below.
Effort = Literal["low", "medium", "high", "xhigh", "max"]

Role = Literal["user", "assistant"]


class ProviderError(RuntimeError):
    """A provider call failed. Carries the provider name for the error message."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn. The system prompt is passed separately, not as a role.

    Anthropic takes the system prompt as its own top-level parameter, and it is
    the part most worth caching, so keeping it out of the turn list means no
    implementation has to go hunting for it.
    """

    role: Role
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """A completed answer, plus the numbers needed to cost and debug it."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Anthropic returns `refusal` here when its safety classifiers decline the
    # request. That is an HTTP 200 with empty content, not an exception, so
    # anything reading `text` has to be able to see why it is empty.
    stop_reason: str | None = None
    cached_input_tokens: int = 0

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Vectors plus what produced them.

    `model` and `dimensions` ride along because a Qdrant collection is created
    with a fixed vector size: mixing two embedding models in one collection is
    silent nonsense, not an error, so the writer needs to be able to check.
    """

    vectors: list[list[float]] = field(default_factory=list)
    model: str = ""
    dimensions: int = 0
    input_tokens: int = 0

    def __len__(self) -> int:
        return len(self.vectors)


class ChatProvider(ABC):
    """Text in, text out.

    **There is deliberately no `temperature` parameter.** It is the knob every
    provider interface reaches for first, and on Claude Opus 5 sending
    `temperature`, `top_p` or `top_k` returns a 400 — they were removed, not
    deprecated. Putting one on this interface would mean either a dead argument
    or a provider that rejects its own contract. `effort` is the supported
    control over how hard the model works, so that is what the interface
    exposes; a provider without an equivalent maps it to whatever it has.
    """

    name: ClassVar[str]

    @property
    @abstractmethod
    def model(self) -> str:
        """The model id, for logging and for the answer's citation footer."""

    @abstractmethod
    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
    ) -> ChatResponse:
        """One request, one answer."""

    @abstractmethod
    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
    ) -> Iterator[str]:
        """Yield answer text as it arrives.

        Separate from `complete` rather than a flag on it, because the return
        types genuinely differ and a `stream: bool` that changes the shape of
        what comes back is the kind of signature callers get wrong.
        """

    @abstractmethod
    def count_tokens(
        self, messages: Sequence[ChatMessage], *, system: str | None = None
    ) -> int:
        """Count tokens the way *this* provider counts them.

        On the interface because tokenizers are provider-specific: counting
        Claude tokens with a local GPT tokenizer is wrong by 15-20% on prose and
        far worse on code, which is exactly what ARGUS embeds.
        """

    @abstractmethod
    def check(self) -> str:
        """`up`, or `down: <reason>` — shaped for /health, never raises."""


class EmbeddingProvider(ABC):
    """Text in, vectors out."""

    name: ClassVar[str]

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector width. The Qdrant collection is created with this size."""

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed a batch. Batching is the interface because per-call overhead
        dominates: embedding 2,000 chunks one HTTP request at a time is minutes
        of latency that batching removes entirely."""

    def embed_one(self, text: str) -> list[float]:
        """A single vector — the query side of retrieval."""
        batch = self.embed([text])
        if not batch.vectors:
            raise ProviderError(self.name, "embedding returned no vectors")
        return batch.vectors[0]

    @abstractmethod
    def check(self) -> str:
        """`up`, or `down: <reason>` — shaped for /health, never raises."""
