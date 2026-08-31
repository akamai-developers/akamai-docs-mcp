# Python examples

Five files, one per client library. Four start the server over stdio with
`python -m akamai_cloud_docs_mcp` from the interpreter in `AKAMAI_DOCS_MCP_PYTHON`
(default: the interpreter running the example); `claude_api_connector.py`
connects to the hosted instance instead and needs no server on this machine.
After the PyPI release the stdio line becomes
`command="uvx", args=["akamai-cloud-docs-mcp"]`.

| File | Library | Runs from | Reads |
|---|---|---|---|
| `mcp_client.py` | mcp 2.x, no agent | repo venv | `AKAMAI_DOCS_MCP_PYTHON` (optional) |
| `openai_agents.py` | openai-agents | repo venv, or the examples venv | `OPENAI_API_KEY`, `MODEL_ID` (default gpt-5-mini) |
| `strands_agent.py` | strands-agents | examples venv | `OPENAI_API_KEY`, `OPENAI_BASE_URL` (a vLLM endpoint), `MODEL_ID` |
| `langchain_agent.py` | langchain-mcp-adapters, langchain | examples venv | `OPENAI_API_KEY` |
| `claude_api_connector.py` | anthropic (MCP connector) | anywhere with `anthropic` | `ANTHROPIC_API_KEY`, `AKAMAI_DOCS_MCP_URL` (optional, default: the hosted instance) |
| `openai_chat_completions.py` | openai (Chat Completions, no agent library) | repo venv | `OPENAI_API_KEY`, `OPENAI_BASE_URL` (a vLLM endpoint), `OPENAI_MODEL` (default gpt-5-mini), `AKAMAI_DOCS_MCP_URL` (optional, default: the hosted instance) |

## One venv

`mcp_client.py` and `openai_agents.py` run from the repository venv. Two
commands set it up:

```bash
# 1. The server (repo root)
uv sync

# 2. Build the index once. Writes ~/.cache/akamai-cloud-docs-mcp/index.json,
#    about 45 seconds, longer when techdocs is slow.
.venv/bin/akamai-cloud-docs-mcp sync
```

Then:

```bash
.venv/bin/python examples/python/mcp_client.py

uv pip install openai-agents            # into .venv; openai-agents accepts mcp 2.x
export OPENAI_API_KEY=sk-...
.venv/bin/python examples/python/openai_agents.py
```

## Two venvs, for Strands and LangChain

The server needs `mcp>=2`. Strands Agents (1.54) and langchain-mcp-adapters
(0.3) pin `mcp<2`, so they cannot share a venv with the server. That is a
Python packaging conflict only: the wire protocol is the same, and an mcp 1.x
client talks to this server without trouble. Run those two examples from their
own venv and point `AKAMAI_DOCS_MCP_PYTHON` at the server's interpreter.

```bash
# 1 and 2 as above: uv sync, then .venv/bin/akamai-cloud-docs-mcp sync.
#    Use the server venv's entry point; the examples venv does not have the package.

# 3. The examples venv, next to it
uv venv examples/python/.venv --python 3.12
uv pip install --python examples/python/.venv/bin/python \
    "strands-agents[openai]" langchain-mcp-adapters langchain-openai langchain openai-agents

export AKAMAI_DOCS_MCP_PYTHON="$PWD/.venv/bin/python"
export OPENAI_API_KEY=sk-...
examples/python/.venv/bin/python examples/python/strands_agent.py
examples/python/.venv/bin/python examples/python/langchain_agent.py
examples/python/.venv/bin/python examples/python/openai_agents.py
```

For a vLLM endpoint, set `OPENAI_BASE_URL=http://<host>:8000/v1` and
`MODEL_ID` to the served model name before running `strands_agent.py`.

## Chat Completions, with the tool loop in your code

`openai_chat_completions.py` is for an app on the Chat Completions API, which
has no MCP support. It fetches `tools/list` from the hosted instance, hands
the two schemas to the model as function tools, and answers each tool call
with one JSON-RPC POST. The server is stateless, so plain `urllib` is enough;
no MCP client library. The same file runs against vLLM or any
OpenAI-compatible endpoint: set `OPENAI_BASE_URL` and `OPENAI_MODEL`. The
assistant message always carries a `content` key because some vLLM chat
templates reject one without it. The loop stops after 8 rounds.

```bash
export OPENAI_API_KEY=sk-...
.venv/bin/python examples/python/openai_chat_completions.py
#   search_docs({"query":"resize a database cluster"...) -> 1296 bytes
#   fetch_doc({"id":"aiven-manage-database","section":"9"}) -> 885 bytes
# Guide id used: aiven-manage-database (section 9).
```

For the Responses API there is nothing to write: pass
`{"type": "mcp", "server_label": "akamai-cloud-docs", "server_url": "https://ea3aad34-8539-405f-be73-55201fdec736.fwf.app/mcp", "require_approval": "never"}`
in `tools` and OpenAI's side makes the calls. Verified on 2026-08-28 with
gpt-5-mini: three tool calls, the same answer. vLLM does not implement that
tool type, which is why the Chat Completions file exists.

## The Claude API connector

`claude_api_connector.py` needs a public HTTP endpoint, not a local server,
because Anthropic's side makes the connection. It defaults to the hosted
instance, `https://ea3aad34-8539-405f-be73-55201fdec736.fwf.app/mcp` (public
preview, no auth, no SLA). Set `AKAMAI_DOCS_MCP_URL` to use another
deployment. Every run is a paid Claude API call.

```bash
uv pip install anthropic                # into .venv
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python examples/python/claude_api_connector.py
```

## The index and the cache directory

The stdio server answers `initialize` at once. With no index on disk, the first
tool call returns an `isError` result that says the index is still building,
while a daemon thread builds it (about 45 seconds, longer when techdocs is
slow). That thread dies when the example exits, and the agent examples exit as
soon as the model answers, so no index is ever written and every run fails the
same way. Run `sync` once first (step 2 above). `mcp_client.py` is the
exception: it polls `search_docs` every 10 seconds until the build finishes, so
it works without `sync`.

MCP stdio clients start the server with a minimal environment (HOME, PATH, and
a few others), so `AKAMAI_DOCS_MCP_CACHE_DIR` must be set in the client's `env`
field for the server, not exported in the shell. The four stdio examples pass
`AKAMAI_DOCS_MCP_CACHE_DIR` and `XDG_CACHE_HOME` through when they are set.
`sync` reads the same variables from the shell, so set them the same way for
both, or leave both unset and use the default `~/.cache/akamai-cloud-docs-mcp/`.

## Timeouts

The 90 second read timeout in `mcp_client.py`, `langchain_agent.py`, and
`openai_agents.py` bounds a live `fetch_doc`, which waits up to 30 seconds on
techdocs before it gives up. The mcp and langchain defaults are no timeout;
the openai-agents default is 5 seconds, too short for that fetch. Strands uses
its default 30 second startup timeout; the server answers in about a second.
