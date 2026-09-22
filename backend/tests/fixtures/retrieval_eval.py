"""The retrieval eval set: 10 questions against psf/requests.

Written on Day 3, before hybrid retrieval exists, so Day 4's claim that graph
expansion beats pure vector search is measured against a fixed target rather
than one moved to fit the result.

Each question names the qualname a correct answer must surface. Questions are
phrased the way someone unfamiliar with the codebase would ask - in intent, not
in the identifiers they are hoping to find - because a question containing the
answer's name only measures keyword matching.
"""

from __future__ import annotations

from dataclasses import dataclass

FIXTURE_REPO = "https://github.com/psf/requests"


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    question: str
    expected: str
    why: str


QUESTIONS: tuple[EvalQuestion, ...] = (
    EvalQuestion(
        "how does a session actually send an http request",
        "Session.request",
        "the central method every verb helper funnels through",
    ),
    EvalQuestion(
        "where are redirects followed",
        "SessionRedirectMixin.resolve_redirects",
        "the redirect loop",
    ),
    EvalQuestion(
        "how is a request turned into something that can be sent over the wire",
        "PreparedRequest.prepare",
        "the prepare pipeline",
    ),
    EvalQuestion(
        "how do i read a response body a piece at a time instead of all at once",
        "Response.iter_content",
        "streaming reads",
    ),
    EvalQuestion(
        "where does basic authentication get added to a request",
        "HTTPBasicAuth.__call__",
        "auth applied as a callable hook",
    ),
    EvalQuestion(
        "how are cookies carried from a response into the next request",
        "extract_cookies_to_jar",
        "cookie propagation",
    ),
    EvalQuestion(
        "what decides whether a proxy should be bypassed for a given url",
        "should_bypass_proxies",
        "proxy bypass rules",
    ),
    EvalQuestion(
        "how is the character encoding of a response body guessed",
        "get_encoding_from_headers",
        "encoding detection",
    ),
    EvalQuestion(
        "where is a file upload encoded into a multipart body",
        "RequestEncodingMixin._encode_files",
        "multipart encoding",
    ),
    EvalQuestion(
        "what raises an error when the server returns a 404 or 500",
        "Response.raise_for_status",
        "status-code to exception",
    ),
)
