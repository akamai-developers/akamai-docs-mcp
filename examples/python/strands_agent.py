"""Strands Agents with this server over stdio and an OpenAI-compatible model.

Strands needs mcp<2, so run this from the examples venv (see README.md) and set
AKAMAI_DOCS_MCP_PYTHON to the interpreter that has akamai-cloud-docs-mcp installed.
Build the index first with `.venv/bin/akamai-cloud-docs-mcp sync`; the agent does
not retry a "still building" answer.
Reads OPENAI_API_KEY, and OPENAI_BASE_URL for a vLLM endpoint such as http://<host>:8000/v1.
"""

import os
import sys

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models.openai import OpenAIModel
from strands.tools.mcp import MCPClient

# After the PyPI release: StdioServerParameters(command="uvx", args=["akamai-cloud-docs-mcp"]).
# stdio_client starts the server with a minimal environment (HOME, PATH and a few
# others), so the cache location only reaches the server through env= here.
SERVER = StdioServerParameters(
    command=os.environ.get("AKAMAI_DOCS_MCP_PYTHON", sys.executable),
    args=["-m", "akamai_cloud_docs_mcp"],
    env={k: os.environ[k] for k in ("AKAMAI_DOCS_MCP_CACHE_DIR", "XDG_CACHE_HOME") if k in os.environ},
)
# The server answers initialize in about a second, so the default 30 s startup timeout is enough.
docs = MCPClient(lambda: stdio_client(SERVER))

# vLLM accepts any api_key. base_url=None means api.openai.com.
model = OpenAIModel(
    client_args={
        "api_key": os.environ.get("OPENAI_API_KEY", "EMPTY"),
        "base_url": os.environ.get("OPENAI_BASE_URL"),
    },
    model_id=os.environ.get("MODEL_ID", "gpt-5-mini"),
)

with docs:
    agent = Agent(model=model, tools=docs.list_tools_sync(), callback_handler=None)
    result = agent("How do I resize a managed database cluster on Akamai Cloud? Name the guide id.")
    print(result)
