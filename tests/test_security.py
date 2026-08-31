"""The security requirements, asserted rather than assumed.

Covers the outbound fetch rules, the redirect allowlist, input bounds, and the
promise that no traceback ever reaches a tool result.
"""

from __future__ import annotations

import io
import json
import signal
import time
import urllib.error
from contextlib import contextmanager
from email.message import Message

import pytest

from akamai_cloud_docs_mcp import config, http
from akamai_cloud_docs_mcp.core import catalog
from akamai_cloud_docs_mcp.core.catalog import (
    FetchError,
    InvalidDocId,
    clean_guide_markdown,
    fetch_guide,
    fetch_url,
    normalize_guide_id,
)
from akamai_cloud_docs_mcp.core.sections import build_toc, flatten_inline, parse_document, snippet


class _FakeResponse(io.BytesIO):
    """Enough of an HTTP response for `fetch_url`."""

    def __init__(self, body: bytes, charset: str = "utf-8"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = f"text/markdown; charset={charset}"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


@pytest.fixture
def captured(monkeypatch):
    """Capture the Request that `fetch_url` would send, and reply with a body."""
    seen: dict = {}

    def fake_open(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        seen["allowed_hosts"] = getattr(request, "allowed_hosts", None)
        return _FakeResponse(seen.get("body", b"# Page\n\nBody text.\n"))

    monkeypatch.setattr(catalog._opener, "open", fake_open)
    return seen


class TestOutboundRules:
    def test_only_https_is_fetched(self):
        with pytest.raises(FetchError, match="only https"):
            fetch_url("http://techdocs.akamai.com/cloud-computing/docs/x.md")

    def test_url_without_a_host_is_rejected(self):
        with pytest.raises(FetchError):
            fetch_url("https:///no-host")

    def test_user_agent_names_the_project_and_version(self, captured):
        fetch_url("https://techdocs.akamai.com/cloud-computing/docs/x.md")
        agent = captured["headers"]["User-agent"]
        assert agent == config.USER_AGENT
        assert agent.startswith("akamai-cloud-docs-mcp/")

    def test_user_agent_is_exactly_the_project_and_version(self):
        # An exact match, so nothing else can creep into what techdocs sees.
        import re

        from akamai_cloud_docs_mcp import __version__

        assert f"akamai-cloud-docs-mcp/{__version__}" == config.USER_AGENT
        assert re.fullmatch(r"akamai-cloud-docs-mcp/\d+\.\d+\.\d+", config.USER_AGENT)

    def test_timeout_is_applied(self, captured):
        fetch_url("https://techdocs.akamai.com/cloud-computing/docs/x.md")
        assert captured["timeout"] == config.REQUEST_TIMEOUT
        assert config.REQUEST_TIMEOUT <= 30

    def test_response_cap_is_enforced(self, monkeypatch):
        oversized = b"x" * (config.MAX_RESPONSE_BYTES + 10)
        monkeypatch.setattr(
            catalog._opener, "open", lambda request, timeout=None: _FakeResponse(oversized)
        )
        with pytest.raises(FetchError, match="response cap"):
            fetch_url("https://techdocs.akamai.com/cloud-computing/docs/x.md")

    def test_cap_is_two_megabytes_for_a_page(self):
        assert config.MAX_RESPONSE_BYTES == 2 * 1024 * 1024

    def test_http_errors_do_not_leak_the_body(self, monkeypatch):
        def raise_http_error(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", Message(), io.BytesIO(b"secret internals")
            )

        monkeypatch.setattr(catalog._opener, "open", raise_http_error)
        with pytest.raises(FetchError) as caught:
            fetch_url("https://techdocs.akamai.com/cloud-computing/docs/x.md")
        assert "secret internals" not in str(caught.value)
        assert "HTTP 500" in str(caught.value)


class TestRedirectAllowlist:
    @pytest.fixture
    def handler(self):
        return catalog._AllowlistRedirectHandler()

    def _request(self, allowed: set[str]):
        import urllib.request

        request = urllib.request.Request("https://techdocs.akamai.com/cloud-computing/docs/x.md")
        request.allowed_hosts = allowed
        return request

    def test_redirect_to_another_host_is_refused(self, handler):
        with pytest.raises(FetchError, match="unexpected host"):
            handler.redirect_request(
                self._request({"techdocs.akamai.com"}),
                None,
                302,
                "Found",
                Message(),
                "https://evil.example.com/cloud-computing/docs/x.md",
            )

    def test_redirect_to_http_is_refused(self, handler):
        with pytest.raises(FetchError, match="non-https"):
            handler.redirect_request(
                self._request({"techdocs.akamai.com"}),
                None,
                302,
                "Found",
                Message(),
                "http://techdocs.akamai.com/cloud-computing/docs/x.md",
            )

    def test_allowed_host_carries_to_the_next_hop(self, handler):
        followed = handler.redirect_request(
            self._request({"techdocs.akamai.com"}),
            None,
            302,
            "Found",
            Message(),
            "https://techdocs.akamai.com/cloud-computing/docs/other.md",
        )
        assert followed.allowed_hosts == {"techdocs.akamai.com"}

    def test_default_allowed_host_is_the_original(self, captured):
        fetch_url("https://techdocs.akamai.com/cloud-computing/docs/x.md")
        assert captured["allowed_hosts"] == {"techdocs.akamai.com"}


class TestModelSuppliedIds:
    """A model can name a document. It can never name a URL to fetch."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example.com/cloud-computing/docs/x.md",
            "http://techdocs.akamai.com/cloud-computing/docs/x.md",
            "https://techdocs.akamai.com/cloud-computing/docs/../../secret.md",
            "https://techdocs.akamai.com@evil.example.com/cloud-computing/docs/x.md",
            "https://techdocs.akamai.com/admin/keys",
            "file:///etc/passwd",
            "../../../etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_hostile_ids_never_reach_the_network(self, hostile, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError(f"a request was attempted for {hostile!r}")

        monkeypatch.setattr(catalog._opener, "open", explode)
        with pytest.raises(InvalidDocId):
            normalize_guide_id(hostile)
        with pytest.raises((InvalidDocId, FetchError)):
            fetch_guide(hostile)

    def test_a_slug_is_rebuilt_into_the_canonical_url(self, captured):
        fetch_guide("aiven-manage-database")
        assert (
            captured["url"]
            == "https://techdocs.akamai.com/cloud-computing/docs/aiven-manage-database.md"
        )


class TestInputBounds:
    def test_bounds_are_set(self):
        assert config.MAX_ID_LEN == 256
        assert config.MAX_SECTION_LEN == 256
        assert config.MAX_QUERY_LEN == 1000
        assert config.MAX_K == 20
        assert config.MIN_K == 1

    def test_query_is_capped(self, service):
        assert service.search_docs("widget " * 1000) is not None

    @pytest.mark.parametrize("value", [-5, 0, 1, 5, 20, 999, 10**9])
    def test_k_never_exceeds_the_cap(self, service, value):
        assert len(service.search_docs("widget", k=value)) <= config.MAX_K

    def test_id_and_section_are_capped(self, service):
        assert "error" in service.fetch_doc("a" * 257)
        assert "error" in service.fetch_doc("create-a-widget", section="1" * 257)


# One line per shape, each about 100 KB. Every one of these hung a parser for
# over 30 s before the patterns behind them were made linear.
_STAR_LINE = " *a" * 33_334
_UNDERSCORE_LINE = " _a" * 33_334
_HEADING_SPACES = "## Step" + " " * 100_000 + "b"
_HEADING_HASHES = "## Step" + " " * 50_000 + "#" * 50_000 + " x"
_NAV_LINE = "- [" + "](a" * 33_333 + " x"
_COMMENT_OPENERS = "<!--" * 25_000
_HTML_BLOCK = (
    "[block:html]" + json.dumps({"html": "<!--" * 25_000}).replace("<", "\\u003c") + "[/block]"
)

ADVERSARIAL_PAGES = {
    "star-emphasis": "# Title\n\nintro\n\n## Step\n\n" + _STAR_LINE + "\n",
    "underscore-emphasis": "# Title\n\nintro\n\n## Step\n\n" + _UNDERSCORE_LINE + "\n",
    "heading-space-run": "# Title\n\nintro\n\n" + _HEADING_SPACES + "\n\nbody.\n",
    "heading-closing-hashes": "# Title\n\nintro\n\n" + _HEADING_HASHES + "\n\nbody.\n",
    "nav-link-line": "# Title\n\nintro\n\n## Step\n\nbody.\n\n## Sibling pages\n\n"
    + _NAV_LINE
    + "\n",
    "comment-openers": "# Title\n\nintro\n\n## Step\n\n" + _COMMENT_OPENERS + "\n",
    "html-block-comments": "# Title\n\nintro\n\n## Step\n\n" + _HTML_BLOCK + "\n",
}


@contextmanager
def _budget(seconds: float, label: str):
    """Fail when the block runs past `seconds`.

    An alarm cuts a hang short where the platform has one, so a regression
    costs the suite one second per case rather than minutes. The wall-clock
    check behind it is what fails the test.
    """
    has_alarm = hasattr(signal, "setitimer")

    def alarm(signum, frame):
        raise TimeoutError(f"{label} ran past {seconds} s")

    if has_alarm:
        previous = signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.perf_counter()
    try:
        yield
    finally:
        if has_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    assert time.perf_counter() - started < seconds, f"{label} ran past {seconds} s"


class TestParserTimeBounds:
    """A crafted page costs one scan, not one scan per marker.

    Each shape goes through the entry points sync and the tools use: the
    cleaner, the parser and its table of contents, and the search snippet.
    The budget is 1 s for about 100 KB; the linear passes take under 50 ms.
    """

    @pytest.mark.parametrize("name", list(ADVERSARIAL_PAGES))
    def test_clean_parse_and_snippet_are_bounded(self, name):
        page = ADVERSARIAL_PAGES[name]
        assert len(page) > 100_000
        with _budget(1.0, f"clean_guide_markdown[{name}]"):
            body, _ = clean_guide_markdown(page)
        with _budget(1.0, f"parse_document+build_toc[{name}]"):
            build_toc(parse_document(body, "Title"))
        with _budget(1.0, f"snippet[{name}]"):
            snippet(body, ["step"])

    @pytest.mark.parametrize(
        "line",
        [
            _STAR_LINE,
            _UNDERSCORE_LINE,
            _HEADING_SPACES,
            _HEADING_HASHES,
            _NAV_LINE,
            _COMMENT_OPENERS,
        ],
        ids=["star", "underscore", "heading-spaces", "heading-hashes", "nav-link", "comments"],
    )
    def test_flatten_inline_is_bounded(self, line):
        with _budget(1.0, "flatten_inline"):
            flatten_inline(line)

    def test_html_block_no_longer_smuggles_comment_openers(self):
        body, _ = clean_guide_markdown(ADVERSARIAL_PAGES["html-block-comments"])
        assert "<!--" not in body


class TestNoTracebacksInResults:
    """Every failure path returns a sentence, not a stack trace."""

    HOSTILE = [
        "",
        " ",
        "../../../etc/passwd",
        "https://evil.example.com/x.md",
        "\x00nullbyte",
        "a" * 500,
        "POST /nope",
        "{}",
        "<script>alert(1)</script>",
    ]

    @pytest.mark.parametrize("bad_id", HOSTILE)
    def test_fetch_doc_never_raises_or_leaks(self, service, bad_id):
        result = service.fetch_doc(bad_id)
        assert isinstance(result, dict)
        assert "error" in result
        rendered = str(result)
        assert "Traceback" not in rendered
        assert ".py" not in rendered

    @pytest.mark.parametrize("bad_id", HOSTILE)
    def test_search_docs_never_raises(self, service, bad_id):
        assert isinstance(service.search_docs(bad_id), list)

    def test_bad_section_never_leaks(self, service, big_guide_live):
        result = service.fetch_doc("create-a-widget", section="../../etc")
        assert "Traceback" not in str(result)


class TestHttpDefaults:
    def test_binds_loopback_by_default(self):
        assert http.DEFAULT_HOST == "127.0.0.1"

    def test_default_port_and_path(self):
        assert http.DEFAULT_PORT == 8000
        assert http.DEFAULT_PATH == "/mcp"


class TestHttpHostValidation:
    """The SDK checks Host and Origin only for a loopback bind unless told otherwise."""

    def test_no_flags_on_a_reachable_bind_keep_the_sdk_defaults(self):
        assert http.transport_security([], [], host="0.0.0.0") is None

    def test_a_loopback_bind_gets_the_loopback_allowlists(self):
        settings = http.transport_security([], [])
        assert settings.enable_dns_rebinding_protection is True
        assert settings.allowed_hosts == list(http.LOOPBACK_ALLOWED_HOSTS)
        assert settings.allowed_origins == list(http.LOOPBACK_ALLOWED_ORIGINS)

    @pytest.mark.parametrize(
        ("host", "pattern"),
        [("127.0.0.2", "127.0.0.2"), ("::ffff:127.0.0.1", "[::ffff:127.0.0.1]")],
    )
    def test_a_non_canonical_loopback_bind_is_checked_and_reachable(self, host, pattern):
        # The SDK names only 127.0.0.1, localhost and ::1. Any other loopback
        # bind gets the same checks, plus its own address so a client on the
        # box can still send `Host: <bind>:PORT`.
        settings = http.transport_security([], [], host=host)
        assert settings.enable_dns_rebinding_protection is True
        assert f"{pattern}:*" in settings.allowed_hosts
        assert f"http://{pattern}:*" in settings.allowed_origins
        assert "127.0.0.1:*" in settings.allowed_hosts

    def test_the_bind_address_stays_accepted_next_to_a_host_list(self):
        settings = http.transport_security(["docs.example.com"], [], host="127.0.0.2")
        assert "127.0.0.2:*" in settings.allowed_hosts
        assert "docs.example.com" in settings.allowed_hosts

    def test_a_host_list_turns_validation_on_everywhere(self):
        settings = http.transport_security(["docs.example.com", "docs.example.com:*"], [])
        assert settings.enable_dns_rebinding_protection is True
        assert "docs.example.com" in settings.allowed_hosts
        assert "docs.example.com:*" in settings.allowed_hosts
        # Loopback stays allowed so a smoke test on the box keeps working.
        assert "127.0.0.1:*" in settings.allowed_hosts

    def test_an_origin_list_alone_is_enough(self):
        settings = http.transport_security([], ["https://docs.example.com"])
        assert settings.enable_dns_rebinding_protection is True
        assert "https://docs.example.com" in settings.allowed_origins

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
    def test_loopback_addresses(self, host):
        assert http.is_loopback(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "docs.example.com"])
    def test_reachable_addresses(self, host):
        assert not http.is_loopback(host)

    def test_non_loopback_bind_without_a_host_list_warns_once(self, service, capsys):
        from akamai_cloud_docs_mcp.core import tools as core_tools

        core_tools.set_service(service)
        http.build_app(auto_sync=False, host="0.0.0.0")
        err = capsys.readouterr().err
        assert err.count("--allowed-host") == 1
        assert "validation is off" in err

    def test_non_loopback_bind_with_origins_only_says_host_checks_are_on(self, service, capsys):
        # An origin list alone turns the checks on with loopback Hosts only,
        # so the line has to say that, not that validation is off.
        from akamai_cloud_docs_mcp.core import tools as core_tools

        core_tools.set_service(service)
        http.build_app(auto_sync=False, host="0.0.0.0", allowed_origins=["https://docs.example.com"])
        err = capsys.readouterr().err
        assert "validation is off" not in err
        assert "loopback names only" in err
        assert "421" in err
        assert err.count("--allowed-host") == 2

    def test_non_loopback_bind_with_a_host_list_is_quiet(self, service, capsys):
        from akamai_cloud_docs_mcp.core import tools as core_tools

        core_tools.set_service(service)
        http.build_app(auto_sync=False, host="0.0.0.0", allowed_hosts=["docs.example.com"])
        err = capsys.readouterr().err
        assert "validation is off" not in err
        assert "loopback names only" not in err

    def test_loopback_bind_does_not_warn(self, service, capsys):
        from akamai_cloud_docs_mcp.core import tools as core_tools

        core_tools.set_service(service)
        http.build_app(auto_sync=False)
        assert "validation is off" not in capsys.readouterr().err
