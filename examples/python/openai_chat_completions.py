"""Use the hosted docs server from the OpenAI Chat Completions API.

Chat Completions has no MCP support, so this file runs the tool loop itself:
list the server's tools, hand them to the model as functions, and answer each
tool call with one JSON-RPC POST. The server is stateless, so plain HTTP is
enough; no MCP client library is needed. The same loop works against vLLM or
any OpenAI-compatible endpoint: set OPENAI_BASE_URL and OPENAI_MODEL.

    OPENAI_API_KEY=... python examples/python/openai_chat_completions.py
    OPENAI_BASE_URL=http://vllm.example:8000/v1 OPENAI_MODEL=Qwen/Qwen2.5-7B-Instruct \
        OPENAI_API_KEY=unused python examples/python/openai_chat_completions.py
"""

import json
import os
import sys
import urllib.request

from openai import OpenAI

URL = os.environ.get(
    "AKAMAI_DOCS_MCP_URL", "https://ea3aad34-8539-405f-be73-55201fdec736.fwf.app/mcp"
)
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
QUESTION = "How do I resize a managed database cluster? Name the guide id you used."


def rpc(method: str, params: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(URL, body, {"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        message = json.load(response)
    if "error" in message:
        raise RuntimeError(message["error"]["message"])
    return message["result"]


def main() -> None:
    tools = [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}}
        for t in rpc("tools/list", {})["tools"]
    ]
    client = OpenAI()
    messages = [{"role": "user", "content": QUESTION}]
    for _ in range(8):
        reply = client.chat.completions.create(model=MODEL, messages=messages, tools=tools).choices[0].message
        assistant = reply.model_dump(exclude_none=True)
        assistant.setdefault("content", "")  # some vLLM chat templates require the key
        messages.append(assistant)
        if not reply.tool_calls:
            print(reply.content)
            return
        for call in reply.tool_calls:
            result = rpc("tools/call", {"name": call.function.name, "arguments": json.loads(call.function.arguments)})
            text = "".join(block.get("text", "") for block in result["content"])
            print(f"  {call.function.name}({call.function.arguments[:70]}) -> {len(text)} bytes", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
    sys.exit("stopped after 8 rounds of tool calls")


if __name__ == "__main__":
    main()
