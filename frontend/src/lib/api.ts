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

export interface Citation {
  path: string;
  line_start: number;
  line_end: number;
  qualname: string | null;
  kind: string;
  score: number;
  /** "vector" = matched the question; "graph" = reached by following calls. */
  source: "vector" | "graph";
  citation: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  model: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  messages: ChatMessage[];
}

export interface ChatHandlers {
  onContext: (conversationId: string, citations: Citation[]) => void;
  onDelta: (text: string) => void;
  onError: (detail: string) => void;
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

  listConversations: (id: string) =>
    request<Conversation[]>(`/repos/${id}/conversations`),

  getConversation: (id: string, conversationId: string) =>
    request<Conversation>(`/repos/${id}/conversations/${conversationId}`),

  /**
   * Stream an answer.
   *
   * Hand-rolled rather than using EventSource, which can only issue GETs and
   * cannot send a JSON body. The reader below parses SSE frames off a fetch
   * stream: frames are separated by a blank line, and a frame's `data:` lines
   * are joined before parsing.
   */
  async chat(
    id: string,
    message: string,
    conversationId: string | null,
    handlers: ChatHandlers,
    signal?: AbortSignal,
    /** Graph node key whose blast radius should be pulled into the context. */
    focusKey?: string | null,
  ): Promise<void> {
    let response: Response;
    try {
      response = await fetch(`${API_URL}/repos/${id}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          conversation_id: conversationId,
          focus_key: focusKey ?? null,
        }),
        signal,
      });
    } catch {
      throw new ApiError(`Cannot reach the ARGUS API at ${API_URL}. Is it running?`, 0);
    }
    if (!response.ok) {
      throw new ApiError(await errorMessage(response), response.status);
    }
    if (!response.body) {
      throw new ApiError("The server returned no stream", 0);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Keep the trailing partial frame in the buffer — a chunk boundary can
      // land mid-frame, and parsing half a frame loses the event.
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";

      for (const frame of frames) {
        let event = "message";
        const data: string[] = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7).trim();
          else if (line.startsWith("data: ")) data.push(line.slice(6));
        }
        if (!data.length) continue;

        const payload = JSON.parse(data.join("\n"));
        if (event === "context") {
          handlers.onContext(payload.conversation_id, payload.citations ?? []);
        } else if (event === "delta") {
          handlers.onDelta(payload.text);
        } else if (event === "error") {
          handlers.onError(payload.detail);
        }
      }
    }
  },
};

export interface GraphNode {
  key: string;
  type: "Repo" | "File" | "Class" | "Function" | "Module";
  display: string;
  name: string | null;
  path: string | null;
  module: string | null;
  qualname: string | null;
  kind: string | null;
  line_start: number | null;
  line_end: number | null;
  is_external: boolean | null;
  unresolved_calls: number | null;
  change_count: number | null;
}

export interface GraphEdge {
  type: "CONTAINS" | "IMPORTS" | "CALLS" | "INHERITS";
  source: string;
  target: string;
  resolution: string | null;
  confidence: number | null;
  count: number | null;
  line: number | null;
}

export interface GraphResponse {
  view: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** True when the cap hid part of the graph — surface it, don't pretend. */
  truncated: boolean;
}

export const graphApi = {
  get: (id: string, view: "files" | "calls", limit = 600) =>
    request<GraphResponse>(`/repos/${id}/graph?view=${view}&limit=${limit}`),
};

export interface ImpactedNode extends GraphNode {
  hops: number;
  confidence: number;
  score: number;
  via: string[];
  /** Node keys from the target to this node, inclusive — the actual route. */
  route: string[];
}

export interface ImpactSummary {
  total: number;
  depth: number;
  decay: number;
  direct: number;
  max_hops: number;
  top_score: number;
  by_hop: Record<string, number>;
  by_type: Record<string, number>;
}

export interface ImpactResponse {
  root: GraphNode;
  items: ImpactedNode[];
  total: number;
  truncated: boolean;
  summary: ImpactSummary;
}

export interface ImpactExplanation {
  key: string;
  display: string;
  text: string;
  model: string;
  affected: number;
  /** Of the affected nodes, how many were actually described to the model. */
  explained: number;
  depth: number;
  truncated: boolean;
  cached: boolean;
  input_tokens: number;
  output_tokens: number;
  refused: boolean;
}

export const impactApi = {
  get: (id: string, key: string, depth = 3, limit = 300) =>
    request<ImpactResponse>(
      `/repos/${id}/impact?key=${encodeURIComponent(key)}&depth=${depth}&limit=${limit}`,
    ),

  /** Not streamed — a few hundred tokens beside a graph, cached server-side. */
  explain: (id: string, key: string, depth = 3) =>
    request<ImpactExplanation>(
      `/repos/${id}/impact/explain?key=${encodeURIComponent(key)}&depth=${depth}`,
    ),
};

export type RiskBand = "low" | "moderate" | "high" | "critical";

export interface RiskItem {
  key: string;
  type: string;
  display: string;
  score: number;
  band: RiskBand;
  factors: Record<string, number>;
  weights: Record<string, number>;
  reasons: string[];
  node: GraphNode;
}

export interface RiskResponse {
  items: RiskItem[];
  total: number;
  depth: number;
  scored: number;
}

export const riskApi = {
  /** Repository-wide ranking. `limit` is the API's ceiling, so colouring the
   *  canvas by risk covers the top slice and leaves the rest unscored. */
  rank: (id: string, type?: "Function" | "File", limit = 200) =>
    request<RiskResponse>(
      `/repos/${id}/risk?limit=${limit}${type ? `&type=${type}` : ""}`,
    ),
};
