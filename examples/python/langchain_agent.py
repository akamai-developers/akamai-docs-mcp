"""LangChain (langchain-mcp-adapters) with this server over stdio.
langchain-mcp-adapters needs mcp<2, so run this from the examples venv (see README.md)
and set AKAMAI_DOCS_MCP_PYTHON to the interpreter that has akamai-cloud-docs-mcp installed.
Build the index first with `.venv/bin/akamai-cloud-docs-mcp sync`; the agent does
not retry a "still building" answer.
"""

import asyncio
import os
import sys
from datetime import timedelta

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

SERVERS = {
    "akamai-cloud-docs": {
        "transport": "stdio",
        # After the PyPI release: "command": "uvx", "args": ["akamai-cloud-docs-mcp"].
        "command": os.environ.get("AKAMAI_DOCS_MCP_PYTHON", sys.executable),
        "args": ["-m", "akamai_cloud_docs_mcp"],
        # The adapter starts the server with a minimal environment (HOME, PATH and a
        # few others), so the cache location only reaches the server through "env".
        "env": {k: os.environ[k] for k in ("AKAMAI_DOCS_MCP_CACHE_DIR", "XDG_CACHE_HOME") if k in os.environ},
        # The server answers at once. The read timeout (default: none) bounds a live
        # fetch_doc, which can wait up to 30 s on techdocs before it gives up.
        "session_kwargs": {"read_timeout_seconds": timedelta(seconds=90)},
    }
}
QUESTION = "How do I resize a managed database cluster on Akamai Cloud? Name the guide id."


async def main() -> None:
    # One session for the whole run; client.get_tools() would start a server per tool call.
    async with MultiServerMCPClient(SERVERS).session("akamai-cloud-docs") as session:
        tools = await load_mcp_tools(session)
        print("tools:", [tool.name for tool in tools])
        agent = create_agent(init_chat_model("openai:gpt-5-mini"), tools)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": QUESTION}]})
        print(result["messages"][-1].content)


asyncio.run(main())
