"""End to end through the MCP SDK, using its in-process client."""

from __future__ import annotations

import json
import logging
import time

import anyio
import pytest
from mcp.client import Client

from akamai_cloud_docs_mcp.core import tools as core_tools
from akamai_cloud_docs_mcp.core.catalog import FetchError
from akamai_cloud_docs_mcp.core.tools import (
    FETCH_INPUT_SCHEMA,
    INSTRUCTIONS,
    SEARCH_INPUT_SCHEMA,
)
from akamai_cloud_docs_mcp.server import (
    FETCH_DESCRIPTION,
    FETCH_LIMITER,
    SEARCH_DESCRIPTION,
    SEARCH_LIMITER,
    build_server,
)
from akamai_cloud_docs_mcp.sync import SyncError, write_index


def run_async(coroutine_factory):
    """Run one coroutine. Avoids taking a dependency on an asyncio plugin."""
    return anyio.run(coroutine_factory)


def _connect(auto_sync: bool):
    def call(operation):
        async def main():
            async with Client(build_server(auto_sync=auto_sync)) as client:
                return await operation(client)

        return run_async(main)

    return call


@pytest.fixture
def connected(service):
    """A client wired to the server, over an invented index."""
    core_tools.set_service(service)
    return _connect(auto_sync=False)


def text_of(result) -> str:
    return "".join(block.text for block in result.content if getattr(block, "text", None))


def _keys(schema) -> set[str]:
    """Every key at any depth of a JSON schema."""
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(schema, list):
        for value in schema:
            found |= _keys(value)
    return found


class TestListTools:
    def test_both_tools_are_advertised_search_first(self, connected):
        tools = connected(lambda client: client.list_tools())
        assert [tool.name for tool in tools.tools] == ["search_docs", "fetch_doc"]

    def test_input_schemas_expose_the_documented_arguments(self, connected):
        tools = {tool.name: tool for tool in connected(lambda c: c.list_tools()).tools}
        assert set(tools["search_docs"].input_schema["properties"]) == {"query", "k"}
        assert set(tools["fetch_doc"].input_schema["properties"]) == {"id", "section"}

    def test_only_the_required_arguments_are_required(self, connected):
        tools = {tool.name: tool for tool in connected(lambda c: c.list_tools()).tools}
        assert tools["search_docs"].input_schema.get("required") == ["query"]
        assert tools["fetch_doc"].input_schema.get("required") == ["id"]

    def test_advertised_schemas_equal_the_literals(self, connected):
        # The SDK builds its schema from the signatures; the Functions handler
        # and the eval harness use the literals. They must not drift apart.
        tools = {tool.name: tool for tool in connected(lambda c: c.list_tools()).tools}
        assert tools["search_docs"].input_schema == SEARCH_INPUT_SCHEMA
        assert tools["fetch_doc"].input_schema == FETCH_INPUT_SCHEMA

    def test_no_title_key_anywhere_in_the_schemas(self, connected):
        # pydantic titles are stripped after registration through a private
        # SDK attribute. This is the test that fails loudly if an upgrade
        # renames it.
        for tool in connected(lambda c: c.list_tools()).tools:
            assert "title" not in _keys(tool.input_schema), tool.name

    def test_every_parameter_is_described_and_bounded(self, connected):
        for tool in connected(lambda c: c.list_tools()).tools:
            for name, prop in tool.input_schema["properties"].items():
                assert prop["description"], f"{tool.name}.{name}"
                if prop["type"] == "string":
                    assert prop["maxLength"] > 0, f"{tool.name}.{name}"

    def test_titles(self, connected):
        tools = {tool.name: tool for tool in connected(lambda c: c.list_tools()).tools}
        assert tools["search_docs"].title == "Search Akamai Cloud docs"
        assert tools["fetch_doc"].title == "Read an Akamai Cloud doc"

    def test_both_tools_are_read_only_and_idempotent(self, connected):
        for tool in connected(lambda c: c.list_tools()).tools:
            assert tool.annotations.read_only_hint is True, tool.name
            assert tool.annotations.destructive_hint is False, tool.name
            assert tool.annotations.idempotent_hint is True, tool.name
            assert tool.annotations.open_world_hint is False, tool.name

    def test_instructions_reach_the_client(self, connected):
        assert connected(lambda client: _ready(client.instructions)) == INSTRUCTIONS

    def test_descriptions_stay_within_the_token_budget(self):
        # ~4 characters per token is the usual rule of thumb. The contract caps
        # both descriptions together at roughly 300 tokens.
        combined = len(SEARCH_DESCRIPTION) + len(FETCH_DESCRIPTION)
        assert combined / 4 < 300, f"descriptions are about {combined // 4} tokens"


