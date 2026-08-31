"""OpenAI Agents SDK with this server over stdio.

openai-agents accepts mcp 2.x, so it can share the repo venv:
    uv pip install openai-agents
    .venv/bin/akamai-cloud-docs-mcp sync
    OPENAI_API_KEY=... .venv/bin/python examples/python/openai_agents.py
Build the index first; the agent does not retry a "still building" answer.
"""

import asyncio
import os
import sys

from agents import Agent, Runner
from agents.mcp import MCPServerStdio

# After the PyPI release: {"command": "uvx", "args": ["akamai-cloud-docs-mcp"]}.
# The SDK starts the server with a minimal environment (HOME, PATH and a few
# others), so the cache location only reaches the server through "env".
PARAMS = {
    "command": os.environ.get("AKAMAI_DOCS_MCP_PYTHON", sys.executable),
    "args": ["-m", "akamai_cloud_docs_mcp"],
    "env": {k: os.environ[k] for k in ("AKAMAI_DOCS_MCP_CACHE_DIR", "XDG_CACHE_HOME") if k in os.environ},
}


async def main() -> None:
    # The server answers at once. The SDK's default tool timeout is 5 s; a live
    # fetch_doc can wait up to 30 s on techdocs, so raise it above that.
    async with MCPServerStdio(
        params=PARAMS, name="akamai-cloud-docs", client_session_timeout_seconds=90
    ) as docs:
        agent = Agent(
            name="Akamai docs helper",
            instructions="Answer from the documentation tools. Name the guide id you used.",
            mcp_servers=[docs],
            model=os.environ.get("MODEL_ID", "gpt-5-mini"),
        )
        result = await Runner.run(agent, "How do I resize a managed database cluster on Akamai Cloud?")
        print(result.final_output)


asyncio.run(main())
