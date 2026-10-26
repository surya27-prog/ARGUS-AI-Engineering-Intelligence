"use client";

import Link from "next/link";

export type RepoTab = "dashboard" | "files" | "graph" | "chat";

const TABS: Array<[RepoTab, string, string]> = [
  ["dashboard", "", "Dashboard"],
  ["files", "/files", "Files & symbols"],
  ["graph", "/graph", "Graph"],
  ["chat", "/chat", "Ask"],
];

/**
 * One nav for every repository page.
 *
 * Previously each page carried its own hand-written link line, and they had
 * drifted — the chat page's "back" link pointed at what is now the dashboard
 * while calling it "Files and symbols". One component means adding a page cannot
 * leave three others describing the app wrongly.
 */
export default function RepoNav({ id, active }: { id: string; active: RepoTab }) {
  return (
    <nav className="repo-nav" aria-label="Repository views">
      {TABS.map(([tab, suffix, label]) =>
        tab === active ? (
          <span key={tab} className="repo-nav-current" aria-current="page">
            {label}
          </span>
        ) : (
          <Link key={tab} href={`/repos/${id}${suffix}`}>
            {label}
          </Link>
        ),
      )}
    </nav>
  );
}