async def _ready(value):
    return value


class TestCallSearchDocs:
    def test_returns_ranked_results(self, connected):
        result = connected(
            lambda client: client.call_tool("search_docs", {"query": "resize a widget"})
        )
        assert result.is_error is not True
        payload = json.loads(text_of(result))
        assert payload[0]["id"] == "resize-a-widget"
        assert set(payload[0]) == {"id", "title", "snippet"}

    def test_no_match_is_an_empty_list_not_an_error(self, connected):
        result = connected(
            lambda client: client.call_tool("search_docs", {"query": "zzz helm rollout"})
        )
        assert result.is_error is not True
        assert json.loads(text_of(result)) == []

    def test_k_is_honoured(self, connected):
        result = connected(
            lambda client: client.call_tool("search_docs", {"query": "widget", "k": 2})
        )
        assert len(json.loads(text_of(result))) == 2

    def test_k_out_of_range_is_clamped_not_rejected(self, connected):
        result = connected(
            lambda client: client.call_tool("search_docs", {"query": "widget", "k": 999})
        )
        assert result.is_error is not True
        assert 0 < len(json.loads(text_of(result))) <= 20


class TestCallFetchDoc:
    def test_table_of_contents(self, connected, big_guide_live):
        result = connected(
            lambda client: client.call_tool("fetch_doc", {"id": "create-a-widget"})
        )
        assert result.is_error is not True
        payload = json.loads(text_of(result))
        assert payload["title"] == "Create a widget"
        assert [section["id"] for section in payload["sections"]] == ["1", "2"]
        assert set(payload) == {"id", "title", "preamble", "sections"}

    def test_single_section_is_markdown_under_a_header(self, connected, big_guide_live):
        result = connected(
            lambda client: client.call_tool(
                "fetch_doc", {"id": "create-a-widget", "section": "2"}
            )
        )
        text = text_of(result)
        assert text.startswith(
            "# Create a widget > Verify the widget [create-a-widget #2] "
            "https://techdocs.akamai.com/cloud-computing/docs/create-a-widget\n\n"
        )
        assert "Run the checker" in text
        assert "Pick a size" not in text
        assert "\\n" not in text

    def test_small_guide_is_markdown_under_a_header(self, connected, small_guide_live):
        result = connected(lambda client: client.call_tool("fetch_doc", {"id": "resize-a-widget"}))
        text = text_of(result)
        assert text.split("\n", 1)[0].startswith("# ")
        assert "[resize-a-widget]" in text.split("\n", 1)[0]
        assert "Deleting a widget" in text

    def test_api_card_is_the_card_alone(self, connected):
        result = connected(lambda client: client.call_tool("fetch_doc", {"id": "POST /widgets"}))
        text = text_of(result)
        assert text.startswith("## POST /widgets")
        assert "linode-cli widgets create" in text
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)

    def test_fallback_note_is_the_second_line(self, connected, monkeypatch):
        def boom(slug, **kwargs):
            raise FetchError("simulated outage")

        monkeypatch.setattr(core_tools, "fetch_guide", boom)
        result = connected(lambda client: client.call_tool("fetch_doc", {"id": "sprocket-basics"}))
        assert result.is_error is not True
        lines = text_of(result).split("\n")
        assert lines[0].startswith("# ")
        assert "indexed copy" in lines[1]

    def test_section_is_optional(self, connected, big_guide_live):
        result = connected(lambda client: client.call_tool("fetch_doc", {"id": "create-a-widget"}))
        assert result.is_error is not True


class TestErrorResults:
    def test_unknown_id_is_an_is_error_result(self, connected):
        result = connected(lambda client: client.call_tool("fetch_doc", {"id": "resize-widget"}))
        assert result.is_error is True
        payload = json.loads(text_of(result))
        assert payload["did_you_mean"][0]["id"] == "resize-a-widget"

    def test_bad_section_is_an_is_error_result(self, connected, big_guide_live):
        result = connected(
            lambda client: client.call_tool(
                "fetch_doc", {"id": "create-a-widget", "section": "99"}
            )
        )
        assert result.is_error is True
        payload = json.loads(text_of(result))
        ids = [section["id"] for section in payload["valid_sections"]]
        assert ids == ["1", "2"]

    def test_rejected_url_does_not_leak_a_traceback(self, connected):
        result = connected(
            lambda client: client.call_tool("fetch_doc", {"id": "https://example.com/x.md"})
        )
        assert result.is_error is True
        body = text_of(result)
        assert "Traceback" not in body
        assert "File \"" not in body

    def test_missing_required_argument_is_reported(self, connected):
        result = connected(lambda client: client.call_tool("fetch_doc", {}))
        assert result.is_error is True


