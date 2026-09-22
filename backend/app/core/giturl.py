"""Validation for the one string a caller hands us that becomes a subprocess.

`POST /repos` takes a URL and runs `git clone` on it. That makes it the most
sensitive input in the API, and three things about it are worth refusing rather
than discovering later.

**A leading dash would be read as a flag.** `git clone <url> <dest>` passes the
URL as an argv element, so a value like `--upload-pack=…` is an option, not an
address. Requiring a known scheme prefix blocks it — which the original validator
already did, and this keeps.

**Embedded credentials leak.** `https://user:token@host/repo` is a valid clone
URL, and ARGUS stores the URL on the repository row, renders it on the dashboard
and writes it into the parse job. A token pasted once would then be visible to
anyone who can see the repository list.

**A private address turns the server into a proxy.** Nothing stops
`http://169.254.169.254/…` or `http://10.0.0.5/git/repo` from being a valid clone
target, and a server that will fetch an arbitrary internal address on request is
a server that will read a cloud metadata endpoint on request. Blocked by default,
with a setting to allow it for anyone running a git server on their own network.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

# Longest URL accepted. Real clone URLs are well under 200 characters; the cap
# exists so nothing downstream has to think about an unbounded string.
MAX_URL_LENGTH = 512

ALLOWED_SCHEMES = ("http://", "https://", "ssh://", "git://")

# `git@github.com:owner/repo.git` — scp-like, no scheme, so `urlsplit` cannot
# read a host out of it and it is matched directly.
_SCP_LIKE = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>.+)$")

# Hostnames that resolve to the machine itself, whatever the DNS says.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class GitUrlError(ValueError):
    """The URL is not one ARGUS will clone. The message is user-facing."""


def validate_git_url(value: str, *, allow_private_hosts: bool = False) -> str:
    """Return the URL unchanged, or raise `GitUrlError` explaining why not."""
    url = value.strip()

    if not url:
        raise GitUrlError("url must not be empty")
    if len(url) > MAX_URL_LENGTH:
        raise GitUrlError(f"url must be at most {MAX_URL_LENGTH} characters")
    if any(ch in url for ch in "\n\r\t\0"):
        raise GitUrlError("url must not contain control characters")

    if scp := _SCP_LIKE.match(url):
        # ssh to a named host. No credential can hide in this form beyond the
        # user, which is part of the address rather than a secret.
        _check_host(scp.group("host"), allow_private_hosts=allow_private_hosts)
        return url

    if not url.startswith(ALLOWED_SCHEMES):
        raise GitUrlError(
            "url must start with https://, http://, ssh:// or git://, "
            "or be an scp-style address like git@github.com:owner/repo.git"
        )

    parts = urlsplit(url)
    if not parts.hostname:
        raise GitUrlError("url must include a host")

    # `parts.password` is set for `https://user:token@host`. A bare `user@host`
    # is an ssh identity rather than a secret, so only the password is refused.
    if parts.password:
        raise GitUrlError(
            "remove the credentials from the url — ARGUS stores and displays it, "
            "so a token in it would be visible to anyone who can see the "
            "repository list. Use a credential helper or a deploy key instead"
        )

    _check_host(parts.hostname, allow_private_hosts=allow_private_hosts)
    return url


def _check_host(host: str, *, allow_private_hosts: bool) -> None:
    if allow_private_hosts:
        return

    lowered = host.lower().strip("[]")
    if lowered in _LOCAL_NAMES:
        raise GitUrlError(_private_message(host))

    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        # A name, not a literal. Deliberately not resolved here: a DNS lookup
        # inside a validator makes request latency depend on a resolver, and a
        # name that resolves to a private address between this check and the
        # clone would pass anyway. This stops the obvious cases; the clone itself
        # is the real boundary.
        return

    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        raise GitUrlError(_private_message(host))


def _private_message(host: str) -> str:
    return (
        f"{host} is a local or private address. ARGUS will not clone from one by "
        "default, because a server that fetches arbitrary internal addresses on "
        "request can be used to read them. Set ALLOW_PRIVATE_GIT_HOSTS=true if "
        "you are pointing it at a git server on your own network"
    )


__all__ = ["ALLOWED_SCHEMES", "MAX_URL_LENGTH", "GitUrlError", "validate_git_url"]
