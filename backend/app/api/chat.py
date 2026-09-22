"""Chat endpoint. Streams a grounded answer as Server-Sent Events."""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.ratelimit import require_chat_quota
from app.models import ChatMessage, Conversation, Repository
from app.schemas.repository import Page
from app.services.chat import stream_answer
from app.services.providers import ProviderError

router = APIRouter(prefix="/repos/{repository_id}", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: uuid.UUID | None = Field(
        default=None, description="omit to start a new conversation"
    )
    focus_key: str | None = Field(
        default=None,
        description=(
            "Graph node key to pull a blast radius into the context. Set by the "
            "graph panel when the user asks about a selected node; there is no "
            "intent detection on the message itself"
        ),
    )


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[dict] = []
    model: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    messages: list[MessageOut] = []

    model_config = {"from_attributes": True}


class ConversationSummaryOut(BaseModel):
    """A conversation without its turns.

    The list endpoint used to return `ConversationOut`, which carries every
    message. With lazy loading that is one query per conversation — 51 for a page
    of 50 — and it ships every message body and citation payload to render what
    is only ever a picker. The count and the last-activity timestamp are what a
    list actually needs, and both come from one aggregate query.
    """

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/chat", dependencies=[Depends(require_chat_quota)])
def chat(
    repository_id: uuid.UUID,
    payload: ChatRequest,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Ask a question. Streams SSE: `context`, then `delta`s, then `done`."""
    if db.get(Repository, repository_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No repository {repository_id}")

    conversation = _conversation(db, repository_id, payload.conversation_id)

    # The generator outlives the request-scoped session that FastAPI closes when
    # this function returns, so the streaming work gets a session of its own.
    session = SessionLocal()
    try:
        conversation = session.get(Conversation, conversation.id)
        context, stream, answer = stream_answer(
            session, conversation, payload.message, focus_key=payload.focus_key
        )
    except ProviderError as exc:
        session.close()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except Exception:
        session.close()
        raise

    def events():
        try:
            # Citations first: the UI can render the sources before any text
            # arrives, and the user can see what the answer will be based on.
            yield _sse(
                "context",
                {
                    "conversation_id": str(conversation.id),
                    "message_id": str(answer.id),
                    "citations": context.as_citations(),
                },
            )
            for piece in stream:
                yield _sse("delta", {"text": piece})
            yield _sse("done", {"message_id": str(answer.id)})
        except Exception as exc:  # noqa: BLE001 - the stream is already open
            yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}"})
        finally:
            session.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations", response_model=Page[ConversationSummaryOut])
def list_conversations(
    repository_id: uuid.UUID,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ConversationSummaryOut]:
    """Conversations for a repository, newest first, without their turns."""
    conditions = [Conversation.repository_id == repository_id]
    total = db.scalar(select(func.count()).select_from(Conversation).where(*conditions)) or 0

    rows = db.execute(
        select(Conversation, func.count(ChatMessage.id).label("message_count"))
        .outerjoin(ChatMessage, ChatMessage.conversation_id == Conversation.id)
        .where(*conditions)
        .group_by(Conversation.id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return Page[ConversationSummaryOut](
        items=[
            ConversationSummaryOut(
                id=conversation.id,
                title=conversation.title,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                message_count=message_count,
            )
            for conversation, message_count in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    repository_id: uuid.UUID, conversation_id: uuid.UUID, db: Session = Depends(get_db)
):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.repository_id != repository_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No conversation {conversation_id}")
    return conversation


def _conversation(
    db: Session, repository_id: uuid.UUID, conversation_id: uuid.UUID | None
) -> Conversation:
    if conversation_id is None:
        conversation = Conversation(repository_id=repository_id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.repository_id != repository_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No conversation {conversation_id}")
    return conversation


__all__ = ["ChatMessage", "router"]
