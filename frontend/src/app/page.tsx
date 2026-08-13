"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError, type Repository } from "@/lib/api";

const POLL_MS = 1500;

export default function HomePage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const page = await api.listRepositories();
      setRepos(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while something is actually in flight, so an idle page is quiet.
  const inFlight = repos.some((r) => r.status === "pending" || r.status === "parsing");
  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [inFlight, refresh]);

  async function submitUrl(event: React.FormEvent) {
    event.preventDefault();
    if (!url.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.createRepository(url.trim());
      setUrl("");
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitZip(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      await api.uploadRepository(file);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function remove(id: string) {
    try {
      await api.deleteRepository(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <>
      <section className="panel">
        <h2>Ingest a repository</h2>
        <form className="ingest" onSubmit={submitUrl}>
          <input
            type="text"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/psf/requests"
            aria-label="Git repository URL"
            disabled={busy}
          />
          <button type="submit" disabled={busy || !url.trim()}>
            {busy ? "Working…" : "Parse repository"}
          </button>
        </form>

        <div className="upload-row">
          <span>or upload a zip archive:</span>
          <input
            ref={fileInput}
            type="file"
            accept=".zip"
            onChange={submitZip}
            disabled={busy}
            aria-label="Repository zip archive"
          />
        </div>

        {error && <p className="error">{error}</p>}
      </section>

      <section className="panel">
        <h2>Repositories</h2>
        {!loaded ? (
          <p className="empty">Loading…</p>
        ) : repos.length === 0 ? (
          <p className="empty">Nothing ingested yet. Paste a GitHub URL above.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th className="num">Files</th>
                <th className="num">Symbols</th>
                <th>Commit</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {repos.map((repo) => (
                <tr key={repo.id}>
                  <td>
                    <Link href={`/repos/${repo.id}`}>{repo.name}</Link>
                    {repo.error_message && (
                      <div className="muted" style={{ fontSize: 12 }}>
                        {repo.error_message}
                      </div>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${repo.status}`}>{repo.status}</span>
                  </td>
                  <td className="num">{repo.file_count}</td>
                  <td className="num">{repo.symbol_count}</td>
                  <td className="mono muted">
                    {repo.commit_sha ? repo.commit_sha.slice(0, 8) : "—"}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <button className="secondary" onClick={() => void remove(repo.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
