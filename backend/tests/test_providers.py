"""Provider interface and selection tests.

Everything here runs offline. That is the point of the stub providers: the
interfaces have to be testable without a key, or every test of Week 3's
retrieval and chat code would cost money and fail during a vendor outage.

The Anthropic and OpenAI implementations are tested for the shape of the
request they build and how they translate errors — not by calling the APIs.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.providers import (
    CHAT_PROVIDERS,
    EMBEDDING_PROVIDERS,
    ChatMessage,
    ProviderError,
    chat_provider_names,
    embedding_provider_names,
    get_chat_provider,
    get_embedding_provider,
    reset_providers,
)
from app.services.providers.stub import HashEmbeddingProvider, StubChatProvider


@pytest.fixture(autouse=True)
def clean_provider_cache():
    reset_providers()
    yield
    reset_providers()


def _settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "embedding_provider": "hash",
        "embedding_dimensions": 8,
    }
    return Settings(**(base | overrides))


# --- selection ---------------------------------------------------------------


def test_the_configured_chat_provider_is_the_one_returned(monkeypatch):
    """The day's criterion: swapping the provider is one env var."""
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert get_chat_provider().name == "stub"
    finally:
        get_settings.cache_clear()


def test_the_configured_embedding_provider_is_the_one_returned(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert get_embedding_provider().name == "hash"
    finally:
        get_settings.cache_clear()


def test_an_unknown_provider_names_the_ones_that_exist(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gpt5-turbo-max")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(ProviderError) as exc:
            get_chat_provider()
        assert "anthropic" in str(exc.value) and "stub" in str(exc.value)
    finally:
        get_settings.cache_clear()


def test_the_two_registries_are_independent():
    """Anthropic has no embeddings endpoint — that is why there are two."""
    assert set(chat_provider_names()) == {"anthropic", "stub"}
    assert set(embedding_provider_names()) == {"openai", "hash"}
    assert not set(CHAT_PROVIDERS) & set(EMBEDDING_PROVIDERS)


def test_providers_are_built_once_and_reused(monkeypatch):
    """A client per request would throw away its connection pool each time."""
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        assert get_chat_provider() is get_chat_provider()
        assert get_embedding_provider() is get_embedding_provider()
    finally:
        get_settings.cache_clear()


# --- the chat interface ------------------------------------------------------


def test_complete_returns_text_and_token_counts():
    provider = StubChatProvider(_settings())
    response = provider.complete([ChatMessage("user", "what does parse_repo do")])

    assert "parse_repo" in response.text
    assert response.input_tokens > 0
    assert response.output_tokens > 0
    assert not response.refused


def test_stream_yields_pieces_that_reassemble_into_the_answer():
    provider = StubChatProvider(_settings())
    messages = [ChatMessage("user", "hello")]

    streamed = "".join(provider.stream(messages))
    assert streamed == provider.complete(messages).text


def test_the_system_prompt_is_counted_but_is_not_a_turn():
    """Anthropic takes `system` as its own parameter, not a role."""
    provider = StubChatProvider(_settings())
    messages = [ChatMessage("user", "hi")]

    without = provider.count_tokens(messages)
    with_system = provider.count_tokens(messages, system="You answer about code.")
    assert with_system > without


def test_a_refusal_is_visible_rather_than_an_empty_string():
    """Anthropic returns a 200 with empty content when it declines."""
    from app.services.providers.base import ChatResponse

    refused = ChatResponse(text="", model="m", stop_reason="refusal")
    assert refused.refused
    assert not ChatResponse(text="hi", model="m", stop_reason="end_turn").refused


def test_the_chat_interface_has_no_temperature_parameter():
    """It is a 400 on current Anthropic models, so it is not on the interface."""
    import inspect

    from app.services.providers.base import ChatProvider

    signature = inspect.signature(ChatProvider.complete)
    assert "temperature" not in signature.parameters
    assert "effort" in signature.parameters


# --- the embedding interface -------------------------------------------------


def test_embed_returns_one_vector_per_input_in_order():
    provider = HashEmbeddingProvider(_settings())
    batch = provider.embed(["alpha", "beta", "gamma"])

    assert len(batch) == 3
    assert all(len(v) == 8 for v in batch.vectors)
    # Alignment is the contract: vector i belongs to text i.
    assert batch.vectors[0] == provider.embed(["alpha"]).vectors[0]


def test_the_same_text_always_embeds_to_the_same_vector():
    provider = HashEmbeddingProvider(_settings())
    assert provider.embed_one("Session.request") == provider.embed_one("Session.request")


def test_different_text_embeds_differently():
    provider = HashEmbeddingProvider(_settings())
    assert provider.embed_one("alpha") != provider.embed_one("beta")


def test_vectors_are_unit_length():
    """Qdrant's cosine distance expects normalised vectors."""
    provider = HashEmbeddingProvider(_settings())
    vector = provider.embed_one("some code")
    assert sum(v * v for v in vector) == pytest.approx(1.0, abs=1e-9)


def test_the_batch_reports_the_model_and_width_that_produced_it():
    """Mixing two embedding models in one collection is silent nonsense."""
    provider = HashEmbeddingProvider(_settings())
    batch = provider.embed(["x"])
    assert batch.dimensions == provider.dimensions == 8
    assert batch.model == provider.model


def test_embedding_an_empty_batch_is_not_an_error():
    provider = HashEmbeddingProvider(_settings())
    assert len(provider.embed([])) == 0


def test_embed_one_returns_a_bare_vector():
    provider = HashEmbeddingProvider(_settings())
    assert len(provider.embed_one("q")) == 8


# --- the real implementations, without calling them --------------------------


def test_the_anthropic_provider_refuses_to_construct_without_a_key():
    from app.services.providers.anthropic_chat import AnthropicChatProvider

    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicChatProvider(_settings(llm_provider="anthropic", anthropic_api_key=""))


def test_the_openai_provider_refuses_to_construct_without_a_key():
    from app.services.providers.openai_embeddings import OpenAIEmbeddingProvider

    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAIEmbeddingProvider(_settings(embedding_provider="openai", openai_api_key=""))


def test_a_dimension_mismatch_is_caught_at_construction():
    """Qdrant would otherwise accept the collection and reject every write."""
    from app.services.providers.openai_embeddings import OpenAIEmbeddingProvider

    with pytest.raises(ProviderError, match="EMBEDDING_DIMENSIONS"):
        OpenAIEmbeddingProvider(
            _settings(
                embedding_provider="openai",
                openai_api_key="sk-test",
                openai_embedding_model="text-embedding-3-small",  # 1536
                embedding_dimensions=768,
            )
        )


def test_the_anthropic_request_omits_sampling_parameters():
    """temperature / top_p / top_k return a 400 on current models."""
    from app.services.providers.anthropic_chat import AnthropicChatProvider

    provider = AnthropicChatProvider(
        _settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    )
    request = provider._request([ChatMessage("user", "hi")], "sys", 1000, None)

    assert not {"temperature", "top_p", "top_k"} & set(request)
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "high"
    # A list block, so a cache_control breakpoint can be added without
    # reshaping the request.
    assert request["system"] == [{"type": "text", "text": "sys"}]


def test_the_anthropic_request_omits_system_when_there_is_none():
    from app.services.providers.anthropic_chat import AnthropicChatProvider

    provider = AnthropicChatProvider(
        _settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    )
    assert "system" not in provider._request([ChatMessage("user", "hi")], None, 100, None)


def test_a_per_call_effort_overrides_the_configured_one():
    from app.services.providers.anthropic_chat import AnthropicChatProvider

    provider = AnthropicChatProvider(
        _settings(llm_provider="anthropic", anthropic_api_key="sk-test", llm_effort="high")
    )
    request = provider._request([ChatMessage("user", "hi")], None, 100, "low")
    assert request["output_config"]["effort"] == "low"


def test_sdk_errors_become_actionable_messages():
    import anthropic

    from app.services.providers.anthropic_chat import _translate

    auth = anthropic.AuthenticationError(
        "bad key", response=_fake_response(401), body=None
    )
    assert "ANTHROPIC_API_KEY was rejected" in str(_translate(auth))

    missing = anthropic.NotFoundError("nope", response=_fake_response(404), body=None)
    assert "ANTHROPIC_MODEL" in str(_translate(missing))

    # Anything unrecognised still names its type rather than vanishing.
    assert "ValueError" in str(_translate(ValueError("boom")))


def _fake_response(status: int):
    import httpx

    return httpx.Response(status_code=status, request=httpx.Request("POST", "http://x"))
