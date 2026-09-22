"""Tests for the explained blast radius.

Prompt construction is pure and tested directly against the diamond fixture from
`test_impact`, which is reused rather than rebuilt: the explanation is a reading
of exactly that radius, and a second fixture could only drift from it.

Everything runs against the stub chat provider, so nothing here needs a key or
makes a billable call.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Repository, SourceFile, Symbol
from app.services import impact_explain
from app.services.chat import ChatContext, focus_radius
from app.services.graph_writer import delete_repo_graph, write_parsed_repo
from app.services.impact import impact
from app.services.impact_explain import (
    SYSTEM_PROMPT,
    ImpactExplanation,
    _build_prompt,
    _short,
    _symbol_details,
    explain_impact,
)
from app.services.providers.base import ChatResponse
from app.services.providers.stub import StubChatProvider
from tests.test_impact import CLI, CONFIG, SERVICE, _diamond_repo, _key, _settings_key


@pytest.fixture
def graphed(repository: Repository):
    """The diamond from `test_impact`, written to the graph.

    The *builder* is imported and the fixture redeclared rather than importing
    the fixture itself: an imported fixture name collides with every parameter
    of the same name, and moving the original would mean editing a suite this
    change has no other reason to touch.
    """
    write_parsed_repo(repository.id, _diamond_repo())
    try:
        yield repository
    finally:
        delete_repo_graph(repository.id)


@pytest.fixture(autouse=True)
def clear_cache():
    """The explanation cache is module state; a leak between tests would make one
    test's assertion depend on another having run."""
    impact_explain._cache.clear()
    yield
    impact_explain._cache.clear()


@pytest.fixture
def symbols(db: Session, graphed: Repository) -> None:
    """Postgres rows matching the graph fixture — the signatures and docstrings
    the graph deliberately does not store."""
    config = SourceFile(repository_id=graphed.id, path=CONFIG, module="app.core.config")
    config.symbols = [
        Symbol(
            repository_id=graphed.id,
            name="get_settings",
            qualname="get_settings",
            kind="function",
            module="app.core.config",
            line_start=49,
            line_end=50,
            docstring="Cached application settings.\n\nBuilt once per process.",
            returns="Settings",
            parameters=[],
        )
    ]
    service = SourceFile(repository_id=graphed.id, path=SERVICE, module="app.service")
    service.symbols = [
        Symbol(
            repository_id=graphed.id,
            name="run",
            qualname="run",
            kind="function",
            module="app.service",
            line_start=12,
            line_end=20,
            parameters=[{"name": "payload", "annotation": "dict"}],
            returns="None",
        )
    ]
    db.add_all([config, service])
    db.commit()


def _radius(repo: Repository):
    return impact(repo.id, _settings_key(repo))


# --- the prompt --------------------------------------------------------------


def test_the_system_prompt_forbids_inventing_behaviour():
    assert "Do not describe behaviour you cannot see" in SYSTEM_PROMPT
    assert "path:line" in SYSTEM_PROMPT


def test_the_system_prompt_asks_for_confidence_to_be_honoured():
    """A guessed edge presented as fact is the failure mode worth preventing."""
    assert "low-confidence" in SYSTEM_PROMPT
    assert "Distinguish what will break from what might" in SYSTEM_PROMPT


def test_prompt_carries_routes_and_confidences(db: Session, graphed: Repository):
    radius = _radius(graphed)
    prompt = _build_prompt(radius, radius.items, {})

    assert "The symbol being changed:" in prompt
    assert "route:" in prompt
    assert "confidence" in prompt
    for name in ("run", "handler", "tick"):
        assert name in prompt


def test_prompt_carries_signatures_and_docstrings(
    db: Session, graphed: Repository, symbols: None
):
    radius = _radius(graphed)
    details = _symbol_details(db, str(graphed.id), [radius.root, *radius.items])
    prompt = _build_prompt(radius, radius.items, details)

    assert "def get_settings() -> Settings" in prompt
    assert "doc: Cached application settings." in prompt
    # Only the docstring's first line — the rest crowds out the routes.
    assert "Built once per process" not in prompt
    assert f"{CONFIG}:49" in prompt


def test_prompt_survives_symbols_with_no_postgres_row(db: Session, graphed: Repository):
    """A node the explainer cannot enrich must still be described, not dropped."""
    radius = _radius(graphed)
    prompt = _build_prompt(radius, radius.items, {})

    assert "The symbol being changed:" in prompt
    for name in ("run", "handler", "tick"):
        assert name in prompt


def test_route_keys_are_shortened_for_the_prompt():
    assert _short("sym:1234-abcd:app.core.config:get_settings") == "get_settings"
    assert _short("plain") == "plain"


# --- the explanation ---------------------------------------------------------


def test_explanation_reports_what_it_was_derived_from(
    db: Session, graphed: Repository, symbols: None
):
    result = explain_impact(
        db, graphed.id, _settings_key(graphed), provider=StubChatProvider()
    )

    assert isinstance(result, ImpactExplanation)
    assert result.text
    assert result.affected == 3
    assert result.explained == 3
    assert result.model == "stub-chat"
    assert not result.cached


def test_an_empty_radius_never_calls_the_model(db: Session, graphed: Repository):
    """Nothing depending on a symbol is a fact. Asking a model to narrate it
    invites invented risk, and costs a call to get it."""

    class Exploding(StubChatProvider):
        def complete(self, *args, **kwargs):
            raise AssertionError("the model must not be called for an empty radius")

    result = explain_impact(
        db, graphed.id, _key(graphed, "app.cli", "handler", CLI), provider=Exploding()
    )

    assert result.affected == 0
    assert result.model == ""
    assert "contained" in result.text
    # The caveat matters: an empty radius is not proof a change is safe.
    assert "dynamic dispatch" in result.text


