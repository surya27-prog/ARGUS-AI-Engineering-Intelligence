/**
 * Typed client for the ARGUS API.
 *
 * The types mirror `backend/app/schemas/repository.py`. They are hand-written
 * for now; Week 6 can generate them from the OpenAPI schema instead.
 */

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export type ParseStatus = "pending" | "parsing" | "complete" | "failed";

export interface Repository {
  id: string;
  name: string;
  url: string | null;
  default_branch: string | null;
  commit_sha: string | null;
  status: ParseStatus;
  error_message: string | null;
  file_count: number;
  symbol_count: number;
  created_at: string;
  updated_at: string;
  parsed_at: string | null;
}

export interface SourceFile {
  id: string;
  path: string;
  module: string | null;
  language: string;
  extension: string;
  size_bytes: number;
  line_count: number;
  symbol_count: number;
  import_count: number;
  call_count: number;
  parse_error: string | null;
}

export interface SymbolParameter {
  name: string;
  kind: string;
  annotation: string | null;
  default: string | null;
}

export interface Symbol {
  id: string;
  file_id: string;
  name: string;
  qualname: string;
  kind: "class" | "function" | "method" | "module";
  module: string | null;
  parent: string | null;
  line_start: number;
  line_end: number;
  docstring: string | null;
  returns: string | null;
  is_async: boolean;
  decorators: string[];
  parameters: SymbolParameter[];
  base_classes: string[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    // A network-level failure here almost always means the API is not running,
    // which is worth saying plainly rather than surfacing "Failed to fetch".
    throw new ApiError(`Cannot reach the ARGUS API at ${API_URL}. Is it running?`, 0);
  }

  if (!response.ok) {
    throw new ApiError(await errorMessage(response), response.status);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors arrive as a list of {loc, msg}.
    if (Array.isArray(detail)) {
      return detail.map((d: { msg?: string }) => d.msg ?? "invalid input").join("; ");
    }
  } catch {
    // fall through to the status text
  }
  return `${response.status} ${response.statusText}`;
}

export const api = {
  listRepositories: () => request<Page<Repository>>("/repos"),

  getRepository: (id: string) => request<Repository>(`/repos/${id}`),

  createRepository: (url: string) =>
    request<Repository>("/repos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }),

  uploadRepository: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Repository>("/repos/upload", { method: "POST", body: form });
  },

  deleteRepository: (id: string) =>
    request<void>(`/repos/${id}`, { method: "DELETE" }),

  listFiles: (id: string, search = "", limit = 500) =>
    request<Page<SourceFile>>(
      `/repos/${id}/files?limit=${limit}${search ? `&search=${encodeURIComponent(search)}` : ""}`,
    ),

  listSymbols: (id: string, fileId?: string, limit = 500) =>
    request<Page<Symbol>>(
      `/repos/${id}/symbols?limit=${limit}${fileId ? `&file_id=${fileId}` : ""}`,
    ),
};
