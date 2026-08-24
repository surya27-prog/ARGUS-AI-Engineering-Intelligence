"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import RepoNav from "@/components/RepoNav";
import RiskHeatmap from "@/components/RiskHeatmap";
import {
  api,
  ApiError,
  debtApi,
  riskApi,
  type DebtResponse,
  type Repository,
  type RiskItem,
} from "@/lib/api";

const POLL_MS = 1500;

/** Files in the heatmap. Enough to show where risk concentrates without asking
 *  the ranking for every file in a large repository. */
const HEATMAP_FILES = 120;

/** Detector labels. The API's slugs are precise but not prose. */
const DEBT_LABEL: Record<string, string> = {
  complexity: "Complex functions",
  god_file: "God files",
  circular_import: "Circular imports",
  dead_code: "Possibly dead",
  missing_docstring: "Undocumented",
};

function StatTile({
  value,
  label,
  hint,
}: {
  value: string | number;
  label: string;
  hint?: string;
}) {
  return (
    <div className="tile">
      <strong>{value}</strong>
      <span>{label}</span>
      {hint && <em>{hint}</em>}
    </div>
  );
}

/**
 * Horizontal bars for the riskiest functions.
 *
 * A bar list rather than a chart: the job is magnitude beside identity, and the
 * identities are long dotted qualnames that would be unreadable rotated under an
 * x-axis. Values are labelled directly, so there is no axis to read against.
 */
function BarList({ items }: { items: RiskItem[] }) {
  if (!items.length) return <p className="empty">Nothing scored yet.</p>;
  const max = Math.max(...items.map((i) => i.score), 1);

  return (
    <ol className="barlist">
      {items.map((item) => (
        <li key={item.key}>
          <span className="mono barlist-label" title={item.display}>
            {item.display}
          </span>
          <span className="barlist-track">
            <span
              className="barlist-fill"
              style={{ width: `${(item.score / max) * 100}%` }}
            />
          </span>
          <span className="num barlist-value">{item.score.toFixed(0)}</span>
        </li>
      ))}
    </ol>
  );
}

