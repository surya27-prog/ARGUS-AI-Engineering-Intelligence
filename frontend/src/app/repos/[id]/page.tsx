"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import {
  api,
  ApiError,
  type Repository,
  type SourceFile,
  type Symbol,
} from "@/lib/api";

const POLL_MS = 1500;

export default function RepositoryPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ path?: string }>;
}) {
  const { id } = use(params);
  // Set when a citation chip in the chat links here, so the cited file is
  // filtered to and selected instead of leaving the user to find it.
  const { path: citedPath } = use(searchParams);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [symbols, setSymbols] = useState<Symbol[]>([]);
  const [symbolTotal, setSymbolTotal] = useState(0);
  const [selected, setSelected] = useState<SourceFile | null>(null);
  const [search, setSearch] = useState(citedPath ?? "");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const current = await api.getRepository(id);
      setRepo(current);
      if (current.status === "complete") {
        setFiles((await api.listFiles(id)).items);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }, [id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const parsing = repo?.status === "pending" || repo?.status === "parsing";
  useEffect(() => {
    if (!parsing) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [parsing, refresh]);

  // Symbols are fetched per selection rather than filtered client-side: a repo
  // can hold more symbols than one page, so filtering what happens to be loaded
  // would show an empty list for files outside that window.
  const complete = repo?.status === "complete";
  const selectedId = selected?.id;
  useEffect(() => {
    if (!complete) return;
    let cancelled = false;
    void (async () => {
      try {
        const page = await api.listSymbols(id, selectedId);
        if (!cancelled) {
          setSymbols(page.items);
          setSymbolTotal(page.total);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, selectedId, complete]);

  // Arriving from a citation chip: select the cited file once its row exists,
  // so the symbol panel shows that file rather than the whole repository.
  useEffect(() => {
    if (!citedPath || selected) return;
    const match = files.find((f) => f.path === citedPath);
    if (match) setSelected(match);
  }, [citedPath, files, selected]);

  const visibleFiles = search
    ? files.filter((f) => f.path.toLowerCase().includes(search.toLowerCase()))
    : files;
  const visibleSymbols = symbols;

  if (error) {
    return (
      <section className="panel">
        <p className="error">{error}</p>
        <p>
          <Link href="/">← Back to repositories</Link>
        </p>
      </section>
    );
  }

  if (!repo) {
    return <p className="empty">Loading…</p>;
  }

  return (
    <>
      <section className="panel">
        <h2>{repo.name}</h2>
        <ul className="stats">
          <li>
            <strong>{repo.file_count}</strong>files
          </li>
          <li>
            <strong>{repo.symbol_count}</strong>symbols
          </li>
          <li>
            <strong>
              <span className={`badge ${repo.status}`}>{repo.status}</span>
            </strong>
            status
          </li>
          <li>
            <strong className="mono">{repo.commit_sha?.slice(0, 8) ?? "—"}</strong>
            {repo.default_branch ?? "commit"}
          </li>
        </ul>
        {repo.url && (
          <p className="muted mono" style={{ fontSize: 12, marginBottom: 0 }}>
            {repo.url}
          </p>
        )}
        {repo.error_message && <p className="error">{repo.error_message}</p>}
        <p style={{ marginBottom: 0 }}>
          <Link href="/">← All repositories</Link>
          {repo.status === "complete" && (
            <>
              {" · "}
              <Link href={`/repos/${id}/graph`}>Graph</Link>
              {" · "}
              <Link href={`/repos/${id}/chat`}>Ask about this codebase →</Link>
            </>
          )}
        </p>
      </section>

      {parsing && (
        <section className="panel">
          <p className="empty">Parsing… this page updates itself when it finishes.</p>
        </section>
      )}

      {repo.status === "complete" && (
        <div className="split">
          <section className="panel">
            <h2>Files ({visibleFiles.length})</h2>
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter by path…"
              aria-label="Filter files"
              style={{ width: "100%", marginBottom: 12 }}
            />
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Path</th>
                    <th className="num">Lines</th>
                    <th className="num">Symbols</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleFiles.map((file) => (
                    <tr
                      key={file.id}
                      className={`clickable ${selected?.id === file.id ? "selected" : ""}`}
                      onClick={() => setSelected(selected?.id === file.id ? null : file)}
                    >
                      <td className="mono">
                        {file.path}
                        {file.parse_error && (
                          <div style={{ color: "var(--error)", fontSize: 11 }}>
                            {file.parse_error}
                          </div>
                        )}
                      </td>
                      <td className="num">{file.line_count}</td>
                      <td className="num">{file.symbol_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {visibleFiles.length === 0 && <p className="empty">No files match.</p>}
            </div>
          </section>

          <section className="panel">
            <h2>
              Symbols ({visibleSymbols.length})
              {selected && (
                <>
                  {" — "}
                  <span className="mono" style={{ textTransform: "none" }}>
                    {selected.path}
                  </span>{" "}
                  <button
                    className="secondary"
                    style={{ padding: "2px 8px", fontSize: 11 }}
                    onClick={() => setSelected(null)}
                  >
                    clear
                  </button>
                </>
              )}
            </h2>
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="num">Line</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleSymbols.map((symbol) => (
                    <tr key={symbol.id}>
                      <td>
                        <span className="kind">{symbol.kind}</span>{" "}
                        <span className="mono">
                          {symbol.is_async && <span className="muted">async </span>}
                          {symbol.qualname}
                          {symbol.kind !== "class" && (
                            <span className="muted">
                              ({symbol.parameters.map((p) => p.name).join(", ")})
                            </span>
                          )}
                          {symbol.base_classes.length > 0 && (
                            <span className="muted">({symbol.base_classes.join(", ")})</span>
                          )}
                        </span>
                        {symbol.module && (
                          <div className="muted" style={{ fontSize: 11 }}>
                            {symbol.module}
                          </div>
                        )}
                      </td>
                      <td className="num mono">{symbol.line_start}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {visibleSymbols.length === 0 && <p className="empty">No symbols extracted.</p>}
              {!selected && symbols.length < symbolTotal && (
                <p className="empty">
                  Showing the first {symbols.length} of {symbolTotal}. Select a file to
                  narrow the list.
                </p>
              )}
            </div>
          </section>
        </div>
      )}
    </>
  );
}
