"use client";

import { ApiError } from "@/lib/api";

/**
 * One way to show a failure, everywhere.
 *
 * Each page had its own `<p className="error">{message}</p>`, which threw away
 * the two things the API goes out of its way to provide: how long to wait after
 * a 429, and the correlation id on a 500. A bare "429 Too Many Requests" invites
 * an immediate retry that fails again; "retry in 12s" does not.
 *
 * The tone follows the cause. A rate limit or an unreachable service is a wait,
 * so it is stated as one and offers a retry. A 4xx is something the request got
 * wrong and retrying will not fix, so no retry is offered.
 */
export function describeError(error: unknown): {
  message: string;
  hint?: string;
  transient: boolean;
} {
  if (!(error instanceof ApiError)) {
    return { message: error instanceof Error ? error.message : String(error), transient: false };
  }

  const hints: string[] = [];
  if (error.retryAfter) {
    hints.push(`Retry in ${error.retryAfter}s.`);
  }
  if (error.errorId) {
    // Also in the server log against the traceback.
    hints.push(`Error id ${error.errorId}.`);
  }
  if (error.status === 0) {
    hints.push("The API may not be running.");
  }

  return {
    message: error.message,
    hint: hints.length ? hints.join(" ") : undefined,
    transient: error.isTransient,
  };
}

export interface ErrorNoteProps {
  error: unknown;
  /** Shown as a retry button when the failure is worth retrying. */
  onRetry?: () => void;
  onDismiss?: () => void;
}

export default function ErrorNote({ error, onRetry, onDismiss }: ErrorNoteProps) {
  if (!error) return null;
  const { message, hint, transient } = describeError(error);

  return (
    // `role=alert` so a screen reader announces it: an error that only appears
    // visually is invisible to anyone not looking at that part of the page.
    <div className={`error-note ${transient ? "transient" : ""}`} role="alert">
      <p>{message}</p>
      {hint && <p className="error-hint">{hint}</p>}
      {(onRetry || onDismiss) && (
        <p className="error-actions">
          {transient && onRetry && (
            <button className="secondary" onClick={onRetry}>
              Try again
            </button>
          )}
          {onDismiss && (
            <button className="secondary" onClick={onDismiss}>
              Dismiss
            </button>
          )}
        </p>
      )}
    </div>
  );
}
