"""Chat tests.

Prompt assembly is pure and tested directly. The endpoint runs against the stub
chat provider, so the suite exercises streaming, persistence and citations
without a key or a billable call.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Conversation, MessageRole, Repository
from app.services.chat import SYSTEM_PROMPT, ChatContext, build_messages, stream_answer
from app.services.providers.stub import StubChatProvider
from app.services.retrieval import SearchHit


def _hit(qualname="Session.request", source="vector", line=644) -> SearchHit:
    return SearchHit(
        score=0.5,
        path="src/requests/sessions.py",
        kind="method",
        line_start=line,
        line_end=line + 20,
        text=f"File: src/requests/sessions.py\n\ndef {qualname.split('.')[-1]}(self): ...",
        qualname=qualname,
        symbol_key=f"sym:x:requests.sessions:{qualname}",
        source=source,
    )


# --- prompt assembly ---------------------------------------------------------


def test_context_blocks_carry_a_citable_location():
    prompt = ChatContext([_hit()]).as_prompt()
    assert "src/requests/sessions.py:644" in prompt
    assert "method Session.request" in prompt


def test_graph_reached_excerpts_are_labelled_as_such():
    """So the model can use them as machinery without answering about them."""
    prompt = ChatContext([_hit(source="graph")]).as_prompt()
    assert "(via call graph)" in prompt
    assert "(via call graph)" not in ChatContext([_hit()]).as_prompt()


def test_an_empty_context_says_so_rather_than_being_blank():
    assert "No excerpts" in ChatContext([]).as_prompt()


def test_the_system_prompt_demands_citations_and_forbids_guessing():
    assert "path:line" in SYSTEM_PROMPT
    assert "Do not fill the gap from general knowledge" in SYSTEM_PROMPT


def test_excerpts_go_in_the_user_turn_not_the_system_prompt():
    """The system prompt must stay byte-identical to remain cacheable."""
    messages = build_messages("how does it work", ChatContext([_hit()]), [])
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert "sessions.py:644" in messages[0].content
    assert messages[0].content.endswith("Question: how does it work")


def test_history_precedes_the_question(db: Session, repository: Repository):
    conversation = Conversation(repository_id=repository.id)
    db.add(conversation)
    db.commit()

    from app.models import ChatMessage as Turn

    history = [
        Turn(conversation_id=conversation.id, role=MessageRole.USER, content="first"),
        Turn(conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="reply"),
    ]
    messages = build_messages("second", ChatContext([]), history)
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert messages[0].content == "first"


def test_citations_record_where_each_excerpt_came_from():
    citations = ChatContext([_hit(), _hit(qualname="Session.send", source="graph")]).as_citations()
    assert {c["source"] for c in citations} == {"vector", "graph"}
    assert citations[0]["citation"] == "src/requests/sessions.py:644"


# --- the streaming turn ------------------------------------------------------


@pytest.fixture
def conversation(db: Session, repository: Repository) -> Conversation:
    conversation = Conversation(repository_id=repository.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def test_a_turn_persists_both_messages_and_the_answer(
    db: Session, conversation: Conversation, monkeypatch
):
    monkeypatch.setattr(
        "app.services.chat.retrieve_context", lambda *a, **k: ChatContext([_hit()])
    )
    _, stream, answer = stream_answer(
        db, conversation, "how does a session send", provider=StubChatProvider()
    )
    text = "".join(stream)

    db.refresh(conversation)
    roles = [m.role for m in conversation.messages]
    assert roles == [MessageRole.USER, MessageRole.ASSISTANT]
    assert conversation.messages[0].content == "how does a session send"
    assert conversation.messages[1].content == text
    assert text


def test_the_answer_keeps_the_citations_it_was_grounded_in(
    db: Session, conversation: Conversation, monkeypatch
):
    """An answer without its context cannot be audited afterwards."""
    monkeypatch.setattr(
        "app.services.chat.retrieve_context", lambda *a, **k: ChatContext([_hit()])
    )
    _, stream, answer = stream_answer(db, conversation, "q", provider=StubChatProvider())
    list(stream)

    db.refresh(answer)
    assert answer.citations[0]["citation"] == "src/requests/sessions.py:644"
    assert answer.model == "stub-chat"


def test_a_partial_stream_still_saves_what_arrived(
    db: Session, conversation: Conversation, monkeypatch
):
    """A disconnected client must not lose the turn entirely."""
    monkeypatch.setattr("app.services.chat.retrieve_context", lambda *a, **k: ChatContext([]))
    _, stream, answer = stream_answer(db, conversation, "q", provider=StubChatProvider())

    next(stream)
    stream.close()

    db.refresh(answer)
    assert answer.content


def test_the_first_question_becomes_the_conversation_title(
    db: Session, conversation: Conversation, monkeypatch
):
    monkeypatch.setattr("app.services.chat.retrieve_context", lambda *a, **k: ChatContext([]))
    _, stream, _ = stream_answer(
        db, conversation, "where are redirects followed", provider=StubChatProvider()
    )
    list(stream)

    db.refresh(conversation)
    assert conversation.title == "where are redirects followed"


# --- the endpoint ------------------------------------------------------------


def test_chat_streams_context_then_deltas_then_done(
    client: TestClient, repository: Repository, monkeypatch
):
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    from app.core.config import get_settings
    from app.services.providers import reset_providers

    get_settings.cache_clear()
    reset_providers()
    monkeypatch.setattr(
        "app.services.chat.retrieve_context", lambda *a, **k: ChatContext([_hit()])
    )
    try:
        response = client.post(
            f"/repos/{repository.id}/chat", json={"message": "how does a session send"}
        )
        assert response.status_code == 200
        body = response.text

        assert "event: context" in body
        assert "event: delta" in body
        assert "event: done" in body
        # Citations arrive before any answer text, so a UI can render sources first.
        assert body.index("event: context") < body.index("event: delta")
        assert "sessions.py:644" in body
    finally:
        get_settings.cache_clear()
        reset_providers()


def test_chat_404s_for_an_unknown_repository(client: TestClient):
    response = client.post(
        "/repos/00000000-0000-0000-0000-000000000000/chat", json={"message": "hi"}
    )
    assert response.status_code == 404


def test_an_empty_message_is_rejected(client: TestClient, repository: Repository):
    assert client.post(f"/repos/{repository.id}/chat", json={"message": ""}).status_code == 422


def test_conversations_are_listed_and_fetchable(
    client: TestClient, db: Session, repository: Repository
):
    conversation = Conversation(repository_id=repository.id, title="t")
    db.add(conversation)
    db.commit()

    # A page, not a bare list. Iterating the envelope yields its keys, so the
    # assertion this replaces raised a TypeError the first time it ever ran.
    listed = client.get(f"/repos/{repository.id}/conversations").json()
    assert any(c["id"] == str(conversation.id) for c in listed["items"])

    fetched = client.get(f"/repos/{repository.id}/conversations/{conversation.id}").json()
    assert fetched["title"] == "t"


def test_a_conversation_from_another_repository_is_not_visible(
    client: TestClient, db: Session, repository: Repository
):
    other = Repository(name="other")
    db.add(other)
    db.commit()
    conversation = Conversation(repository_id=other.id)
    db.add(conversation)
    db.commit()
    try:
        response = client.get(f"/repos/{repository.id}/conversations/{conversation.id}")
        assert response.status_code == 404
    finally:
        db.delete(other)
        db.commit()
