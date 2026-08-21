"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  ApiError,
  graphApi,
  impactApi,
  riskApi,
  type GraphResponse,
  type ImpactExplanation,
  type ImpactResponse,
  type Repository,
  type RiskBand,
} from "@/lib/api";
import type { ColorMode, ImpactOverlay } from "@/components/GraphCanvas";
import {
  applyView,
  GROUP_PREFIX,
  isGroup,
  NODE_TYPES,
  projectImpact,
  type NodeType,
} from "@/lib/graphView";

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

const RISK_LEGEND: Array<[string, string]> = [
  ["low", "#3fb950"],
  ["moderate", "#d29922"],
  ["high", "#f0883e"],
  ["critical", "#f85149"],
  ["unscored", "#484f58"],
];

/** Blast radius depth. Past three hops the radius is usually the whole repo. */
const IMPACT_DEPTH = 3;

export default function GraphPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [view, setView] = useState<View>("files");
  const [limit, setLimit] = useState(400);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [types, setTypes] = useState<Set<NodeType>>(() => new Set(NODE_TYPES));
  const [collapse, setCollapse] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [colorMode, setColorMode] = useState<ColorMode>("type");

  const [risk, setRisk] = useState<Map<string, { score: number; band: RiskBand }>>(
    () => new Map(),
  );
  const [impact, setImpact] = useState<ImpactResponse | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);
  const [explanation, setExplanation] = useState<ImpactExplanation | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [explainError, setExplainError] = useState<string | null>(null);

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

  // Risk is fetched once, lazily, the first time the user asks to see it — it is
  // an expensive repository-wide ranking and most visits never switch mode.
  useEffect(() => {
    if (colorMode !== "risk" || risk.size > 0) return;
    let cancelled = false;
    void riskApi
      .rank(id, undefined, 200)
      .then((data) => {
        if (cancelled) return;
        setRisk(new Map(data.items.map((i) => [i.key, { score: i.score, band: i.band }])));
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof ApiError ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [colorMode, id, risk.size]);

  const viewData = useMemo(
    () =>
      applyView(graph?.nodes ?? [], graph?.edges ?? [], { types, collapse, expanded }),
    [graph, types, collapse, expanded],
  );

  const node = useMemo(
    () => viewData.nodes.find((n) => n.key === selected) ?? null,
    [viewData, selected],
  );

  // Selecting a real node loads its blast radius. A collapsed group stands for
  // many nodes with no single origin for a change, so it is inspected, not traced.
  useEffect(() => {
    // The explanation costs a model call, so it is never carried over from a
    // previous selection — a stale paragraph about a different symbol is worse
    // than an empty panel.
    setExplanation(null);
    setExplainError(null);

    if (!selected || selected.startsWith(GROUP_PREFIX)) {
      setImpact(null);
      return;
    }
    let cancelled = false;
    setImpactLoading(true);
    void impactApi
      .get(id, selected, IMPACT_DEPTH)
      .then((data) => {
        if (!cancelled) setImpact(data);
      })
      .catch(() => {
        // A node with no dependents, or one the impact service cannot score,
        // is a normal outcome — it just means nothing lights up.
        if (!cancelled) setImpact(null);
      })
      .finally(() => {
        if (!cancelled) setImpactLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, selected]);

  const overlay = useMemo<ImpactOverlay | null>(() => {
    if (!impact || !selected) return null;
    const { hopsByKey, routeEdges } = projectImpact(impact.items, viewData.representative);
    if (hopsByKey.size === 0) return null;
    return {
      rootKey: viewData.representative.get(selected) ?? selected,
      hopsByKey,
      routeEdges,
    };
  }, [impact, selected, viewData.representative]);

  const onSelect = useCallback((key: string | null) => setSelected(key), []);

  // Explicit rather than automatic: every explanation is a model call, and
  // clicking around a graph would otherwise bill one per node.
  async function explain() {
    if (!selected) return;
    setExplaining(true);
    setExplainError(null);
    try {
      setExplanation(await impactApi.explain(id, selected, IMPACT_DEPTH));
    } catch (e) {
      // The ranked list is still valid — only the prose is unavailable, which
      // is what the 503 from this endpoint says.
      setExplainError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setExplaining(false);
    }
  }

  function toggleType(type: NodeType) {
    setTypes((current) => {
      const next = new Set(current);
      // Never let the last type be switched off — an empty canvas reads as a
      // bug rather than as a filter.
      if (next.has(type) && next.size > 1) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  function toggleGroup(group: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(group)) next.delete(group);
      else next.add(group);
      return next;
    });
  }

  const legend = colorMode === "risk" ? RISK_LEGEND : LEGEND;
  const groupNode = node && isGroup(node) ? node : null;

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
          <label className="muted" style={{ fontSize: 12 }}>
            Colour{" "}
            <select
              value={colorMode}
              onChange={(e) => setColorMode(e.target.value as ColorMode)}
            >
              <option value="type">by type</option>
              <option value="risk">by risk</option>
            </select>
          </label>
          <label className="muted" style={{ fontSize: 12 }}>
            <input
              type="checkbox"
              checked={collapse}
              onChange={(e) => setCollapse(e.target.checked)}
            />{" "}
            Collapse by module
          </label>
          <span className="muted" style={{ fontSize: 12 }}>
            {graph
              ? `${viewData.nodes.length} nodes · ${viewData.edges.length} edges`
              : "…"}
          </span>
        </div>

        <div className="graph-controls" style={{ marginBottom: 0 }}>
          <span className="muted" style={{ fontSize: 11, letterSpacing: "0.05em" }}>
            SHOW
          </span>
          {NODE_TYPES.map((type) => (
            <label key={type} className="muted" style={{ fontSize: 12 }}>
              <input
                type="checkbox"
                checked={types.has(type)}
                onChange={() => toggleType(type)}
              />{" "}
              {type}
            </label>
          ))}
        </div>

        <p style={{ marginBottom: 0, marginTop: 12 }}>
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

      {(graph?.truncated || viewData.hiddenByFilter > 0) && (
        <section className="panel">
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            {graph?.truncated && (
              <>
                The {limit}-node cap hid part of this repository — raise it or switch
                view to see a different slice.{" "}
              </>
            )}
            {viewData.hiddenByFilter > 0 && (
              <>
                {viewData.hiddenByFilter} more{" "}
                {graph?.truncated ? "of the fetched nodes are" : "nodes"} hidden by the
                type filter.
              </>
            )}
          </p>
        </section>
      )}

      <div className="split">
        <section className="panel">
          <div className="legend">
            {legend.map(([label, color]) => (
              <span key={label} className="legend-item">
                <i style={{ background: color }} /> {label}
              </span>
            ))}
            {overlay && (
              <span className="legend-item">
                <i style={{ background: "#f0883e" }} /> blast radius
              </span>
            )}
          </div>
          {loading ? (
            <p className="empty">Laying out the graph…</p>
          ) : viewData.nodes.length ? (
            <GraphCanvas
              nodes={viewData.nodes}
              edges={viewData.edges}
              selected={selected}
              onSelect={onSelect}
              colorMode={colorMode}
              riskByKey={risk}
              impact={overlay}
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
            <>
              <dl className="detail">
                <dt>Name</dt>
                <dd className="mono">{node.display}</dd>
                <dt>Type</dt>
                <dd>
                  {groupNode
                    ? `collapsed module · ${groupNode.size} nodes`
                    : `${node.type}${node.kind ? ` · ${node.kind}` : ""}${
                        node.is_external ? " · external" : ""
                      }`}
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
                {node.module && !groupNode && (
                  <>
                    <dt>Module</dt>
                    <dd className="mono">{node.module}</dd>
                  </>
                )}
                {colorMode === "risk" && !groupNode && (
                  <>
                    <dt>Risk</dt>
                    <dd>
                      {risk.has(node.key) ? (
                        <>
                          {risk.get(node.key)!.score.toFixed(1)}{" "}
                          <span className="muted">({risk.get(node.key)!.band})</span>
                        </>
                      ) : (
                        <span className="muted">
                          not in the top 200 — unscored, not safe
                        </span>
                      )}
                    </dd>
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

              {groupNode && (
                <p style={{ marginBottom: 0 }}>
                  <button
                    className="secondary"
                    onClick={() => toggleGroup(groupNode.module ?? groupNode.display)}
                  >
                    Expand this module
                  </button>
                </p>
              )}

              {!groupNode && (
                <div style={{ marginTop: 16 }}>
                  <h2>Blast radius</h2>
                  {impactLoading ? (
                    <p className="empty">Tracing dependents…</p>
                  ) : impact && impact.items.length ? (
                    <>
                      <ul className="stats">
                        <li>
                          <strong>{impact.summary.total}</strong>affected
                        </li>
                        <li>
                          <strong>{impact.summary.direct}</strong>direct
                        </li>
                        <li>
                          <strong>{impact.summary.max_hops}</strong>max hops
                        </li>
                      </ul>

                      <div className="explain">
                        {explanation ? (
                          <>
                            <p className="explain-text">{explanation.text}</p>
                            <p className="explain-meta">
                              {explanation.explained < explanation.affected
                                ? `Read from the ${explanation.explained} highest-scoring of ${explanation.affected} affected nodes`
                                : `Read from all ${explanation.affected} affected nodes`}
                              {explanation.model && ` · ${explanation.model}`}
                              {explanation.cached && " · cached"}
                            </p>
                          </>
                        ) : (
                          <button
                            className="secondary"
                            onClick={() => void explain()}
                            disabled={explaining}
                          >
                            {explaining ? "Explaining…" : "Explain this in English"}
                          </button>
                        )}
                        {explainError && (
                          <p className="error" style={{ fontSize: 12 }}>
                            {explainError}
                          </p>
                        )}
                        <p style={{ margin: "8px 0 0", fontSize: 12 }}>
                          <Link
                            href={`/repos/${id}/chat?focus=${encodeURIComponent(selected!)}&q=${encodeURIComponent(
                              `What breaks if I change ${node.display}?`,
                            )}`}
                          >
                            Ask follow-ups in chat →
                          </Link>
                        </p>
                      </div>

                      <div className="scroll" style={{ maxHeight: 240, marginTop: 10 }}>
                        <table>
                          <thead>
                            <tr>
                              <th>Node</th>
                              <th className="num">Hops</th>
                              <th className="num">Score</th>
                            </tr>
                          </thead>
                          <tbody>
                            {impact.items.slice(0, 40).map((item) => {
                              // The radius is computed over the whole graph, so
                              // it can name nodes the capped slice never fetched.
                              // Those rows still inform; they just cannot be
                              // selected, and saying so beats a dead click.
                              const onCanvas = viewData.representative.has(item.key);
                              return (
                                <tr
                                  key={item.key}
                                  className={onCanvas ? "clickable" : undefined}
                                  onClick={
                                    onCanvas ? () => setSelected(item.key) : undefined
                                  }
                                  title={onCanvas ? item.key : "Not in the current view"}
                                >
                                  <td className={onCanvas ? "mono" : "mono muted"}>
                                    {item.display}
                                  </td>
                                  <td className="num">{item.hops}</td>
                                  <td className="num">{item.score.toFixed(2)}</td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                      {impact.items.length > 40 && (
                        <p className="empty">
                          Showing the 40 highest-scoring of {impact.total}.
                        </p>
                      )}
                      {impact.truncated && (
                        <p className="empty">
                          The radius hit the result cap — there may be more beyond it.
                        </p>
                      )}
                    </>
                  ) : (
                    <p className="empty">
                      Nothing depends on this node within {IMPACT_DEPTH} hops.
                    </p>
                  )}
                </div>
              )}
            </>
          ) : (
            <p className="empty">Click a node to light up what depends on it.</p>
          )}
        </section>
      </div>
    </>
  );
}