class _Counting(StubChatProvider):
    """Counts calls so a cache hit is provable rather than assumed."""

    def __init__(self, calls: list[int]) -> None:
        super().__init__()
        self.calls = calls

    def complete(self, messages, **kwargs):
        self.calls.append(1)
        return ChatResponse(text="explained", model="stub-chat", stop_reason="end_turn")


def test_a_second_call_is_served_from_the_cache(db: Session, graphed: Repository):
    calls: list[int] = []
    key = _settings_key(graphed)

    first = explain_impact(db, graphed.id, key, commit_sha="abc", provider=_Counting(calls))
    second = explain_impact(db, graphed.id, key, commit_sha="abc", provider=_Counting(calls))

    assert len(calls) == 1
    assert not first.cached
    assert second.cached
    assert second.text == first.text


def test_a_new_commit_invalidates_the_cache(db: Session, graphed: Repository):
    """The graph moves on re-parse, so an explanation of the old graph is wrong."""
    calls: list[int] = []
    key = _settings_key(graphed)

    explain_impact(db, graphed.id, key, commit_sha="abc", provider=_Counting(calls))
    explain_impact(db, graphed.id, key, commit_sha="def", provider=_Counting(calls))

    assert len(calls) == 2


def test_a_refusal_is_reported_rather_than_cached(db: Session, graphed: Repository):
    class Refusing(StubChatProvider):
        def complete(self, messages, **kwargs):
            return ChatResponse(text="", model="stub-chat", stop_reason="refusal")

    result = explain_impact(
        db, graphed.id, _settings_key(graphed), provider=Refusing()
    )

    assert result.refused
    assert "refused" in result.text
    assert not impact_explain._cache


# --- the endpoint ------------------------------------------------------------


def test_explain_endpoint_returns_prose_and_counts(
    client: TestClient, graphed: Repository, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(impact_explain, "get_chat_provider", StubChatProvider)

    response = client.get(
        f"/repos/{graphed.id}/impact/explain", params={"key": _settings_key(graphed)}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["affected"] == 3
    assert body["explained"] == 3
    assert body["depth"] == 3
    assert body["text"]


def test_explain_endpoint_404s_for_an_unknown_node(
    client: TestClient, graphed: Repository, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(impact_explain, "get_chat_provider", StubChatProvider)

    response = client.get(
        f"/repos/{graphed.id}/impact/explain", params={"key": "sym:nope:nope:nope"}
    )

    assert response.status_code == 404


def test_explain_endpoint_404s_for_an_unknown_repository(client: TestClient):
    response = client.get("/repos/00000000-0000-0000-0000-000000000000/impact/explain?key=x")
    assert response.status_code == 404


# --- the chat wiring ---------------------------------------------------------


def test_focus_radius_is_a_prompt_block(graphed: Repository):
    block = focus_radius(graphed.id, _settings_key(graphed))

    assert block is not None
    assert block.startswith("Blast radius of")
    assert "confidence" in block
    assert "route:" in block


def test_focus_radius_states_an_empty_radius_plainly(graphed: Repository):
    block = focus_radius(graphed.id, _key(graphed, "app.cli", "handler", CLI))

    assert block is not None
    assert "nothing in this repository reaches it" in block


def test_an_unknown_focus_key_degrades_instead_of_failing(graphed: Repository):
    """The key comes from a click, so a stale one must not lose the answer."""
    assert focus_radius(graphed.id, "sym:gone:gone:gone") is None


def test_the_system_prompt_tells_chat_what_a_radius_block_is():
    from app.services.chat import SYSTEM_PROMPT as CHAT_PROMPT

    assert "Blast radius" in CHAT_PROMPT
    assert "complete within its stated depth" in CHAT_PROMPT


def test_a_focus_key_puts_the_radius_in_the_prompt(
    db: Session, graphed: Repository, monkeypatch: pytest.MonkeyPatch
):
    """The whole point of the wiring: the model sees the graph, not just search
    hits it would have to infer callers from."""
    from app.models import Conversation
    from app.services.chat import stream_answer

    monkeypatch.setattr(
        "app.services.chat.retrieve_context", lambda *a, **k: ChatContext([])
    )
    conversation = Conversation(repository_id=graphed.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    sent: list[str] = []

    class Capturing(StubChatProvider):
        def stream(self, messages, **kwargs):
            sent.append(messages[-1].content)
            yield "ok"

    try:
        _, stream, _ = stream_answer(
            db,
            conversation,
            "what breaks if I change this?",
            provider=Capturing(),
            focus_key=_settings_key(graphed),
        )
        list(stream)

        assert sent, "the provider was never called"
        assert "Blast radius of" in sent[0]
        assert "route:" in sent[0]
    finally:
        db.delete(conversation)
        db.commit()


def test_no_focus_key_leaves_the_prompt_alone(
    db: Session, graphed: Repository, monkeypatch: pytest.MonkeyPatch
):
    from app.models import Conversation
    from app.services.chat import stream_answer

    monkeypatch.setattr(
        "app.services.chat.retrieve_context", lambda *a, **k: ChatContext([])
    )
    conversation = Conversation(repository_id=graphed.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    sent: list[str] = []

    class Capturing(StubChatProvider):
        def stream(self, messages, **kwargs):
            sent.append(messages[-1].content)
            yield "ok"

    try:
        _, stream, _ = stream_answer(db, conversation, "how does this work?", provider=Capturing())
        list(stream)

        assert "Blast radius" not in sent[0]
    finally:
        db.delete(conversation)
        db.commit()
