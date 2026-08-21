"""Grounded question answering over a repository.

Retrieve, then answer only from what was retrieved. The point of ARGUS is that
answers come from the code in front of it rather than from what the model
remembers about a library with the same name, so the prompt is built to make
ungrounded answering the awkward path: the context is labelled with exact
citations, and the instructions say what to do when the context is not enough.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import ChatMessage as ChatTurn
from app.models import Conversation, MessageRole
from app.services.graph_queries import NodeNotFound
from app.services.hybrid import hybrid_search
from app.services.impact import impact
from app.services.providers import ChatMessage, ChatProvider, get_chat_provider
from app.services.retrieval import SearchHit

logger = logging.getLogger(__name__)

# How many chunks go into the prompt. Enough to cover a question that spans a
# few symbols, small enough that the answer stays specific — a context stuffed
# with forty marginal chunks buries the two that matter.
CONTEXT_CHUNKS = 8

# Turns of history replayed. The API is stateless, so this is the whole memory;
# capped because a long thread otherwise crowds out the retrieved code.
HISTORY_TURNS = 8

# A focused question carries the blast radius of one symbol. Smaller than the
# explain endpoint's list because it shares the context window with the
# retrieved code, which is still what answers most of the question.
FOCUS_NODES = 15
FOCUS_DEPTH = 3

SYSTEM_PROMPT = """You are ARGUS, answering questions about one specific codebase.

You are given excerpts retrieved from that codebase. Answer from those excerpts \
only. They are the ground truth, even where they contradict what you know about \
a library of the same name — this is the user's code, not the public version of \
whatever it resembles.

Cite every claim about the code with the `path:line` shown in the excerpt \
header, inline, like (src/requests/sessions.py:644). A claim about the code \
with no citation is not useful, because the user cannot check it.

Some excerpts are marked `via call graph`. Those were not matched to the \
question directly — they were reached by following calls from something that \
was. They are usually the machinery an answer depends on, so use them to \
explain how something works, but do not present them as if the user asked \
about them.

When the excerpts do not answer the question, say so plainly and name what you \
would need to see. Do not fill the gap from general knowledge. A wrong answer \
about someone's own codebase is worse than no answer.

A question may arrive with a `Blast radius` block: the symbols that reach one \
particular symbol, with the route a change would travel and the confidence of \
each link. That block comes from the dependency graph, not from a search, so it \
is complete within its stated depth in a way the excerpts are not — use it for \
questions about what a change would affect, and prefer it over inferring \
callers from the excerpts. Its confidences matter: a low-confidence link is a \
link ARGUS guessed.

Be direct. Lead with the answer, then the supporting detail."""


@dataclass(frozen=True, slots=True)
class ChatContext:
    hits: list[SearchHit]

    def as_prompt(self) -> str:
        if not self.hits:
            return "No excerpts were retrieved for this question."
        blocks = []
        for hit in self.hits:
            label = hit.qualname or hit.name or hit.path
            origin = " (via call graph)" if hit.source == "graph" else ""
            blocks.append(
                f"--- {hit.citation} — {hit.kind} {label}{origin} ---\n{hit.text}"
            )
        return "\n\n".join(blocks)

    def as_citations(self) -> list[dict]:
        return [
            {
                "path": h.path,
                "line_start": h.line_start,
                "line_end": h.line_end,
                "qualname": h.qualname,
                "kind": h.kind,
                "score": round(h.score, 4),
                "source": h.source,
                "citation": h.citation,
            }
            for h in self.hits
        ]


def retrieve_context(
    repository_id: UUID | str, question: str, *, limit: int = CONTEXT_CHUNKS
) -> ChatContext:
    return ChatContext(hits=hybrid_search(repository_id, question, top_k=limit))


def build_messages(
    question: str,
    context: ChatContext,
    history: list[ChatTurn],
    *,
    radius: str | None = None,
) -> list[ChatMessage]:
    """History, then the retrieved code, then the question.

    The excerpts go in the user turn rather than the system prompt because they
    change every question: the system prompt stays byte-identical across the
    conversation, which is what lets it be cached.
    """
    messages = [
        ChatMessage(role=turn.role, content=turn.content)
        for turn in history[-HISTORY_TURNS:]
        if turn.content
    ]
    parts = [f"Excerpts from the codebase:\n\n{context.as_prompt()}"]
    if radius:
        parts.append(radius)
    parts.append(f"Question: {question}")
    messages.append(ChatMessage(role="user", content="\n\n".join(parts)))
    return messages


def focus_radius(
    repository_id: UUID | str, key: str, *, depth: int = FOCUS_DEPTH
) -> str | None:
    """The blast radius of `key` as a prompt block, or None if unavailable.

    A focus key comes from the user clicking a node, so a stale or unparsed one
    is a normal outcome rather than an error: the question is still answerable
    from the excerpts alone, so a failure here degrades the answer instead of
    losing it.
    """
    try:
        result = impact(repository_id, key, depth=depth, limit=FOCUS_NODES)
    except NodeNotFound:
        logger.info("Focus key %s is not in the graph; answering without a radius", key)
        return None
    except Exception:  # noqa: BLE001 - the chat answer matters more than the radius
        logger.exception("Could not compute the focus radius for %s", key)
        return None

    root = result.root.get("display") or key
    if not result.items:
        return (
            f"Blast radius of `{root}`: nothing in this repository reaches it "
            f"within {depth} hops."
        )

    lines = [
        f"Blast radius of `{root}` — {result.summary['total']} node(s) within "
        f"{depth} hop(s), {result.summary['direct']} of them direct"
        + (", capped" if result.truncated else "")
        + ":"
    ]
    for item in result.items:
        route = " -> ".join(k.rsplit(":", 1)[-1] for k in item["route"])
        lines.append(
            f"  {item['display']} — hop {item['hops']}, "
            f"confidence {item['confidence']:.2f}, via {' then '.join(item['via'])}"
            f"\n    route: {route}"
        )
    return "\n".join(lines)


def stream_answer(
    db: Session,
    conversation: Conversation,
    question: str,
    *,
    provider: ChatProvider | None = None,
    focus_key: str | None = None,
) -> tuple[ChatContext, Iterator[str], ChatTurn]:
    """Retrieve, persist the question, and return a stream of the answer.

    The assistant row is created empty and filled as the stream drains, so a
    disconnected client still leaves a record of what was asked and what it was
    grounded in rather than losing the turn entirely.
    """
    provider = provider or get_chat_provider()
    context = retrieve_context(conversation.repository_id, question)
    history = list(conversation.messages)

    db.add(ChatTurn(conversation_id=conversation.id, role=MessageRole.USER, content=question))
    answer = ChatTurn(
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content="",
        citations=context.as_citations(),
        model=provider.model,
    )
    db.add(answer)
    if conversation.title == "New conversation":
        conversation.title = question[:120]
    db.commit()
    db.refresh(answer)

    radius = focus_radius(conversation.repository_id, focus_key) if focus_key else None
    messages = build_messages(question, context, history, radius=radius)

    def generate() -> Iterator[str]:
        pieces: list[str] = []
        try:
            for piece in provider.stream(messages, system=SYSTEM_PROMPT):
                pieces.append(piece)
                yield piece
        finally:
            # Runs on client disconnect too, so a partial answer is still saved.
            answer.content = "".join(pieces)
            db.commit()

    return context, generate(), answer
