"""MCPServer construction and tool registration, shared by every transport.

The tool bodies do nothing but hand off to `core.tools`, render the payload
with `core.tools.render_result`, and mark a payload carrying an `error` key as
an `isError` tool result. Blocking work runs in a worker thread so a live page
fetch never stalls the event loop.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import anyio
import anyio.to_thread
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from .config import DEFAULT_K, MAX_ID_LEN, MAX_QUERY_LEN, MAX_SECTION_LEN, index_path
from .core import tools as core_tools
from .core.catalog import CatalogError
from .core.tools import (
    FETCH_ANNOTATIONS,
    FETCH_DESCRIPTION,
    FETCH_INPUT_SCHEMA,
    FETCH_TITLE,
    INSTRUCTIONS,
    SEARCH_ANNOTATIONS,
    SEARCH_DESCRIPTION,
    SEARCH_INPUT_SCHEMA,
    SEARCH_TITLE,
)
from .sync import SyncError

# Re-exported so `from .server import SEARCH_DESCRIPTION` keeps working.
__all__ = [
    "SERVER_NAME",
    "INSTRUCTIONS",
    "SEARCH_DESCRIPTION",
    "FETCH_DESCRIPTION",
    "FETCH_LIMITER",
    "SEARCH_LIMITER",
    "build_server",
]

SERVER_NAME = "akamai-cloud-docs"

# Separate thread pools. A live guide fetch can take up to the 30 second request
# timeout, and 40 of them fill anyio's default pool, so a search that takes 20
# milliseconds would queue behind them on a shared HTTP instance. Search is
# in-process and needs few threads. Shrinking the fetch pool to 16 measured a
# 45-fetch burst at 6.0 s instead of 4.0 s, so it stays at 40.
FETCH_LIMITER = anyio.CapacityLimiter(40)
SEARCH_LIMITER = anyio.CapacityLimiter(8)

STILL_BUILDING = (
    "The documentation index is still building (about 45 seconds on first run). "
    "Retry in 30 seconds."
)
NO_INDEX = "No documentation index. Run `akamai-cloud-docs-mcp sync`."
UNREADABLE_INDEX = (
    "The index file is not readable; run `akamai-cloud-docs-mcp sync` to rebuild it."
)


def _tool_result(payload: Any) -> CallToolResult:
    """One text block per result, rendered by `core.tools.render_result`.

    Doing this here rather than letting the SDK convert the return value keeps a
    list of search results in a single block instead of one block per result,
    and lets markdown go out as markdown. Every transport renders the same
    payload the same way, so a client sees identical text on stdio, HTTP and
    Akamai Functions.

    A payload carrying an `error` key comes back as an isError result.
    """
    is_error = isinstance(payload, dict) and "error" in payload
    text = core_tools.render_result(payload)
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


def _load_failure(exc: BaseException) -> str:
    """One sentence for a failed index load, with no filesystem path in it."""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return NO_INDEX
    if isinstance(exc, ValueError):
        return UNREADABLE_INDEX
    return (
        f"Building the index failed: {core_tools.short_reason(exc)}. "
        "Run `akamai-cloud-docs-mcp sync` in another shell, or restart the server, then retry."
    )


def _describe(schema: dict, name: str) -> str:
    return schema["properties"][name]["description"]


def build_server(*, auto_sync: bool = True, log_level: str | None = None) -> MCPServer:
    """Build the server with both tools registered.

    `log_level` is the SDK's root logging level (debug, info, warning, error).
    None keeps the SDK default. Below debug, the streamable HTTP module is held
    at WARNING because it logs "Terminating session: None" on every stateless
    request.
    """
    options: dict[str, Any] = {}
    if log_level is not None:
        options["log_level"] = log_level.upper()
    server = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        **options,
    )
    if log_level is not None:
        # The SDK calls logging.basicConfig, which does nothing once the root
        # logger has a handler, so the second server built in a process would
        # keep the first one's level.
        logging.getLogger().setLevel(log_level.upper())
    if (log_level or "").lower() != "debug":
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)

    def _service() -> core_tools.DocsService | dict:
        """The loaded service, or an error payload saying why there is none."""
        if auto_sync and not core_tools.is_ready() and not index_path().exists():
            # The stdio transport builds a missing index in a thread that holds
            # the service lock for the whole build. Answer now rather than
            # block a tool call past the client's timeout. A build that failed
            # is reported instead of retried inside the call; the stdio
            # builder retries on its own schedule.
            failure = core_tools.load_error()
            if failure is None:
                return {"error": STILL_BUILDING}
            return {"error": _load_failure(failure)}
        try:
            return core_tools.load_service(auto_sync=auto_sync, quiet=True)
        except (OSError, ValueError, SyncError, CatalogError) as exc:
            return {"error": _load_failure(exc)}

    @server.tool(
        title=SEARCH_TITLE,
        description=SEARCH_DESCRIPTION,
        annotations=ToolAnnotations.model_validate(SEARCH_ANNOTATIONS),
        structured_output=False,
    )
    async def search_docs(
        query: Annotated[
            str,
            Field(description=_describe(SEARCH_INPUT_SCHEMA, "query"), max_length=MAX_QUERY_LEN),
        ],
        k: Annotated[int, Field(description=_describe(SEARCH_INPUT_SCHEMA, "k"))] = DEFAULT_K,
    ) -> Any:
        def work():
            service = _service()
            if isinstance(service, dict):
                return service
            return service.search_docs(query, k)

        return _tool_result(await anyio.to_thread.run_sync(work, limiter=SEARCH_LIMITER))

    @server.tool(
        title=FETCH_TITLE,
        description=FETCH_DESCRIPTION,
        annotations=ToolAnnotations.model_validate(FETCH_ANNOTATIONS),
        structured_output=False,
    )
    async def fetch_doc(
        id: Annotated[  # noqa: A002
            str,
            Field(description=_describe(FETCH_INPUT_SCHEMA, "id"), max_length=MAX_ID_LEN),
        ],
        section: Annotated[
            str,
            Field(description=_describe(FETCH_INPUT_SCHEMA, "section"), max_length=MAX_SECTION_LEN),
        ] = "",
    ) -> Any:
        def work():
            service = _service()
            if isinstance(service, dict):
                return service
            return service.fetch_doc(id, section)

        return _tool_result(await anyio.to_thread.run_sync(work, limiter=FETCH_LIMITER))

    # pydantic titles the argument model and every field. They cost tokens on
    # every turn and say nothing, and mcp 2.x has no schema hook to leave them
    # out, so they are stripped after registration. A test asserts the result
    # equals the schema literals in core.tools.
    for tool in server._tool_manager.list_tools():
        tool.parameters.pop("title", None)
        for prop in tool.parameters.get("properties", {}).values():
            prop.pop("title", None)

    return server