export default function DashboardPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [files, setFiles] = useState<RiskItem[]>([]);
  const [functions, setFunctions] = useState<RiskItem[]>([]);
  const [debt, setDebt] = useState<DebtResponse | null>(null);
  const [selected, setSelected] = useState<RiskItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const current = await api.getRepository(id);
      setRepo(current);
      setError(null);
      return current;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      return null;
    }
  }, [id]);

  useEffect(() => {
    void refresh().finally(() => setLoading(false));
  }, [refresh]);

  const parsing = repo?.status === "pending" || repo?.status === "parsing";
  useEffect(() => {
    if (!parsing) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [parsing, refresh]);

  // Three independent requests rather than one await chain: the debt scan is by
  // far the slowest, and making the risk panels wait behind it would leave the
  // whole dashboard blank for as long as it takes.
  const complete = repo?.status === "complete";
  useEffect(() => {
    if (!complete) return;
    let cancelled = false;

    void riskApi.rank(id, "File", HEATMAP_FILES).then(
      (data) => !cancelled && setFiles(data.items),
      () => undefined,
    );
    void riskApi.rank(id, "Function", 10).then(
      (data) => !cancelled && setFunctions(data.items),
      () => undefined,
    );
    // limit=1: the dashboard needs the summary and the file rollup, not the
    // findings themselves — those live in the report and the download.
    void debtApi.get(id, 1).then(
      (data) => !cancelled && setDebt(data),
      (e) => !cancelled && setError(e instanceof ApiError ? e.message : String(e)),
    );

    return () => {
      cancelled = true;
    };
  }, [complete, id]);

  if (loading) return <p className="empty">Loading…</p>;

  if (!repo) {
    return (
      <section className="panel">
        <p className="error">{error ?? "Repository not found."}</p>
        <Link href="/">← All repositories</Link>
      </section>
    );
  }

  const worstFunction = functions[0];
  const criticalFiles = files.filter((f) => f.band === "critical").length;
  const failed = debt ? Object.entries(debt.summary.failed) : [];

  return (
    <>
      <section className="panel">
        <h2>{repo.name}</h2>
        <RepoNav id={id} active="dashboard" />
        <p className="muted mono" style={{ fontSize: 12, margin: "10px 0 0" }}>
          {repo.commit_sha ? repo.commit_sha.slice(0, 8) : "—"}
          {repo.default_branch ? ` on ${repo.default_branch}` : ""}
          {repo.url ? ` · ${repo.url}` : ""}
        </p>
        {repo.error_message && <p className="error">{repo.error_message}</p>}
        {error && !repo.error_message && <p className="error">{error}</p>}
      </section>

      {parsing && (
        <section className="panel">
          <p className="empty">
            Parsing — this page fills in by itself when the parse finishes.
          </p>
        </section>
      )}

      {complete && (
        <>
          <section className="panel">
            <h2>At a glance</h2>
            <div className="tiles">
              <StatTile value={repo.file_count} label="files" />
              <StatTile value={repo.symbol_count} label="symbols" />
              <StatTile
                value={debt ? debt.summary.scanned_total : "…"}
                label="debt findings"
                hint={debt ? `${debt.summary.by_severity.high ?? 0} high severity` : undefined}
              />
              <StatTile
                value={worstFunction ? worstFunction.score.toFixed(0) : "…"}
                label="highest risk"
                hint={worstFunction?.display}
              />
              <StatTile
                value={criticalFiles}
                label="critical files"
                hint={`of ${files.length} scored`}
              />
            </div>
          </section>

          {failed.length > 0 && (
            <section className="panel">
              {/* A detector that crashed reports zero findings, which reads as a
                  clean result. The counts above are wrong until this is fixed. */}
              <p className="error" style={{ fontSize: 12, margin: 0 }}>
                {failed.length} detector{failed.length === 1 ? "" : "s"} did not run, so
                the counts above are incomplete:{" "}
                {failed.map(([kind, why]) => `${kind} (${why})`).join("; ")}
              </p>
            </section>
          )}

          <div className="split">
            <section className="panel">
              <h2>Risk by file</h2>
              <RiskHeatmap
                items={files}
                selected={selected?.key ?? null}
                onSelect={setSelected}
              />
            </section>

            <section className="panel">
              <h2>{selected ? "Why this score" : "Riskiest functions"}</h2>
              {selected ? (
                <>
                  <p className="mono" style={{ fontSize: 13, marginTop: 0 }}>
                    {selected.display}{" "}
                    <span className="muted">
                      {selected.score.toFixed(1)} ({selected.band})
                    </span>
                  </p>
                  <ul className="reasons">
                    {selected.reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                  {/* The factors table is the point: a 0-100 number nobody can
                      take apart is a number nobody should trust. */}
                  <table>
                    <thead>
                      <tr>
                        <th>Factor</th>
                        <th className="num">Value</th>
                        <th className="num">Weight</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(selected.factors).map(([factor, value]) => (
                        <tr key={factor}>
                          <td>{factor}</td>
                          <td className="num">{value.toFixed(2)}</td>
                          <td className="num muted">
                            {(selected.weights[factor] ?? 0).toFixed(2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p style={{ marginBottom: 0, marginTop: 12 }}>
                    <button className="secondary" onClick={() => setSelected(null)}>
                      Back to riskiest functions
                    </button>
                  </p>
                </>
              ) : (
                <BarList items={functions} />
              )}
            </section>
          </div>

          <section className="panel">
            <h2>Technical debt</h2>
            {debt ? (
              <>
                <div className="tiles">
                  {Object.entries(DEBT_LABEL).map(([kind, label]) => (
                    <StatTile
                      key={kind}
                      value={debt.summary.by_kind[kind] ?? 0}
                      label={label}
                    />
                  ))}
                </div>
                {debt.files.length > 0 && (
                  <div className="scroll" style={{ maxHeight: 320, marginTop: 16 }}>
                    <table>
                      <thead>
                        <tr>
                          <th>Files carrying the most debt</th>
                          <th className="num">Findings</th>
                          <th>Worst</th>
                        </tr>
                      </thead>
                      <tbody>
                        {debt.files.slice(0, 10).map((entry) => (
                          <tr key={entry.path}>
                            <td className="mono">
                              <Link
                                href={`/repos/${id}/files?path=${encodeURIComponent(entry.path)}`}
                              >
                                {entry.path}
                              </Link>
                            </td>
                            <td className="num">{entry.findings}</td>
                            <td className="muted">{entry.worst}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <p style={{ marginBottom: 0, marginTop: 12 }}>
                  {/* A plain link, not fetch-and-blob: the endpoint already sets
                      Content-Disposition, so the browser does the rest. */}
                  <a href={debtApi.markdownUrl(id)}>Download the full report (Markdown)</a>
                </p>
              </>
            ) : (
              <p className="empty">Scanning for debt…</p>
            )}
          </section>
        </>
      )}

      <section className="panel">
        <p style={{ margin: 0 }}>
          <Link href="/">← All repositories</Link>
        </p>
      </section>
    </>
  );
}