@pytest.fixture
def cold(cache_env):
    """No installed service and an empty cache directory."""
    core_tools.set_service(None)
    yield cache_env
    core_tools.set_service(None)


class TestIndexLoading:
    """What a client sees when the index is missing, broken, or still building."""

    def test_missing_index_without_auto_sync_is_one_sentence(self, cold):
        result = _connect(auto_sync=False)(
            lambda client: client.call_tool("search_docs", {"query": "widget"})
        )
        assert result.is_error is True
        text = text_of(result)
        assert "akamai-cloud-docs-mcp sync" in text
        assert str(cold) not in text
        assert "Errno" not in text
        assert "/" not in text.replace("akamai-cloud-docs-mcp", "")

    def test_corrupt_index_maps_to_the_rebuild_sentence(self, cold):
        cold.mkdir(parents=True)
        (cold / "index.json").write_text('{"docs": [', encoding="utf-8")
        result = _connect(auto_sync=False)(
            lambda client: client.call_tool("fetch_doc", {"id": "create-a-widget"})
        )
        assert result.is_error is True
        text = text_of(result)
        assert "rebuild" in text
        assert str(cold) not in text

    def test_missing_index_with_auto_sync_says_still_building(self, cold):
        # stdio starts the build in a thread. The tool call must answer now,
        # not block for the 45 second build.
        started = time.perf_counter()
        result = _connect(auto_sync=True)(
            lambda client: client.call_tool("search_docs", {"query": "widget"})
        )
        assert time.perf_counter() - started < 5
        assert result.is_error is True
        assert "still building" in text_of(result)

    def test_failed_background_build_is_reported_not_retried(self, cold, monkeypatch):
        def failing_sync(path=None, *, quiet=False, **kwargs):
            raise SyncError("no guide links parsed from https://techdocs.example.invalid/llms.txt")

        monkeypatch.setattr("akamai_cloud_docs_mcp.sync.sync", failing_sync)
        with pytest.raises(SyncError):
            core_tools.load_service(auto_sync=True, quiet=True)

        result = _connect(auto_sync=True)(
            lambda client: client.call_tool("search_docs", {"query": "widget"})
        )
        assert result.is_error is True
        text = text_of(result)
        assert text.startswith('{"error":"Building the index failed: no guide links')
        # The sentence names the way out; the tool call itself does not retry.
        assert "akamai-cloud-docs-mcp sync" in text
        assert text.endswith('then retry."}')
        assert str(cold) not in text

    def test_present_index_is_loaded_on_first_call(self, cold, index_payload):
        write_index(index_payload)
        result = _connect(auto_sync=True)(
            lambda client: client.call_tool("search_docs", {"query": "widget"})
        )
        assert result.is_error is not True
        assert json.loads(text_of(result))


class TestLimiters:
    def test_pool_sizes(self):
        assert FETCH_LIMITER.total_tokens == 40
        assert SEARCH_LIMITER.total_tokens == 8

    def test_search_is_not_queued_behind_slow_fetches(self, service, monkeypatch):
        core_tools.set_service(service)

        def slow_fetch(slug, **kwargs):
            time.sleep(0.5)
            return "# Slow page\n\nText.\n", "", ""

        monkeypatch.setattr(core_tools, "fetch_guide", slow_fetch)

        async def main():
            async with Client(build_server(auto_sync=False)) as client:
                timing = {}

                async def fetch():
                    await client.call_tool("fetch_doc", {"id": "create-a-widget"})

                async def search():
                    await anyio.sleep(0.1)
                    started = time.perf_counter()
                    result = await client.call_tool("search_docs", {"query": "widget"})
                    timing["search"] = time.perf_counter() - started
                    assert result.is_error is not True

                async with anyio.create_task_group() as group:
                    for _ in range(45):
                        group.start_soon(fetch)
                    group.start_soon(search)
                return timing["search"]

        assert run_async(main) < 1.0


@pytest.fixture
def root_level():
    level = logging.getLogger().level
    yield
    logging.getLogger().setLevel(level)


class TestLogging:
    def test_log_level_reaches_the_root_logger(self, root_level):
        build_server(auto_sync=False, log_level="warning")
        assert logging.getLogger().level == logging.WARNING

    def test_streamable_http_is_quiet_below_debug(self, root_level):
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.NOTSET)
        build_server(auto_sync=False)
        assert logging.getLogger("mcp.server.streamable_http").level == logging.WARNING

    def test_debug_leaves_streamable_http_alone(self, root_level):
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.NOTSET)
        build_server(auto_sync=False, log_level="debug")
        assert logging.getLogger("mcp.server.streamable_http").level == logging.NOTSET
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
