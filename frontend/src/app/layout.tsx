import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "ARGUS",
  description: "AI Engineering Intelligence Platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="masthead">
            <h1>
              <Link href="/" style={{ color: "var(--text)" }}>
                ARGUS
              </Link>
            </h1>
            <span className="tagline">AI Engineering Intelligence</span>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
