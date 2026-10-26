"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { use, useCallback, useEffect, useRef, useState } from "react";

import RepoNav from "@/components/RepoNav";
import {
  api,
  ApiError,
  type ChatMessage,
  type Citation,
  type Repository,
} from "@/lib/api";

/** A chip linking to the file view, scrolled to the cited line. */
function CitationChip({ repoId, citation }: { repoId: string; citation: Citation }) {
  const label = citation.qualname ?? citation.path.split("/").pop();
  return (
    <Link
      className={`chip ${citation.source}`}
      href={`/repos/${repoId}/files?path=${encodeURIComponent(citation.path)}`}
      title={
        `${citation.citation}\n${citation.kind} ${citation.qualname ?? ""}\n` +
        (citation.source === "graph"
          ? "reached by following calls from a direct match"
          : "matched the question directly")
      }
    >
      <span className="mono">{label}</span>
      <span className="muted"> {citation.citation.split(":").pop()}</span>
    </Link>
  );
}

function Message({ repoId, message }: { repoId: string; message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={`turn ${message.role}`}>
      <div className="turn-role">{isUser ? "You" : "ARGUS"}</div>
      <div className="turn-body">
        {message.content || <span className="muted">…</span>}
      </div>
      {!isUser && message.citations.length > 0 && (
        <div className="chips">
          <span className="muted" style={{ fontSize: 11 }}>
            Grounded in:
          </span>
          {message.citations.map((c) => (
            <CitationChip key={`${c.path}:${c.line_start}`} repoId={repoId} citation={c} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function ChatPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const search = useSearchParams();

  useEffect(() => {
    void api
      .getRepository(id)
      .then(setRepo)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [id]);

  // The graph panel hands a node over in the URL rather than guessing intent
  // from the question text. `q` only seeds the box; the user still sends it.
  useEffect(() => {
    const focus = search.get("focus");
    if (focus) setFocusKey(focus);
    const question = search.get("q");
    if (question) setInput(question);
  }, [search]);

  // Follow the answer as it streams in.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      const question = input.trim();
      if (!question || streaming) return;

      setInput("");
      setError(null);
      setStreaming(true);
      setMessages((prev) => [
        ...prev,
        {
          id: `local-${Date.now()}`,
          role: "user",
          content: question,
          citations: [],
          model: null,
          created_at: new Date().toISOString(),
        },
        {
          id: `pending-${Date.now()}`,
          role: "assistant",
          content: "",
          citations: [],
          model: null,
          created_at: new Date().toISOString(),
        },
      ]);

      // Both handlers below update the last message in place — the assistant
      // turn is already rendered, so deltas append rather than re-render a list.
      const updateLast = (patch: Partial<ChatMessage>) =>
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          next[next.length - 1] = { ...last, ...patch };
          return next;
        });

      try {
        await api.chat(
          id,
          question,
          conversationId,
          {
            onContext: (newConversationId, citations) => {
              setConversationId(newConversationId);
              updateLast({ citations });
            },
            onDelta: (text) =>
              setMessages((prev) => {
                const next = [...prev];
                const last = next[next.length - 1];
                next[next.length - 1] = { ...last, content: last.content + text };
                return next;
              }),
            onError: (detail) => setError(detail),
          },
          undefined,
          focusKey,
        );
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      } finally {
        setStreaming(false);
      }
    },
    [id, input, conversationId, streaming, focusKey],
  );

  return (
    <>
      <section className="panel">
        <h2>Ask about {repo?.name ?? "this repository"}</h2>
        <p className="muted" style={{ marginBottom: 8, fontSize: 12 }}>
          Answers are grounded in retrieved code. Every claim carries a citation
          you can click through to the source.
        </p>
        {focusKey && (
          <p className="focus-note">
            Focused on <span className="mono">{focusKey.split(":").pop()}</span> — its
            blast radius from the dependency graph goes into the context, alongside the
            retrieved code.{" "}
            <button
              className="secondary"
              style={{ padding: "1px 8px", fontSize: 11 }}
              onClick={() => setFocusKey(null)}
            >
              clear
            </button>
          </p>
        )}
        <RepoNav id={id} active="chat" />
      </section>

      {error && (
        <section className="panel">
          <p className="error">{error}</p>
        </section>
      )}

      <section className="panel">
        <div className="chat-log">
          {messages.length === 0 && (
            <p className="empty">
              Ask something like “how does a session send a request?”
            </p>
          )}
          {messages.map((m) => (
            <Message key={m.id} repoId={id} message={m} />
          ))}
          <div ref={bottom} />
        </div>

        <form onSubmit={send} className="chat-form">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask a question about this codebase…"
            aria-label="Question"
            disabled={streaming}
          />
          <button type="submit" disabled={streaming || !input.trim()}>
            {streaming ? "Answering…" : "Ask"}
          </button>
        </form>
      </section>
    </>
  );
}
