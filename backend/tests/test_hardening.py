"""Input validation and rate limiting.

Both are pure: a limiter's behaviour is a function of its clock and a validator's
of its input, so neither needs Postgres, Neo4j or a provider to be tested.
"""

from __future__ import annotations

import pytest

from app.core.giturl import MAX_URL_LENGTH, GitUrlError, validate_git_url
from app.core.ratelimit import RateLimiter, client_key

# --- git URL validation ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/psf/requests.git",
        "https://github.com/psf/requests",
        "http://github.com/psf/requests.git",
        "git@github.com:psf/requests.git",
        "ssh://git@github.com/psf/requests.git",
        "git://github.com/psf/requests.git",
        # A username with no password is an ssh identity, not a secret.
        "https://someone@github.com/psf/requests.git",
    ],
)
def test_real_clone_urls_are_accepted(url: str):
    assert validate_git_url(url) == url


def test_surrounding_whitespace_is_stripped():
    assert validate_git_url("  https://github.com/psf/requests.git \n") == (
        "https://github.com/psf/requests.git"
    )


def test_a_leading_dash_is_refused():
    """`git clone <url> <dest>` passes the URL as argv, so `--upload-pack=…`
    would be read as an option rather than an address."""
    with pytest.raises(GitUrlError, match="must start with"):
        validate_git_url("--upload-pack=sh -c evil")


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "/etc/passwd", "C:\\Windows", "javascript:alert(1)", "ftp://h/r"],
)
def test_non_git_schemes_are_refused(url: str):
    with pytest.raises(GitUrlError, match="must start with"):
        validate_git_url(url)


def test_embedded_credentials_are_refused():
    """The URL is stored on the row and rendered on the dashboard, so a token in
    it would be visible to anyone who can see the repository list."""
    with pytest.raises(GitUrlError, match="remove the credentials"):
        validate_git_url("https://user:ghp_secret@github.com/o/r.git")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/r.git",
        "http://127.0.0.1/r.git",
        "https://10.0.0.5/git/r.git",
        "https://192.168.1.10/r.git",
        "https://172.16.0.1/r.git",
        # The cloud metadata endpoint — the case this check exists for.
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/r.git",
        "git@localhost:owner/repo.git",
    ],
)
def test_private_and_local_addresses_are_refused_by_default(url: str):
    with pytest.raises(GitUrlError, match="local or private address"):
        validate_git_url(url)


def test_private_addresses_are_allowed_when_configured():
    """For anyone pointing ARGUS at a git server on their own network."""
    url = "http://10.0.0.5/git/r.git"
    assert validate_git_url(url, allow_private_hosts=True) == url


def test_a_public_hostname_is_not_resolved():
    """Resolving inside a validator would make request latency depend on DNS,
    and a name resolving to a private address later would pass anyway."""
    assert validate_git_url("https://git.example.com/r.git")


def test_empty_and_oversized_urls_are_refused():
    with pytest.raises(GitUrlError, match="must not be empty"):
        validate_git_url("   ")
    with pytest.raises(GitUrlError, match=f"at most {MAX_URL_LENGTH}"):
        validate_git_url("https://github.com/" + "a" * MAX_URL_LENGTH)


def test_control_characters_are_refused():
    with pytest.raises(GitUrlError, match="control characters"):
        validate_git_url("https://github.com/o/r.git\nrm -rf /")


def test_a_scheme_with_no_host_is_refused():
    with pytest.raises(GitUrlError, match="must include a host"):
        validate_git_url("https:///path/only")


# --- rate limiting -----------------------------------------------------------


def test_a_burst_up_to_capacity_is_allowed():
    limiter = RateLimiter(name="t", per_minute=60, capacity=3)

    assert [limiter.check("a", now=0.0) for _ in range(3)] == [None, None, None]


def test_the_call_past_capacity_is_refused_with_a_wait():
    limiter = RateLimiter(name="t", per_minute=60, capacity=2)
    for _ in range(2):
        limiter.check("a", now=0.0)

    wait = limiter.check("a", now=0.0)

    assert wait is not None
    assert wait >= 1.0


def test_the_bucket_refills_over_time():
    """A token bucket rather than a fixed window: a window lets a caller spend
    the whole allowance at the end of one and again at the start of the next."""
    limiter = RateLimiter(name="t", per_minute=60, capacity=1)
    assert limiter.check("a", now=0.0) is None
    assert limiter.check("a", now=0.5) is not None
    # 60/minute is one per second, so a second later there is a token again.
    assert limiter.check("a", now=1.0) is None


def test_refill_is_capped_at_capacity():
    """Idling for an hour must not buy an hour's worth of burst."""
    limiter = RateLimiter(name="t", per_minute=60, capacity=2)
    limiter.check("a", now=0.0)

    allowed = [limiter.check("a", now=3600.0) for _ in range(3)]

    assert allowed[:2] == [None, None]
    assert allowed[2] is not None


def test_clients_have_separate_buckets():
    limiter = RateLimiter(name="t", per_minute=60, capacity=1)
    limiter.check("a", now=0.0)

    assert limiter.check("b", now=0.0) is None


def test_reset_forgives_one_client_or_all():
    limiter = RateLimiter(name="t", per_minute=60, capacity=1)
    limiter.check("a", now=0.0)
    limiter.check("b", now=0.0)

    limiter.reset("a")
    assert limiter.check("a", now=0.0) is None
    assert limiter.check("b", now=0.0) is not None

    limiter.reset()
    assert limiter.check("b", now=0.0) is None


def test_tracked_clients_are_bounded():
    """Otherwise memory grows with request volume, which is the shape of a leak."""
    from app.core.ratelimit import MAX_TRACKED_CLIENTS

    limiter = RateLimiter(name="t", per_minute=60, capacity=1)
    for i in range(MAX_TRACKED_CLIENTS + 50):
        limiter.check(f"client-{i}", now=0.0)

    assert len(limiter._buckets) <= MAX_TRACKED_CLIENTS


class _FakeRequest:
    def __init__(self, headers: dict[str, str], host: str | None):
        self.headers = headers
        self.client = type("C", (), {"host": host})() if host else None


def test_the_client_key_prefers_the_forwarded_address():
    """Behind a proxy, `request.client.host` is the proxy for every caller — one
    shared bucket for the world."""
    request = _FakeRequest({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, "10.0.0.1")

    assert client_key(request) == "203.0.113.7"


def test_the_client_key_falls_back_to_the_peer_address():
    assert client_key(_FakeRequest({}, "198.51.100.4")) == "198.51.100.4"


def test_a_missing_client_still_yields_a_key():
    """ASGI does not guarantee `client`; a None here must not 500 the request."""
    assert client_key(_FakeRequest({}, None)) == "unknown"
