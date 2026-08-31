"""Claude API MCP connector: Anthropic's side connects to this server and Claude calls its tools.

The default URL is the hosted instance on Akamai Functions: a public preview,
no auth, no SLA, limits subject to change. Set AKAMAI_DOCS_MCP_URL to point at
another deployment, such as your own Functions app. Needs ANTHROPIC_API_KEY,
and every run spends API credit:
    uv pip install anthropic
    .venv/bin/python examples/python/claude_api_connector.py
A stdio server cannot be used here; the connector only reaches URLs.
"""

import os
import sys

import anthropic

DEFAULT_URL = "https://ea3aad34-8539-405f-be73-55201fdec736.fwf.app/mcp"
URL = os.environ.get("AKAMAI_DOCS_MCP_URL") or DEFAULT_URL
if not os.environ.get("ANTHROPIC_API_KEY"):
    sys.exit("Set ANTHROPIC_API_KEY.")

# Both parameters are required together: mcp_servers names the server, and an
# mcp_toolset entry references that name. The connector is behind a beta flag.
client = anthropic.Anthropic()
response = client.beta.messages.create(
    model="claude-opus-5",
    max_tokens=1024,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{"type": "url", "url": URL, "name": "akamai-cloud-docs"}],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "akamai-cloud-docs"}],
    messages=[
        {"role": "user", "content": "How do I resize a managed database cluster on Akamai Cloud?"}
    ],
)
if response.stop_reason == "refusal":
    sys.exit("The request was refused by a safety classifier.")
for block in response.content:
    if block.type == "text":
        print(block.text)
