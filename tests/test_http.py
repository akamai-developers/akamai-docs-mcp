"""The HTTP transport at the wire level, through Starlette's test client.

Every request here carries a port in its Host header. The SDK's loopback
allowlist is `127.0.0.1:*`, so a portless Host would fail for the wrong reason.
"""

from __future__ import annotations

import json
import logging
import os

import anyio
import pytest
from starlette.testclient import TestClient

from akamai_cloud_docs_mcp import __version__, http
from akamai_cloud_docs_mcp.core import tools as core_tools
from akamai_cloud_docs_mcp.sync import write_index

BASE_URL = "http://127.0.0.1:8000"
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
MODERN = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def rpc(method: str, params: dict | None = None, id: int | None = 1) -> dict:  # noqa: A002
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if id is not None:
        message["id"] = id
    return message


@pytest.fixture
def make_client(service):
    """Build an app over the invented index and enter its lifespan."""
    core_tools.set_service(service)
    clients = []

    def build(**kwargs):
        app = http.build_app(auto_sync=False, **kwargs)
        client = TestClient(app, base_url=BASE_URL)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


class TestLegacyHandshake:
    def test_initialize_returns_capabilities_serverinfo_and_instructions(self, client):
        params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }
        response = client.post(http.DEFAULT_PATH, headers=HEADERS, json=rpc("initialize", params))
        assert response.status_code == 200
        result = response.json()["result"]
        assert "tools" in result["capabilities"]
        assert result["serverInfo"] == {"name": "akamai-cloud-docs", "version": __version__}
        assert result["instructions"]

    def test_tools_list_needs_no_initialize(self, client):
        response = client.post(http.DEFAULT_PATH, headers=HEADERS, json=rpc("tools/list"))
        assert response.status_code == 200
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert names == {"search_docs", "fetch_doc"}

    def test_notification_is_accepted_with_no_body(self, client):
        message = rpc("notifications/initialized", id=None)
        response = client.post(http.DEFAULT_PATH, headers=HEADERS, json=message)
        assert response.status_code == 202
        assert response.content == b""

    def test_batch_is_rejected(self, client):
        response = client.post(http.DEFAULT_PATH, headers=HEADERS, json=[rpc("tools/list")])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32602


