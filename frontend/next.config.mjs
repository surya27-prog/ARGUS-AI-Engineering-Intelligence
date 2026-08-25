import path from "node:path";
import { fileURLToPath } from "node:url";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emits `.next/standalone` with a minimal server and only the modules the
  // app actually imports, traced from the build. Without it the runtime image
  // has to carry all of node_modules.
  output: "standalone",
  // Next walks up looking for a lockfile to infer the workspace root, and picks
  // up unrelated ones outside the repo. Pin it to this directory.
  outputFileTracingRoot: path.dirname(fileURLToPath(import.meta.url)),
};

export default nextConfig;
