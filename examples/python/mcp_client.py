"""Raw MCP client (mcp 2.x): start the server over stdio, search, then fetch one document.

Build the index once:    .venv/bin/akamai-cloud-docs-mcp sync
Run from the repo venv:  .venv/bin/python examples/python/mcp_client.py
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# The interpreter that has akamai-cloud-docs-mcp installed. After the PyPI release
# this becomes StdioServerParameters(command="uvx", args=["akamai-cloud-docs-mcp"]).
# stdio_client starts the server with a minimal environment (HOME, PATH and a few
# others), so the cache location only reaches the server through env= here.
SERVER = StdioServerParameters(
    command=os.environ.get("AKAMAI_DOCS_MCP_PYTHON", sys.executable),
    args=["-m", "akamai_cloud_docs_mcp"],
    env={k: os.environ[k] for k in ("AKAMAI_DOCS_MCP_CACHE_DIR", "XDG_CACHE_HOME") if k in os.environ},
)
SEARCH = ("search_docs", {"query": "resize a database cluster", "k": 3})


async def main() -> None:
    # The server answers initialize at once. The read timeout (default: none) bounds
    # a live fetch_doc, which can wait up to 30 s on techdocs before it gives up.
    async with (
        stdio_client(SERVER) as (read, write),
        ClientSession(read, write, read_timeout_seconds=90) as session,
    ):
        init = await session.initialize()
        print(f"server: {init.server_info.name} {init.server_info.version}, protocol {init.protocol_version}")

        hits = await session.call_tool(*SEARCH)
        # With no index on disk the server builds one on a daemon thread and answers
        # "still building" until it is done (a minute, up to three when techdocs is
        # slow). Wait here: the thread dies with this process, so an early exit
        # leaves no index behind.
        for _ in range(18):
            if not (hits.is_error and "still building" in hits.content[0].text):
                break
            await asyncio.sleep(10)
            hits = await session.call_tool(*SEARCH)
        print("search_docs:", hits.content[0].text)
        if not hits.is_error:
            first = json.loads(hits.content[0].text)[0]["id"]
            toc = await session.call_tool("fetch_doc", {"id": first})
            print("fetch_doc:", toc.content[0].text[:600])


asyncio.run(main())