class TestModernEnvelope:
    def _headers(self, version: str = MODERN) -> dict:
        return {
            **HEADERS,
            "MCP-Protocol-Version": version,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "search_docs",
        }

    def test_tools_call_carries_the_result_envelope(self, client):
        params = {
            "name": "search_docs",
            "arguments": {"query": "resize a widget"},
            "_meta": MODERN_META,
        }
        response = client.post(
            http.DEFAULT_PATH, headers=self._headers(), json=rpc("tools/call", params)
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert set(result) >= {"content", "isError", "resultType", "_meta"}
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])[0]["id"] == "resize-a-widget"

    def test_unknown_protocol_version_is_refused(self, client):
        # Header and envelope have to agree on the version, or the SDK reports
        # the mismatch instead of the unsupported version.
        meta = {**MODERN_META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}
        params = {"name": "search_docs", "arguments": {"query": "x"}, "_meta": meta}
        response = client.post(
            http.DEFAULT_PATH, headers=self._headers("2099-01-01"), json=rpc("tools/call", params)
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == -32022
        assert error["data"]["supported"] == [MODERN]


class TestHostAndOrigin:
    def test_loopback_host_is_accepted_by_default(self, client):
        response = client.post(http.DEFAULT_PATH, headers=HEADERS, json=rpc("tools/list"))
        assert response.status_code == 200

    def test_foreign_origin_is_refused(self, client):
        headers = {**HEADERS, "origin": "https://evil.example"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 403

    def test_proxy_host_is_refused_without_the_flag(self, client):
        headers = {**HEADERS, "host": "docs.example.com"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 421

    def test_proxy_host_is_accepted_with_the_flag(self, make_client):
        client = make_client(allowed_hosts=["docs.example.com"])
        headers = {**HEADERS, "host": "docs.example.com"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 200

    def test_the_flag_keeps_loopback_working(self, make_client):
        client = make_client(allowed_hosts=["docs.example.com"])
        response = client.post(http.DEFAULT_PATH, headers=HEADERS, json=rpc("tools/list"))
        assert response.status_code == 200

    def test_a_bare_name_does_not_match_a_host_with_a_port(self, make_client):
        client = make_client(allowed_hosts=["docs.example.com"])
        headers = {**HEADERS, "host": "docs.example.com:8443"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 421

    def test_allowed_origin_flag_accepts_that_origin(self, make_client):
        client = make_client(allowed_origins=["https://docs.example.com"])
        headers = {**HEADERS, "origin": "https://docs.example.com"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 200


class TestNonCanonicalLoopbackBind:
    """127.0.0.2 is loopback too, but the SDK would leave it unchecked."""

    def test_foreign_host_is_refused(self, make_client):
        client = make_client(host="127.0.0.2")
        headers = {**HEADERS, "host": "evil.example"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 421

    def test_foreign_origin_is_refused(self, make_client):
        client = make_client(host="127.0.0.2")
        headers = {**HEADERS, "origin": "https://evil.example"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 403

    def test_the_bind_address_itself_is_accepted(self, make_client):
        client = make_client(host="127.0.0.2")
        headers = {**HEADERS, "host": "127.0.0.2:8000"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 200

    def test_canonical_loopback_still_works(self, make_client):
        client = make_client(host="127.0.0.2")
        response = client.post(http.DEFAULT_PATH, headers=HEADERS, json=rpc("tools/list"))
        assert response.status_code == 200


class TestShorthandLoopbackBind:
    """127.1 and 127.000.000.001 bind 127.0.0.1, so they get the same checks.

    `ipaddress` refuses both spellings; the C library, and so uvicorn, expands
    them. Before, such a bind ran with the checks off under a warning that
    called the address not loopback.
    """

    SPELLINGS = ["127.1", "127.000.000.001"]

    @pytest.mark.parametrize("host", [*SPELLINGS, "0x7f000001"])
    def test_is_loopback_with_no_warning(self, host):
        assert http.is_loopback(host) is True
        assert http._validation_warning(host, [], []) == ""

    @pytest.mark.parametrize(
        "host", ["10.1", "1.1", "0", "0.0.0.0", "127.0.0.1 evil", "", "example.com"]
    )
    def test_shorthand_for_another_address_stays_unchecked(self, host):
        assert http.is_loopback(host) is False
        assert http.transport_security([], [], host=host) is None

    @pytest.mark.parametrize("host", SPELLINGS)
    def test_foreign_host_is_refused(self, make_client, host):
        client = make_client(host=host)
        headers = {**HEADERS, "host": "evil.example"}
        response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
        assert response.status_code == 421

    @pytest.mark.parametrize("host", SPELLINGS)
    def test_the_bind_spelling_and_the_canonical_address_are_accepted(self, make_client, host):
        # A local client copies the spelling from the command line into its
        # URL, so the Host it sends is that spelling with the port.
        client = make_client(host=host)
        for name in (host, "127.0.0.1"):
            headers = {**HEADERS, "host": f"{name}:8000"}
            response = client.post(http.DEFAULT_PATH, headers=headers, json=rpc("tools/list"))
            assert response.status_code == 200, name


class TestHealthAndMethods:
    def test_healthz_reports_the_index(self, client, service):
        response = client.get(http.HEALTH_PATH)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "name",
            "version",
            "documents",
            "index_built_at",
            "index_age_days",
            "endpoint",
        }
        assert body["documents"] == len(service.search_index)
        assert body["index_built_at"] == service.built_at
        assert body["index_age_days"] < 1
        assert body["endpoint"] == http.DEFAULT_PATH

    def test_healthz_follows_a_rebuilt_index(self, cache_env, index_payload):
        # A sync on a timer replaces the file; the running server picks the new
        # index up by mtime, and the probe must report the new build, not the
        # one loaded at startup.
        core_tools.set_service(None)
        path = write_index(index_payload)
        app = http.build_app(auto_sync=False)
        with TestClient(app, base_url=BASE_URL) as client:
            before = client.get(http.HEALTH_PATH).json()
            assert before["documents"] == len(index_payload["docs"])

            index_payload["docs"].append(
                {
                    "id": "paint-a-widget",
                    "kind": "guide",
                    "title": "Paint a widget",
                    "url": "https://techdocs.akamai.com/cloud-computing/docs/paint-a-widget.md",
                    "text": "# Paint a widget\n\nPaint dries slowly on a widget.\n",
                }
            )
            index_payload["built_at"] = "2030-01-01T00:00:00Z"
            index_payload["prebuilt"] = core_tools.SearchIndex(index_payload["docs"]).prebuilt()
            write_index(index_payload)
            later = os.stat(path).st_mtime + 10
            os.utime(path, (later, later))

            after = client.get(http.HEALTH_PATH).json()
        core_tools.set_service(None)
        assert after["documents"] == before["documents"] + 1
        assert after["index_built_at"] == "2030-01-01T00:00:00Z"

    def test_healthz_keeps_the_last_good_index_when_the_file_is_damaged(
        self, cache_env, index_payload
    ):
        core_tools.set_service(None)
        path = write_index(index_payload)
        app = http.build_app(auto_sync=False)
        with TestClient(app, base_url=BASE_URL) as client:
            before = client.get(http.HEALTH_PATH).json()
            path.write_text("{not json", encoding="utf-8")
            later = os.stat(path).st_mtime + 10
            os.utime(path, (later, later))
            response = client.get(http.HEALTH_PATH)
        core_tools.set_service(None)
        assert response.status_code == 200
        assert response.json()["index_built_at"] == before["index_built_at"]

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    def test_get_and_delete_on_the_mcp_path_are_405(self, client, method):
        response = client.request(method, http.DEFAULT_PATH, headers={"accept": "text/event-stream"})
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert response.text.count("\n") == 1

    @pytest.mark.parametrize("method", ["HEAD", "PUT", "PATCH", "OPTIONS"])
    def test_every_other_method_gets_the_same_allow_header(self, client, method):
        # The SDK answers these itself with `Allow: GET, POST, DELETE`, which
        # would tell a probe that GET is fine.
        response = client.request(method, http.DEFAULT_PATH)
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"

    def test_trailing_slash_is_guarded_too(self, client):
        response = client.get(http.DEFAULT_PATH + "/")
        assert response.status_code == 405

    def test_other_paths_pass_through(self, client):
        assert client.get("/nowhere").status_code == 404

    def test_custom_path_is_guarded(self, make_client):
        client = make_client(path="/docs-mcp")
        assert client.get("/docs-mcp").status_code == 405
        response = client.post("/docs-mcp", headers=HEADERS, json=rpc("tools/list"))
        assert response.status_code == 200

    def test_trailing_slash_on_the_configured_path_is_dropped(self, make_client):
        # The SDK would answer POST on the unslashed path with a redirect, and
        # healthz would report a path no client uses.
        client = make_client(path="/docs-mcp/")
        response = client.post("/docs-mcp", headers=HEADERS, json=rpc("tools/list"))
        assert response.status_code == 200
        assert client.get(http.HEALTH_PATH).json()["endpoint"] == "/docs-mcp"

    def test_path_without_a_leading_slash_is_refused(self, service):
        core_tools.set_service(service)
        with pytest.raises(ValueError, match="start with a slash"):
            http.build_app(auto_sync=False, path="docs-mcp")

    def test_guard_matches_under_an_asgi_root_path(self):
        # uvicorn puts root_path into scope["path"]; Starlette strips it
        # before routing, so the guard has to strip it too. Exercised at the
        # ASGI level because a miss would open the SDK's endless GET stream.
        reached = []

        async def inner(scope, receive, send):
            reached.append(scope["path"])

        sent = []

        async def send(message):
            sent.append(message)

        guard = http.MethodGuard(inner, "/mcp")
        scope = {"type": "http", "method": "GET", "path": "/api/mcp", "root_path": "/api"}
        anyio.run(guard, scope, None, send)
        assert reached == []
        assert sent[0]["status"] == 405

    def test_routes_work_under_an_asgi_root_path(self, service):
        core_tools.set_service(service)
        app = http.build_app(auto_sync=False)
        with TestClient(app, base_url=BASE_URL, root_path="/api") as client:
            assert client.get("/api" + http.HEALTH_PATH).status_code == 200
            response = client.post("/api/mcp", headers=HEADERS, json=rpc("tools/list"))
            assert response.status_code == 200
            refused = client.delete("/api/mcp")
            assert refused.status_code == 405
            assert refused.headers["allow"] == "POST"
            assert refused.text.count("\n") == 1


class TestStartup:
    def test_missing_index_is_a_startup_failure(self, cache_env):
        core_tools.set_service(None)
        with pytest.raises(SystemExit) as failure:
            http.build_app(auto_sync=False)
        assert "akamai-cloud-docs-mcp sync" in str(failure.value)
        assert str(cache_env) not in str(failure.value)

    def test_startup_line_names_the_document_count(self, service, capsys):
        core_tools.set_service(service)
        http.build_app(auto_sync=False)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert f"Loaded {len(service.search_index)} documents" in captured.err

    def test_old_index_prints_the_staleness_line(self, index_payload, capsys):
        stale = core_tools.DocsService({**index_payload, "built_at": "2020-01-01T00:00:00Z"})
        core_tools.set_service(stale)
        try:
            http.build_app(auto_sync=False)
        finally:
            core_tools.set_service(None)
        assert "sync" in capsys.readouterr().err

    def test_fresh_index_prints_no_staleness_line(self, service, capsys):
        core_tools.set_service(service)
        http.build_app(auto_sync=False)
        assert "sync" not in capsys.readouterr().err

    def test_auto_sync_announces_the_build_and_shows_progress(
        self, cache_env, service, monkeypatch, capsys
    ):
        seen = {}

        def fake_load(**kwargs):
            seen.update(kwargs)
            return service

        monkeypatch.setattr(core_tools, "load_service", fake_load)
        http.build_app(auto_sync=True)
        err = capsys.readouterr().err
        assert "Building it now" in err
        assert "45 seconds" in err
        assert seen["quiet"] is False, "an operator watching the log should see progress"

    def test_no_build_line_when_the_index_is_present(self, cache_env, service, monkeypatch, capsys):
        cache_env.mkdir(parents=True)
        (cache_env / "index.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(core_tools, "load_service", lambda **kwargs: service)
        http.build_app(auto_sync=True)
        assert "Building it now" not in capsys.readouterr().err


class TestLogLevel:
    @pytest.fixture
    def root_level(self):
        level = logging.getLogger().level
        yield
        logging.getLogger().setLevel(level)

    def test_build_app_leaves_the_host_logger_alone(self, service, root_level):
        # build_app is the import target for an external ASGI server, which
        # owns its own logging setup.
        core_tools.set_service(service)
        logging.getLogger().setLevel(logging.DEBUG)
        http.build_app(auto_sync=False)
        assert logging.getLogger().level == logging.DEBUG

    def test_an_explicit_level_still_applies(self, service, root_level):
        core_tools.set_service(service)
        logging.getLogger().setLevel(logging.DEBUG)
        http.build_app(auto_sync=False, log_level="warning")
        assert logging.getLogger().level == logging.WARNING

    def test_run_defaults_to_info(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(http, "build_app", lambda **kwargs: seen.update(kwargs) or "app")
        monkeypatch.setattr(http.uvicorn, "run", lambda app, **kwargs: seen.update(uvicorn=kwargs))
        http.run()
        assert seen["log_level"] == "info"
        assert seen["uvicorn"]["log_level"] == "info"
