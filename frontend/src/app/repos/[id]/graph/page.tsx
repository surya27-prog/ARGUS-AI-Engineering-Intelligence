"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  ApiError,
  graphApi,
  type GraphResponse,
  type Repository,
} from "@/lib/api";

// Cytoscape measures its container on construction, so it cannot be
// server-rendered — there is no DOM to measure.
const GraphCanvas = dynamic(() => import("@/components/GraphCanvas"), {
  ssr: false,
  loading: () => <p className="empty">Loading graph…</p>,
});

type View = "files" | "calls";

const LEGEND: Array<[string, string]> = [
  ["File", "#58a6ff"],
  ["Class", "#bc8cff"],
  ["Function", "#3fb950"],
  ["Module", "#8b949e"],
  ["Repo", "#d29922"],
];

export default function GraphPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [view, setView] = useState<View>("files");
  const [limit, setLimit] = useState(400);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void api.getRepository(id).then(setRepo).catch(() => undefined);
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSelected(null);
    void graphApi
      .get(id, view, limit)
      .then((data) => {
        if (!cancelled) {
          setGraph(data);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, view, limit]);

  const node = useMemo(
    () => graph?.nodes.find((n) => n.key === selected) ?? null,
    [graph, selected],
  );

  const onSelect = useCallback((key: string | null) => setSelected(key), []);

  return (
    <>
      <section className="panel">
        <h2>{repo?.name ?? "Repository"} graph</h2>
        <div className="graph-controls">
          <div>
            {(["files", "calls"] as View[]).map((v) => (
              <button
                key={v}
                className={view === v ? "" : "secondary"}
                onClick={() => setView(v)}
              >
                {v === "files" ? "Files & imports" : "Calls"}
              </button>
            ))}
          </div>
          <label className="muted" style={{ fontSize: 12 }}>
            Max nodes{" "}
            <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
              {[200, 400, 800, 1500].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <span className="muted" style={{ fontSize: 12 }}>
            {graph ? `${graph.nodes.length} nodes · ${graph.edges.length} edges` : "…"}
          </span>
        </div>
        <p style={{ marginBottom: 0 }}>
          <Link href={`/repos/${id}`}>← Files and symbols</Link>
          {" · "}
          <Link href={`/repos/${id}/chat`}>Ask about this codebase →</Link>
        </p>
      </section>

      {error && (
        <section className="panel">
          <p className="error">{error}</p>
        </section>
      )}

      {graph?.truncated && (
        <section className="panel">
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            Showing the first {graph.nodes.length} nodes — this repository has more.
            Raise the cap or switch view to see a different slice.
          </p>
        </section>
      )}

      <div className="split">
        <section className="panel">
          <div className="legend">
            {LEGEND.map(([label, color]) => (
              <span key={label} className="legend-item">
                <i style={{ background: color }} /> {label}
              </span>
            ))}
          </div>
          {loading ? (
            <p className="empty">Laying out the graph…</p>
          ) : graph && graph.nodes.length ? (
            <GraphCanvas
              nodes={graph.nodes}
              edges={graph.edges}
              selected={selected}
              onSelect={onSelect}
            />
          ) : (
            <p className="empty">
              Nothing to draw. Has this repository finished parsing?
            </p>
          )}
        </section>

        <section className="panel">
          <h2>Selection</h2>
          {node ? (
            <dl className="detail">
              <dt>Name</dt>
              <dd className="mono">{node.display}</dd>
              <dt>Type</dt>
              <dd>
                {node.type}
                {node.kind ? ` · ${node.kind}` : ""}
                {node.is_external ? " · external" : ""}
              </dd>
              {node.path && (
                <>
                  <dt>File</dt>
                  <dd className="mono">
                    <Link href={`/repos/${id}?path=${encodeURIComponent(node.path)}`}>
                      {node.path}
                      {node.line_start ? `:${node.line_start}` : ""}
                    </Link>
                  </dd>
                </>
              )}
              {node.module && (
                <>
                  <dt>Module</dt>
                  <dd className="mono">{node.module}</dd>
                </>
              )}
              {node.unresolved_calls != null && (
                <>
                  <dt>Unresolved calls</dt>
                  <dd>{node.unresolved_calls}</dd>
                </>
              )}
              {node.change_count != null && (
                <>
                  <dt>Commits touching it</dt>
                  <dd>{node.change_count}</dd>
                </>
              )}
            </dl>
          ) : (
            <p className="empty">Click a node to inspect it.</p>
          )}
        </section>
      </div>
    </>
  );
}
