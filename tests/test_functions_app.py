"""The Akamai Functions entry point, driven through a stand-in Spin runtime.

`functions/app.py` imports `spin_sdk`, which only exists inside the Wasm
component, so this module installs a stand-in first and then imports the app by
path. The stand-in mirrors the real signatures, checked against spin-sdk 3.4.1:

    Store.get/set/delete/exists/get_keys
    Request(method, uri, headers, body)
    Response(status, headers, body)
    variables.get(key)
    http.send(request) -> Response
    wit.imports.outgoing_handler.handle(request, options)
    wit.imports.types.RequestOptions with set_connect_timeout,
    set_first_byte_timeout and set_between_bytes_timeout
    wit.types.Err, the wrapper the SDK raises around a wasi error value

Every document here is invented, and nothing touches the network. The one
test that drives the handler through the real MCP SDK client does so over an
in-process ASGI shim, so no socket opens.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import anyio
import pytest

from akamai_cloud_docs_mcp import __version__
from akamai_cloud_docs_mcp.config import DEFAULT_K
from akamai_cloud_docs_mcp.core import tools as core_tools

APP_PATH = Path(__file__).resolve().parents[1] / "functions" / "app.py"

GENERATION_A = "a" * 32
GENERATION_B = "b" * 32
GENERATION_C = "c" * 32
TOKEN = "correct-horse"
AUTH = {"authorization": f"Bearer {TOKEN}"}


# --- the stand-in Spin runtime -----------------------------------------------


class FakeStore:
    """An in-memory key value store with the Spin Store surface."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.writes: list[tuple[str, str]] = []  # (operation, key), in order
        self.reads = 0

    def get(self, key: str) -> bytes | None:
        self.reads += 1
        return self.data.get(key)

    def set(self, key: str, value: bytes) -> None:
        self.data[key] = value
        self.writes.append(("set", key))

    def delete(self, key: str) -> None:
        self.data.pop(key, None)
        self.writes.append(("delete", key))

    def exists(self, key: str) -> bool:
        return key in self.data

    def get_keys(self) -> list[str]:
        return sorted(self.data)


class FakeRequest:
    def __init__(self, method: str, uri: str, headers: dict, body: bytes | None) -> None:
        self.method = method
        self.uri = uri
        self.headers = headers
        self.body = body


class FakeResponse:
    def __init__(self, status: int, headers: dict, body: bytes | None) -> None:
        self.status = status
        self.headers = headers
        self.body = body


class FakeIncomingHandler:
    """Stands in for the SDK base class the app subclasses."""


class FakeErr(Exception):
    """`spin_sdk.wit.types.Err`: what the SDK raises around a wasi error value.

    The real one is `@dataclass(frozen=True) class Err(Generic[E], Exception)`
    with one field, `value`. `http.send` raises it with an `ErrorCode_*`
    variant inside; a `RequestOptions` setter raises it with `None` inside.
    """

    def __init__(self, value) -> None:
        super().__init__(value)
        self.value = value


class FakeRequestOptions:
    """Records the timeouts the app sets. `refuse` names the phases whose setter raises."""

    refuse: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.connect = None
        self.first_byte = None
        self.between_bytes = None

    def _set(self, phase: str, duration) -> None:
        if phase in self.refuse:
            raise FakeErr(None)
        setattr(self, phase, duration)

    def set_connect_timeout(self, duration) -> None:
        self._set("connect", duration)

    def set_first_byte_timeout(self, duration) -> None:
        self._set("first_byte", duration)

    def set_between_bytes_timeout(self, duration) -> None:
        self._set("between_bytes", duration)


def _install_spin_stub() -> dict:
    """Put a fake `spin_sdk` on sys.modules and hand back its control surface."""
    state: dict = {
        "store": FakeStore(),
        "variables": {},
        "sent": [],
        "responder": None,
        "handled": [],
    }

    spin_sdk = ModuleType("spin_sdk")
    http_mod = ModuleType("spin_sdk.http")
    kv_mod = ModuleType("spin_sdk.key_value")
    var_mod = ModuleType("spin_sdk.variables")
    wit_mod = ModuleType("spin_sdk.wit")
    wit_types_mod = ModuleType("spin_sdk.wit.types")
    imports_mod = ModuleType("spin_sdk.wit.imports")
    handler_mod = ModuleType("spin_sdk.wit.imports.outgoing_handler")
    types_mod = ModuleType("spin_sdk.wit.imports.types")

    http_mod.Request = FakeRequest
    http_mod.Response = FakeResponse
    http_mod.IncomingHandler = FakeIncomingHandler

    def send(request):
        state["sent"].append(request)
        responder = state["responder"]
        if responder is None:
            raise AssertionError("the app made an unexpected outbound request")
        return responder(request)

    def handle(request, options):
        state["handled"].append((request, options))
        return "future"

    http_mod.send = send
    kv_mod.open_default = lambda: state["store"]
    var_mod.get = lambda key: state["variables"].get(key)
    handler_mod.handle = handle
    types_mod.RequestOptions = FakeRequestOptions
    wit_types_mod.Err = FakeErr

    spin_sdk.http = http_mod
    spin_sdk.key_value = kv_mod
    spin_sdk.variables = var_mod
    spin_sdk.wit = wit_mod
    wit_mod.types = wit_types_mod
    wit_mod.imports = imports_mod
    imports_mod.outgoing_handler = handler_mod
    imports_mod.types = types_mod

    modules = {
        "spin_sdk": spin_sdk,
        "spin_sdk.http": http_mod,
        "spin_sdk.key_value": kv_mod,
        "spin_sdk.variables": var_mod,
        "spin_sdk.wit": wit_mod,
        "spin_sdk.wit.types": wit_types_mod,
        "spin_sdk.wit.imports": imports_mod,
        "spin_sdk.wit.imports.outgoing_handler": handler_mod,
        "spin_sdk.wit.imports.types": types_mod,
    }
    sys.modules.update(modules)
    state["modules"] = list(modules)
    return state


@pytest.fixture
def spin(monkeypatch):
    """Import `functions/app.py` fresh against a stand-in Spin runtime."""
    for name in list(sys.modules):
        if name == "spin_sdk" or name.startswith("spin_sdk."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    state = _install_spin_stub()
    # Importing the app replaces the guide fetcher for the whole process.
    # Put the real one back so later tests see the fetcher they expect.
    real_fetch_guide = core_tools.fetch_guide

    spec = importlib.util.spec_from_file_location("functions_app_under_test", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "functions_app_under_test", module)
    spec.loader.exec_module(module)

    state["app"] = module
    yield state

    module._service = None
    module._generation = ""
    core_tools.fetch_guide = real_fetch_guide
    FakeRequestOptions.refuse = ()
    for name in state["modules"]:
        sys.modules.pop(name, None)


def meta_for(raw: bytes, chunks: int, generation: str, **extra) -> dict:
    return {
        "generation": generation,
        "chunks": chunks,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        **extra,
    }


def split(raw: bytes, chunk_bytes: int) -> list[bytes]:
    return [raw[start : start + chunk_bytes] for start in range(0, len(raw), chunk_bytes)]


def publish(spin, payload: dict, chunk_bytes: int = 256, generation: str = GENERATION_A) -> dict:
    """Push an index through the store functions the way `sync --push` does."""
    app = spin["app"]
    raw = json.dumps(payload).encode("utf-8")
    blobs = split(raw, chunk_bytes)
    for number, blob in enumerate(blobs):
        app.store_chunk(spin["store"], generation, number, blob)
    meta = meta_for(raw, len(blobs), generation, built_at=payload.get("built_at", ""))
    app.finish_push(spin["store"], meta)
    return meta


def admin(spin, body, *, headers: dict | None = None):
    """POST one body to /admin/sync with the right token unless told otherwise."""
    spin["variables"]["sync_token"] = TOKEN
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = FakeRequest("POST", "/admin/sync", AUTH if headers is None else headers, raw)
    return spin["app"].handle_admin_sync(request)


def push_via_admin(spin, payload: dict, generation: str, chunk_bytes: int = 256, finish=True):
    """A whole push over the admin route: every chunk, then finish."""
    raw = json.dumps(payload).encode("utf-8")
    blobs = split(raw, chunk_bytes)
    for number, blob in enumerate(blobs):
        body = {"generation": generation, "chunk": number, "data": base64.b64encode(blob).decode()}
        assert admin(spin, body).status == 200
    meta = meta_for(raw, len(blobs), generation)
    if finish:
        response = admin(spin, {"finish": meta})
        assert response.status == 200, response.body
    return meta


def rpc(spin, method: str, params: dict | None = None, request_id: int = 1):
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
    status, raw = spin["app"].handle_rpc(body.encode())
    return status, json.loads(raw)


def call(spin, name: str, arguments: dict):
    """One tools/call. Returns (status, message)."""
    return rpc(spin, "tools/call", {"name": name, "arguments": arguments})


def result_text(message: dict) -> str:
    return message["result"]["content"][0]["text"]


def handle(spin, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    return spin["app"].IncomingHandler().handle_request(FakeRequest(method, path, headers or {}, body))


# --- chunk storage -----------------------------------------------------------


def test_publish_then_load_round_trips(spin, index_payload):
    publish(spin, index_payload)
    service = spin["app"].load_service()
    hits = service.search_docs("widget", 5)
    assert [hit["id"] for hit in hits]
    assert spin["store"].get("index:meta") is not None


def test_chunks_live_under_their_generation(spin, index_payload):
    publish(spin, index_payload, generation=GENERATION_A)
    keys = spin["store"].get_keys()
    assert all(key == "index:meta" or key.startswith(f"index:{GENERATION_A}:chunk:") for key in keys)


def test_load_without_an_index_teaches_the_operator(spin):
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    assert "sync --push" in str(caught.value)


def test_load_rejects_a_short_read(spin, index_payload):
    meta = publish(spin, index_payload)
    spin["store"].data["index:meta"] = json.dumps({**meta, "bytes": meta["bytes"] + 99}).encode()
    spin["app"]._service = None
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    assert "incomplete" in str(caught.value)


def test_load_reads_only_the_published_generation(spin, index_payload):
    """Chunks of another generation never enter the payload, whatever they hold."""
    publish(spin, index_payload, generation=GENERATION_A)
    spin["app"].store_chunk(spin["store"], GENERATION_B, 0, b"{garbage")
    spin["app"]._service = None
    assert spin["app"].load_service().search_docs("widget", 3)


def test_load_rejects_a_spliced_payload(spin, index_payload):
    """Same length, different bytes: only the sha256 can tell."""
    publish(spin, index_payload, chunk_bytes=256)
    store = spin["store"]
    key = f"index:{GENERATION_A}:chunk:0"
    store.data[key] = b"x" * len(store.data[key])
    spin["app"]._service = None
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    assert "checksum" in str(caught.value)


def test_load_rejects_a_meta_with_the_wrong_sha256(spin, index_payload):
    meta = publish(spin, index_payload)
    spin["store"].data["index:meta"] = json.dumps({**meta, "sha256": "0" * 64}).encode()
    spin["app"]._service = None
    with pytest.raises(LookupError):
        spin["app"].load_service()


@pytest.mark.parametrize(
    "bad",
    [
        {"chunks": 10**9},
        {"chunks": True},
        {"chunks": "4"},
        {"chunks": 0},
        {"bytes": -1},
        {"bytes": "12"},
        {"bytes": True},
        {"generation": "not hex"},
        {"generation": 12},
        {"sha256": "short"},
        {"sha256": None},
    ],
)
def test_load_rejects_bad_meta_types_before_reading_a_chunk(spin, index_payload, bad):
    """A meta claiming 10**9 chunks used to cost 10**9 reads. Now it costs one."""
    meta = publish(spin, index_payload)
    store = spin["store"]
    store.data["index:meta"] = json.dumps({**meta, **bad}).encode()
    spin["app"]._service = None
    store.reads = 0
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    assert "Push the index again" in str(caught.value)
    assert store.reads == 1


def test_load_rejects_unreadable_meta(spin):
    spin["store"].data["index:meta"] = b"{not json"
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    assert "not valid JSON" in str(caught.value)


def test_load_rejects_a_payload_that_is_not_an_index(spin):
    """The checksum matches, the JSON parses, the shape is wrong: still a 503."""
    raw = b'[1, 2, 3]'
    spin["app"].store_chunk(spin["store"], GENERATION_A, 0, raw)
    spin["app"].finish_push(spin["store"], meta_for(raw, 1, GENERATION_A))
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    assert "not readable" in str(caught.value)


def without_block(index_payload: dict, schema: int | None = None) -> dict:
    """The index as a sync before the prebuilt block would have pushed it."""
    older = {key: value for key, value in index_payload.items() if key != "prebuilt"}
    if schema is not None:
        older["schema"] = schema
    return older


def test_load_refuses_an_index_from_an_older_sync(spin, index_payload):
    """Building the search index costs about 0.9 s per request here, so an
    index without the block is a 503 that names the sync which writes one."""
    publish(spin, without_block(index_payload, schema=1))
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    message = str(caught.value)
    assert "index schema 1" in message
    assert "sync --push" in message
    assert __version__ in message


def test_an_index_without_the_block_is_503_wherever_the_index_is_needed(spin, index_payload):
    publish(spin, without_block(index_payload))
    status, message = call(spin, "search_docs", {"query": "widget"})
    assert status == 503
    assert "no prebuilt search index" in message["error"]["message"]
    response = handle(spin, "GET", "/")
    assert response.status == 503
    assert "sync --push" in json.loads(response.body)["error"]
    # tools/list never touches the index, so it keeps answering.
    assert rpc(spin, "tools/list")[0] == 200


def test_load_takes_the_prebuilt_path(spin, index_payload, monkeypatch):
    """Nothing is tokenized at load: `_build` is never called."""
    publish(spin, index_payload)

    def never(self):
        raise AssertionError("the search index was built at load")

    monkeypatch.setattr(core_tools.SearchIndex, "_build", never)
    service = spin["app"].load_service()
    assert service.search_docs("widget", 3)
    assert service.fetch_doc("POST /widgets")["kind"] == "api"


def test_a_damaged_prebuilt_block_is_not_readable(spin, index_payload):
    damaged = {**index_payload, "prebuilt": {**index_payload["prebuilt"], "lengths": []}}
    publish(spin, damaged)
    with pytest.raises(LookupError) as caught:
        spin["app"].load_service()
    assert "not readable" in str(caught.value)


def test_finish_push_clears_every_chunk_of_other_generations(spin, index_payload):
    """A shrinking index used to strand chunks past a 32 key window."""
    app, store = spin["app"], spin["store"]
    for number in range(50):
        app.store_chunk(store, GENERATION_A, number, b"stale")
    app.finish_push(store, meta_for(b"stale" * 50, 50, GENERATION_A))

    publish(spin, index_payload, generation=GENERATION_B)

    leftover = [key for key in store.get_keys() if GENERATION_A in key]
    assert leftover == []


def test_finish_push_clears_the_layout_before_generations(spin, index_payload):
    """Keys from a v1 deployment, `index:chunk:N`, go too."""
    store = spin["store"]
    store.data["index:chunk:0"] = b"old layout"
    publish(spin, index_payload)
    assert "index:chunk:0" not in store.data


def test_finish_push_publishes_before_it_deletes(spin, index_payload):
    """Meta first, so a concurrent reader never sees it point at a deleted chunk."""
    app, store = spin["app"], spin["store"]
    publish(spin, index_payload, generation=GENERATION_A)
    store.writes.clear()

    publish(spin, index_payload, generation=GENERATION_B)

    operations = [entry for entry in store.writes if entry[0] == "delete" or entry[1] == "index:meta"]
    assert operations[0] == ("set", "index:meta")
    assert ("delete", f"index:{GENERATION_A}:chunk:0") in operations
    assert app.load_service()


def test_finish_push_leaves_a_generation_no_meta_has_named(spin, index_payload):
    """Chunks under a generation no meta named may belong to a push in flight,
    so they stay. A push that died before its finish leaves them behind too:
    the store cannot tell the two apart, and deleting the wrong one costs
    every reader a 503 (the overlapping finishes test below)."""
    app, store = spin["app"], spin["store"]
    for number in range(3):
        app.store_chunk(store, GENERATION_B, number, b"in flight, or orphaned")

    publish(spin, index_payload, generation=GENERATION_A)

    assert [key for key in store.get_keys() if GENERATION_B in key] == [
        f"index:{GENERATION_B}:chunk:{number}" for number in range(3)
    ]


def test_finish_push_sweeps_only_the_generation_it_replaces(spin, index_payload):
    """Three generations in the store: the published one goes, the unnamed one
    stays, the layout before generations goes."""
    app, store = spin["app"], spin["store"]
    publish(spin, index_payload, generation=GENERATION_A)
    app.store_chunk(store, GENERATION_B, 0, b"in flight")
    store.data["index:chunk:0"] = b"old layout"

    publish(spin, index_payload, generation=GENERATION_C)

    keys = store.get_keys()
    assert not any(GENERATION_A in key for key in keys)
    assert f"index:{GENERATION_B}:chunk:0" in keys
    assert "index:chunk:0" not in keys
    assert json.loads(store.get("index:meta"))["generation"] == GENERATION_C


def test_finishing_the_published_generation_again_keeps_its_chunks(spin, index_payload):
    """A `--push-only` retry after a finish that did land must not sweep itself."""
    app = spin["app"]
    meta = publish(spin, index_payload, generation=GENERATION_A)
    assert app.finish_push(spin["store"], meta) == ""
    assert app.load_service().search_docs("widget", 3)


def test_two_overlapping_finishes_leave_a_complete_index(spin, index_payload):
    """Push A finishes between push B's verification reads and B's meta write.

    Spin runs requests concurrently, so this interleaving is real. A sweep
    that cleared every generation but its own deleted B's chunks here, and B
    then published a meta with nothing behind it: both clients saw 200, and
    every reader saw 503 until the next push.
    """
    app, store = spin["app"], spin["store"]
    raw = json.dumps(index_payload).encode("utf-8")
    blobs = split(raw, 256)
    for generation in (GENERATION_A, GENERATION_B):
        for number, blob in enumerate(blobs):
            app.store_chunk(store, generation, number, blob)
    meta_a = meta_for(raw, len(blobs), GENERATION_A)
    meta_b = meta_for(raw, len(blobs), GENERATION_B)

    real_set = store.set
    fired = False

    def set_with_a_finish_in_between(key, value):
        nonlocal fired
        if key == app.META_KEY and not fired:
            fired = True
            app.finish_push(store, meta_a)
        real_set(key, value)

    store.set = set_with_a_finish_in_between
    app.finish_push(store, meta_b)

    assert json.loads(store.get("index:meta"))["generation"] == GENERATION_B
    assert all(store.exists(f"index:{GENERATION_B}:chunk:{n}") for n in range(len(blobs)))
    app._service = None
    assert app.load_service().search_docs("widget", 3)


def test_finish_push_survives_unreadable_meta(spin, index_payload):
    spin["store"].data["index:meta"] = b"{not json"
    meta = publish(spin, index_payload)
    assert json.loads(spin["store"].get("index:meta")) == meta


def test_finish_push_refuses_a_missing_chunk(spin):
    app, store = spin["app"], spin["store"]
    raw = b"0123456789"
    app.store_chunk(store, GENERATION_A, 0, raw[:5])
    with pytest.raises(app.PushError) as caught:
        app.finish_push(store, meta_for(raw, 2, GENERATION_A))
    assert "never received" in str(caught.value)
    assert store.get("index:meta") is None


def test_finish_push_refuses_a_byte_count_that_does_not_add_up(spin):
    app, store = spin["app"], spin["store"]
    raw = b"0123456789"
    app.store_chunk(store, GENERATION_A, 0, raw)
    with pytest.raises(app.PushError):
        app.finish_push(store, {**meta_for(raw, 1, GENERATION_A), "bytes": 11})
    assert store.get("index:meta") is None


def test_finish_push_refuses_a_wrong_sha256(spin):
    app, store = spin["app"], spin["store"]
    raw = b"0123456789"
    app.store_chunk(store, GENERATION_A, 0, raw)
    with pytest.raises(app.PushError):
        app.finish_push(store, {**meta_for(raw, 1, GENERATION_A), "sha256": "0" * 64})
    assert store.get("index:meta") is None


def test_publishing_drops_the_cached_service(spin, index_payload):
    publish(spin, index_payload, generation=GENERATION_A)
    first = spin["app"].load_service()
    publish(spin, index_payload, generation=GENERATION_B)
    assert spin["app"].load_service() is not first


# --- JSON-RPC ----------------------------------------------------------------


def test_tools_list_matches_the_shipped_pair(spin):
    status, message = rpc(spin, "tools/list")
    assert status == 200
    tools = message["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["search_docs", "fetch_doc"]
    assert tools[0]["inputSchema"]["required"] == ["query"]
    assert all(tool["description"].strip() for tool in tools)


def test_tools_list_carries_titles_and_read_only_annotations(spin):
    for tool in rpc(spin, "tools/list")[1]["result"]["tools"]:
        assert tool["title"]
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }


def _sdk_tools() -> list[dict]:
    from mcp.client import Client

    from akamai_cloud_docs_mcp.server import build_server

    async def main():
        async with Client(build_server(auto_sync=False)) as client:
            return (await client.list_tools()).tools

    return [tool.model_dump(by_alias=True, exclude_none=True) for tool in anyio.run(main)]


def test_tools_list_equals_what_the_sdk_server_advertises(spin):
    """Two transports, one source. Name, title, description, schema, annotations."""
    keys = ("name", "title", "description", "inputSchema", "annotations")
    ours = rpc(spin, "tools/list")[1]["result"]["tools"]
    theirs = _sdk_tools()
    assert [set(tool) for tool in ours] == [set(keys), set(keys)]
    assert ours == [{key: tool[key] for key in keys} for tool in theirs]


def test_tools_call_search_returns_a_tool_result(spin, index_payload):
    publish(spin, index_payload)
    status, message = call(spin, "search_docs", {"query": "widget"})
    assert status == 200
    result = message["result"]
    assert result["isError"] is False
    assert json.loads(result_text(message))


def test_tools_call_marks_a_teaching_error(spin, index_payload):
    publish(spin, index_payload)
    _, message = call(spin, "fetch_doc", {"id": "no-such-page"})
    result = message["result"]
    assert result["isError"] is True
    payload = json.loads(result_text(message))
    assert payload["error"]
    assert payload["did_you_mean"]


def test_tools_call_without_an_index_answers_503(spin):
    status, message = call(spin, "search_docs", {"query": "x"})
    assert status == 503
    assert message["error"]["code"] == -32000


def test_unknown_tool_is_an_error_result_not_a_crash(spin, index_payload):
    publish(spin, index_payload)
    _, message = call(spin, "drop_tables", {})
    assert message["result"]["isError"] is True


def test_api_card_is_rendered_as_the_card_alone(spin, index_payload):
    publish(spin, index_payload)
    _, message = call(spin, "fetch_doc", {"id": "POST /widgets"})
    text = result_text(message)
    assert text.startswith("## POST /widgets")
    assert "\\n" not in text
    payload = spin["app"].load_service().fetch_doc("POST /widgets")
    assert text == core_tools.render_result(payload)


def test_section_is_markdown_under_one_header(spin, index_payload, big_guide_body):
    publish(spin, index_payload)
    spin["responder"] = lambda request: FakeResponse(200, {}, big_guide_body.encode())
    _, message = call(spin, "fetch_doc", {"id": "create-a-widget", "section": "1"})
    text = result_text(message)
    assert text.startswith("# Create a widget > ")
    assert "[create-a-widget #1]" in text.splitlines()[0]
    assert "\\n" not in text
    payload = spin["app"].load_service().fetch_doc("create-a-widget", "1")
    assert text == core_tools.render_result(payload)


def test_table_of_contents_stays_json(spin, index_payload, big_guide_body):
    publish(spin, index_payload)
    spin["responder"] = lambda request: FakeResponse(200, {}, big_guide_body.encode())
    _, message = call(spin, "fetch_doc", {"id": "create-a-widget"})
    toc = json.loads(result_text(message))
    assert toc["sections"]


def test_malformed_body_is_a_parse_error(spin):
    status, raw = spin["app"].handle_rpc(b"{not json")
    assert status == 400
    assert json.loads(raw)["error"]["code"] == -32700


def test_an_integer_too_long_to_parse_is_a_parse_error(spin):
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_docs","arguments":{"query":"x","k":' + b"1" * 5000 + b"}}}"
    status, raw = spin["app"].handle_rpc(body)
    assert status == 400
    assert json.loads(raw)["error"]["code"] == -32700


@pytest.mark.parametrize("body", [b"[]", b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', b'"hi"', b"null", b"42"])
def test_anything_but_one_object_is_an_invalid_request(spin, body):
    status, raw = spin["app"].handle_rpc(body)
    assert status == 400
    assert json.loads(raw)["error"]["code"] == -32600


def test_a_notification_is_accepted_without_a_body(spin):
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    status, raw = spin["app"].handle_rpc(body)
    assert status == 202
    assert raw == b""


def test_non_object_params_are_invalid_params_with_the_id_echoed(spin):
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": "x"}).encode()
    status, raw = spin["app"].handle_rpc(body)
    message = json.loads(raw)
    assert status == 200
    assert message["id"] == 7
    assert message["error"]["code"] == -32602


def test_non_object_arguments_are_invalid_params_with_the_id_echoed(spin, index_payload):
    publish(spin, index_payload)
    status, message = rpc(spin, "tools/call", {"name": "search_docs", "arguments": "nope"}, request_id=9)
    assert status == 200
    assert message["id"] == 9
    assert message["error"]["code"] == -32602


def test_a_method_that_is_not_a_string_is_an_invalid_request(spin):
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": 5}).encode()
    status, raw = spin["app"].handle_rpc(body)
    assert status == 200
    assert json.loads(raw)["error"]["code"] == -32600


@pytest.mark.parametrize("k", ["Infinity", "1e400", "-Infinity"])
def test_a_k_json_accepts_but_int_cannot_is_clamped(spin, index_payload, k):
    publish(spin, index_payload)
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_docs",'
        '"arguments":{"query":"widget","k":' + k + "}}}"
    ).encode()
    status, raw = spin["app"].handle_rpc(body)
    message = json.loads(raw)
    assert status == 200
    assert message["result"]["isError"] is False
    assert len(json.loads(result_text(message))) <= DEFAULT_K


def test_unknown_method_is_reported_as_such(spin):
    _, message = rpc(spin, "resources/list")
    assert message["error"]["code"] == -32601


def test_server_discover_is_unknown_so_a_client_falls_back_to_initialize(spin):
    _, message = rpc(spin, "server/discover")
    assert message["error"]["code"] == -32601


# --- handshake ---------------------------------------------------------------


def test_initialize_answers_the_full_handshake_shape(spin):
    status, message = rpc(spin, "initialize", {"protocolVersion": "2025-11-25"})
    assert status == 200
    result = message["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "akamai-cloud-docs"
    assert result["serverInfo"]["version"]
    assert result["instructions"] == core_tools.INSTRUCTIONS


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_initialize_echoes_a_known_version(spin, version):
    _, message = rpc(spin, "initialize", {"protocolVersion": version})
    assert message["result"]["protocolVersion"] == version


@pytest.mark.parametrize("version", ["2026-07-28", "1999-01-01", 42, None, ["2025-11-25"]])
def test_initialize_answers_the_newest_it_knows_to_anything_else(spin, version):
    _, message = rpc(spin, "initialize", {"protocolVersion": version})
    assert message["result"]["protocolVersion"] == "2025-11-25"
    assert spin["app"].PROTOCOL_VERSION == "2025-11-25"


def test_initialize_without_params_still_answers(spin):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
    status, raw = spin["app"].handle_rpc(body)
    assert status == 200
    assert json.loads(raw)["result"]["protocolVersion"] == "2025-11-25"


def test_ping_answers_an_empty_result(spin):
    status, message = rpc(spin, "ping")
    assert status == 200
    assert message["result"] == {}


def _asgi(spin):
    """Wrap the handler as an ASGI app so the SDK client can drive it in-process."""
    handler = spin["app"].IncomingHandler()

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        uri = scope["path"]
        if scope.get("query_string"):
            uri += "?" + scope["query_string"].decode()
        headers = {name.decode(): value.decode() for name, value in scope["headers"]}
        response = handler.handle_request(FakeRequest(scope["method"], uri, headers, body))
        out = response.body or b""
        raw_headers = [(k.encode(), v.encode()) for k, v in response.headers.items()]
        raw_headers.append((b"content-length", str(len(out)).encode()))
        await send({"type": "http.response.start", "status": response.status, "headers": raw_headers})
        await send({"type": "http.response.body", "body": out})

    return app


def _through_sdk(spin, mode: str, operation):
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    async def main():
        transport = httpx2.ASGITransport(app=_asgi(spin))
        async with httpx2.AsyncClient(transport=transport) as http_client:
            url = "http://functions.invalid/mcp"
            async with Client(streamable_http_client(url, http_client=http_client), mode=mode) as client:
                return await operation(client)

    return anyio.run(main)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_the_real_sdk_client_connects_and_calls_both_tools(spin, index_payload, small_guide, mode):
    """`auto` probes server/discover first, gets -32601, and falls back."""
    publish(spin, index_payload)
    spin["responder"] = lambda request: FakeResponse(200, {}, small_guide.encode())

    async def operation(client):
        tools = await client.list_tools()
        search = await client.call_tool("search_docs", {"query": "widget"})
        card = await client.call_tool("fetch_doc", {"id": "POST /widgets"})
        guide = await client.call_tool("fetch_doc", {"id": "resize-a-widget"})
        return tools, search, card, guide

    tools, search, card, guide = _through_sdk(spin, mode, operation)
    assert [tool.name for tool in tools.tools] == ["search_docs", "fetch_doc"]
    assert search.is_error is False
    assert json.loads(search.content[0].text)
    assert card.content[0].text.startswith("## POST /widgets")
    assert guide.is_error is False
    assert guide.content[0].text.startswith("# ")


def test_the_real_sdk_client_gets_the_instructions(spin):
    async def operation(client):
        return client.instructions

    instructions = _through_sdk(spin, "legacy", operation)
    assert instructions == core_tools.INSTRUCTIONS


# --- admin -------------------------------------------------------------------


def test_admin_sync_requires_the_token(spin):
    spin["variables"]["sync_token"] = TOKEN
    app = spin["app"]
    for header in ({}, {"authorization": "Bearer wrong"}, {"authorization": TOKEN}):
        response = app.handle_admin_sync(FakeRequest("POST", "/admin/sync", header, b"{}"))
        assert response.status == 401


def test_admin_sync_refuses_a_non_ascii_token(spin):
    """hmac.compare_digest raises on such a string; that must be a 401, not a 500."""
    response = admin(spin, {}, headers={"authorization": "Bearer café"})
    assert response.status == 401


def test_admin_sync_refuses_when_no_token_is_configured(spin):
    """An unset variable must not become an open write path."""
    response = spin["app"].handle_admin_sync(
        FakeRequest("POST", "/admin/sync", {"authorization": "Bearer "}, b"{}")
    )
    assert response.status == 401


def test_admin_sync_uploads_chunks_then_publishes(spin, index_payload):
    meta = push_via_admin(spin, index_payload, GENERATION_A)
    published = json.loads(spin["store"].get("index:meta"))
    assert published["generation"] == GENERATION_A
    assert published["sha256"] == meta["sha256"]
    assert spin["app"].load_service().search_docs("widget", 3)


def test_admin_sync_finish_echoes_the_meta_it_published(spin, index_payload):
    raw = json.dumps(index_payload).encode()
    body = {"generation": GENERATION_A, "chunk": 0, "data": base64.b64encode(raw).decode()}
    assert admin(spin, body).status == 200
    meta = meta_for(raw, 1, GENERATION_A, built_at="2026-01-01T00:00:00Z", documents=4)
    response = admin(spin, {"finish": meta})
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True, "published": meta}


def test_a_partial_push_leaves_the_old_index_serving(spin, index_payload):
    publish(spin, index_payload, generation=GENERATION_A)
    push_via_admin(spin, {**index_payload, "docs": []}, GENERATION_B, finish=False)

    status, message = call(spin, "search_docs", {"query": "widget"})
    assert status == 200
    assert json.loads(result_text(message))
    assert json.loads(handle(spin, "GET", "/").body)["generation"] == GENERATION_A


def test_a_finish_whose_bytes_do_not_add_up_is_a_400_and_meta_is_unchanged(spin, index_payload):
    publish(spin, index_payload, generation=GENERATION_A)
    meta = push_via_admin(spin, index_payload, GENERATION_B, finish=False)
    response = admin(spin, {"finish": {**meta, "bytes": meta["bytes"] + 1}})
    assert response.status == 400
    assert "finish" in json.loads(response.body)["error"]
    assert json.loads(spin["store"].get("index:meta"))["generation"] == GENERATION_A


def test_a_finish_for_chunks_never_sent_is_a_400(spin):
    response = admin(spin, {"finish": meta_for(b"never sent", 1, GENERATION_A)})
    assert response.status == 400
    assert spin["store"].get("index:meta") is None


def test_a_sweep_failure_after_publish_is_a_200_with_a_warning(spin, index_payload, capsys):
    """Meta is written before the sweep. A store error there must not turn a
    finished publish into the 500 the client retries three times and then
    reports as rejected."""
    app, store = spin["app"], spin["store"]
    publish(spin, index_payload, generation=GENERATION_A)
    meta = push_via_admin(spin, index_payload, GENERATION_B, finish=False)

    def boom():
        raise RuntimeError("a key name or an address could be in here")

    store.get_keys = boom
    capsys.readouterr()
    response = admin(spin, {"finish": meta})

    assert response.status == 200
    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["published"] == meta
    assert "RuntimeError" in body["warning"]
    assert "key name" not in body["warning"]
    assert json.loads(store.get("index:meta"))["generation"] == GENERATION_B
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [json.loads(line) for line in lines] == [
        {"event": "sweep", "ok": False, "error": "RuntimeError"}
    ]
    del store.get_keys
    assert app.load_service().search_docs("widget", 3)


@pytest.mark.parametrize(
    "body",
    [
        b"null",
        b"[]",
        b'"chunk"',
        b"{not json",
        {"hello": "world"},
        {"chunk": 0, "data": "AAAA"},
        {"generation": "xyz", "chunk": 0, "data": "AAAA"},
        {"generation": 12, "chunk": 0, "data": "AAAA"},
        {"generation": GENERATION_A, "chunk": "abc", "data": "AAAA"},
        {"generation": GENERATION_A, "chunk": True, "data": "AAAA"},
        {"generation": GENERATION_A, "chunk": -1, "data": "AAAA"},
        {"generation": GENERATION_A, "chunk": 1.5, "data": "AAAA"},
        {"generation": GENERATION_A, "chunk": 64, "data": "AAAA"},
        {"generation": GENERATION_A, "chunk": 10**12, "data": "AAAA"},
        {"generation": GENERATION_A, "chunk": 0},
        {"generation": GENERATION_A, "chunk": 0, "data": 123},
        {"generation": GENERATION_A, "chunk": 0, "data": None},
        {"generation": GENERATION_A, "chunk": 0, "data": "abc"},
        {"generation": GENERATION_A, "chunk": 0, "data": "!!!!"},
        {"generation": GENERATION_A, "chunk": 0, "data": ""},
        {"finish": "x"},
        {"finish": None},
        {"finish": {"bytes": 5}},
        {"finish": {"generation": GENERATION_A, "chunks": "9", "bytes": 1, "sha256": "0" * 64}},
        {"finish": {"generation": GENERATION_A, "chunks": True, "bytes": 1, "sha256": "0" * 64}},
        {"finish": {"generation": GENERATION_A, "chunks": 10**9, "bytes": 3, "sha256": "0" * 64}},
        {"finish": {"generation": GENERATION_A, "chunks": 1, "bytes": 0, "sha256": "0" * 64}},
        {"finish": {"generation": GENERATION_A, "chunks": 1, "bytes": 3, "sha256": "nope"}},
        {"finish": {"generation": "short", "chunks": 1, "bytes": 3, "sha256": "0" * 64}},
    ],
)
def test_every_malformed_admin_input_is_a_400_and_the_app_keeps_serving(spin, index_payload, body):
    publish(spin, index_payload, generation=GENERATION_B)
    response = admin(spin, body)
    assert response.status == 400, response.body
    assert json.loads(response.body)["error"]
    assert json.loads(spin["store"].get("index:meta"))["generation"] == GENERATION_B
    assert call(spin, "search_docs", {"query": "widget"})[0] == 200


def test_an_oversized_chunk_is_a_413(spin):
    app = spin["app"]
    data = base64.b64encode(b"x" * (app.MAX_CHUNK_BYTES + 1)).decode()
    response = admin(spin, {"generation": GENERATION_A, "chunk": 0, "data": data})
    assert response.status == 413
    assert spin["store"].data == {}


def test_a_chunk_at_the_cap_is_stored(spin):
    app = spin["app"]
    data = base64.b64encode(b"x" * app.MAX_CHUNK_BYTES).decode()
    response = admin(spin, {"generation": GENERATION_A, "chunk": 0, "data": data})
    assert response.status == 200
    assert json.loads(response.body)["bytes"] == app.MAX_CHUNK_BYTES


def test_a_bad_chunk_number_is_never_stored(spin):
    for number in (-1, 64, True, "0"):
        admin(spin, {"generation": GENERATION_A, "chunk": number, "data": "AAAA"})
    assert spin["store"].data == {}


# --- routing -----------------------------------------------------------------


def test_status_page_reports_the_index_and_hides_configuration(spin, index_payload):
    publish(spin, index_payload)
    response = handle(spin, "GET", "/")
    assert response.status == 200
    body = json.loads(response.body)
    assert body["documents"] == len(index_payload["docs"])
    assert body["generation"] == GENERATION_A
    assert body["protocol"] == "2025-11-25"
    assert body["stale"] is False
    assert 0 <= body["index_age_days"] < 1
    assert "staleness" not in body
    assert "sync_token" not in response.body.decode()


def test_status_page_flags_a_stale_index(spin, index_payload):
    publish(spin, {**index_payload, "built_at": "2020-01-01T00:00:00Z"})
    body = json.loads(handle(spin, "GET", "/").body)
    assert body["stale"] is True
    assert body["index_age_days"] > 7
    assert body["staleness"].startswith("Index built ")
    assert "akamai-cloud-docs-mcp sync" in body["staleness"]


def test_status_page_says_what_to_do_when_empty(spin):
    response = handle(spin, "GET", "/")
    assert response.status == 503
    assert "sync --push" in json.loads(response.body)["error"]


def test_status_page_is_503_not_500_on_a_bad_meta(spin, index_payload):
    meta = publish(spin, index_payload)
    spin["store"].data["index:meta"] = json.dumps({**meta, "chunks": 10**9}).encode()
    spin["app"]._service = None
    response = handle(spin, "GET", "/")
    assert response.status == 503


def test_mcp_post_carries_the_protocol_header(spin, index_payload):
    publish(spin, index_payload)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    response = handle(spin, "POST", "/mcp", body=body)
    assert response.headers["mcp-protocol-version"] == "2025-11-25"
    assert response.headers["mcp-protocol-version"] == spin["app"].PROTOCOL_VERSION


def test_query_string_and_trailing_slash_still_route(spin):
    assert handle(spin, "GET", "/?debug=1").status == 503  # reached the handler, no index yet
    assert handle(spin, "GET", "/mcp/?debug=1").status == 405
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    assert handle(spin, "POST", "/mcp/", body=body).status == 200


def test_get_and_delete_on_mcp_are_405_with_allow_post(spin):
    for method in ("GET", "DELETE"):
        response = handle(spin, method, "/mcp")
        assert response.status == 405
        assert response.headers["allow"] == "POST"
        assert response.headers["content-type"].startswith("text/plain")
        assert response.body.count(b"\n") == 1
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["mcp-protocol-version"] == "2025-11-25"


def test_options_preflight_on_mcp(spin):
    response = handle(spin, "OPTIONS", "/mcp")
    assert response.status == 204
    assert not response.body
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    assert response.headers["access-control-allow-headers"] == (
        "content-type, accept, mcp-protocol-version, mcp-session-id, mcp-method, mcp-name"
    )
    assert response.headers["access-control-expose-headers"] == "mcp-protocol-version"


def test_root_answers_head_like_get_without_a_body(spin, index_payload):
    """Uptime probes send HEAD. It used to be a 405."""
    publish(spin, index_payload)
    get = handle(spin, "GET", "/")
    head = handle(spin, "HEAD", "/")
    assert get.status == 200
    assert (head.status, head.headers) == (get.status, get.headers)
    assert not head.body
    assert handle(spin, "HEAD", "/?probe=1").status == 200


def test_root_answers_head_with_the_status_get_would_give(spin):
    assert handle(spin, "HEAD", "/").status == 503


def test_root_sends_no_cors_so_it_answers_no_preflight(spin, index_payload):
    """OPTIONS / used to promise CORS headers that GET / never sent, so a
    browser passed the preflight and was then blocked on the answer."""
    publish(spin, index_payload)
    options = handle(spin, "OPTIONS", "/")
    assert options.status == 405
    assert options.headers["allow"] == "GET, HEAD"
    for response in (options, handle(spin, "GET", "/"), handle(spin, "HEAD", "/")):
        assert not any(name.startswith("access-control-") for name in response.headers)


def test_every_mcp_response_carries_cors_headers(spin, index_payload):
    publish(spin, index_payload)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    for response in (handle(spin, "POST", "/mcp", body=body), handle(spin, "POST", "/mcp", body=b"[]")):
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["access-control-expose-headers"] == "mcp-protocol-version"
        assert "authorization" not in response.headers["access-control-allow-headers"]


def test_admin_sync_carries_no_cors_headers(spin):
    response = handle(spin, "POST", "/admin/sync", body=b"{}")
    assert response.status == 401
    assert not any(name.startswith("access-control-") for name in response.headers)


def test_wrong_methods_and_paths(spin):
    assert handle(spin, "GET", "/admin/sync").status == 405
    assert handle(spin, "PUT", "/mcp", body=b"{}").status == 405
    assert handle(spin, "POST", "/", body=b"{}").status == 405
    assert handle(spin, "GET", "/nope").status == 404


def test_an_unexpected_failure_never_leaks_a_traceback(spin, monkeypatch):
    app = spin["app"]

    def boom(body):
        raise RuntimeError("secret detail from inside")

    monkeypatch.setattr(app, "handle_rpc", boom)
    response = handle(spin, "POST", "/mcp", body=b"{}")
    assert response.status == 500
    text = response.body.decode()
    assert "secret detail" not in text
    assert "RuntimeError" in text
    assert response.headers["access-control-allow-origin"] == "*"


def test_admin_and_status_are_guarded_the_same_way(spin, monkeypatch):
    app = spin["app"]

    def boom(*args):
        raise KeyError("secret detail from inside")

    monkeypatch.setattr(app, "handle_admin_sync", boom)
    monkeypatch.setattr(app, "handle_status", boom)
    for method, path in (("POST", "/admin/sync"), ("GET", "/")):
        response = handle(spin, method, path, body=b"{}")
        assert response.status == 500
        text = response.body.decode()
        assert "secret detail" not in text
        assert "KeyError" in text


# --- logging -----------------------------------------------------------------


def test_each_call_prints_one_json_line_without_the_query(spin, index_payload, capsys):
    publish(spin, index_payload)
    call(spin, "search_docs", {"query": "sprocket-secret-query"})
    call(spin, "fetch_doc", {"id": "no-such-page"})
    call(spin, "drop_tables", {})
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(lines) == 3
    entries = [json.loads(line) for line in lines]
    assert all(set(entry) == {"tool", "ms", "ok"} for entry in entries)
    assert [entry["tool"] for entry in entries] == ["search_docs", "fetch_doc", "unknown"]
    assert [entry["ok"] for entry in entries] == [True, False, False]
    assert all(isinstance(entry["ms"], int) and entry["ms"] >= 0 for entry in entries)
    assert "sprocket-secret-query" not in "".join(lines)
    assert "no-such-page" not in "".join(lines)


def test_a_call_that_finds_no_index_still_prints_one_line(spin, capsys):
    """The 503 is the outage the log exists to show. It used to print nothing."""
    status, _ = call(spin, "search_docs", {"query": "sprocket-secret-query"})
    assert status == 503
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry) == {"tool", "ms", "ok", "error"}
    assert (entry["tool"], entry["ok"], entry["error"]) == ("search_docs", False, "index")
    assert isinstance(entry["ms"], int) and entry["ms"] >= 0
    assert "sprocket-secret-query" not in lines[0]
    assert "sync --push" not in lines[0]  # the operator text stays out of the line


def test_a_call_that_crashes_prints_one_line_naming_the_class(
    spin, index_payload, capsys, monkeypatch
):
    publish(spin, index_payload)

    def boom(self, query, k):
        raise RuntimeError("secret detail from inside")

    monkeypatch.setattr(core_tools.DocsService, "search_docs", boom)
    capsys.readouterr()
    with pytest.raises(RuntimeError):
        call(spin, "search_docs", {"query": "widget"})
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    entry = json.loads(lines[0])
    assert len(lines) == 1
    assert (entry["tool"], entry["ok"], entry["error"]) == ("search_docs", False, "RuntimeError")
    assert "secret detail" not in lines[0]


# --- outbound ----------------------------------------------------------------


def test_guide_fetch_is_pinned_to_the_documentation_host(spin):
    """The model supplies a slug. The URL is built and validated here."""
    app = spin["app"]
    spin["responder"] = lambda request: FakeResponse(200, {}, b"# Invented page\n\nBody.\n")

    body, _updated, url = app.spin_fetch_guide("create-a-widget")

    assert url == "https://techdocs.akamai.com/cloud-computing/docs/create-a-widget.md"
    assert spin["sent"][0].uri == url
    assert "Invented page" in body


def test_guide_fetch_refuses_a_foreign_url(spin):
    from akamai_cloud_docs_mcp.core.catalog import InvalidDocId

    app = spin["app"]
    for hostile in (
        "https://evil.example.com/cloud-computing/docs/page.md",
        "http://techdocs.akamai.com/cloud-computing/docs/page.md",
        "https://techdocs.akamai.com/other-product/docs/page.md",
        "https://techdocs.akamai.com/cloud-computing/docs/../../secrets.md",
    ):
        with pytest.raises(InvalidDocId):
            app.spin_fetch_guide(hostile)
    assert spin["sent"] == []


def test_a_send_failure_is_a_fetch_error_naming_only_the_type(spin):
    from akamai_cloud_docs_mcp.core.catalog import FetchError

    def explode(request):
        raise ConnectionResetError("peer 203.0.113.9 reset the connection")

    spin["responder"] = explode
    with pytest.raises(FetchError) as caught:
        spin["app"].spin_fetch_guide("create-a-widget")
    assert "ConnectionResetError" in str(caught.value)
    assert "203.0.113.9" not in str(caught.value)


def test_a_send_failure_from_the_sdk_names_the_wasi_error_code(spin):
    """The SDK raises `Err(ErrorCode_X())`. The code is named, not the wrapper.

    A timeout, a refused connection and a denied host all read as `Err`
    before, to the model in the fallback note and to `spin aka logs`.
    """
    from akamai_cloud_docs_mcp.core.catalog import FetchError

    class ErrorCode_DnsError:
        """Shaped like the variants in spin_sdk.wit.imports.types; some carry a payload."""

        def __init__(self, value) -> None:
            self.value = value

    def explode(request):
        raise FakeErr(ErrorCode_DnsError("NXDOMAIN for techdocs.akamai.com from 203.0.113.9"))

    spin["responder"] = explode
    with pytest.raises(FetchError) as caught:
        spin["app"].spin_fetch_guide("create-a-widget")
    message = str(caught.value)
    assert message.endswith("read failed: ErrorCode_DnsError")
    assert "203.0.113.9" not in message


def test_the_fallback_note_names_the_wasi_error_code(spin, index_payload):
    publish(spin, index_payload)

    class ErrorCode_ConnectionReadTimeout:
        pass

    def explode(request):
        raise FakeErr(ErrorCode_ConnectionReadTimeout())

    spin["responder"] = explode
    _, message = call(spin, "fetch_doc", {"id": "resize-a-widget"})
    text = result_text(message)
    assert "read failed: ErrorCode_ConnectionReadTimeout" in text
    assert "read failed: Err)" not in text


def test_a_send_failure_falls_back_to_the_indexed_copy(spin, index_payload):
    """An upstream outage is a note on the indexed text, not a 500."""
    publish(spin, index_payload)

    def explode(request):
        raise RuntimeError("secret runtime detail")

    spin["responder"] = explode
    status, message = call(spin, "fetch_doc", {"id": "resize-a-widget"})
    assert status == 200
    assert message["result"]["isError"] is False
    text = result_text(message)
    assert "indexed copy" in text
    assert "RuntimeError" in text
    assert "secret runtime detail" not in text


def test_outbound_requests_get_a_ten_second_bound_on_every_phase(spin):
    """The SDK passes no options. The app's replacement bounds the connect,
    the first byte, and every body chunk after it: the SDK reads the body
    chunk by chunk, so a stall after the first byte had no bound before."""
    app = spin["app"]
    request = object()
    app._handle_with_timeout(request, None)
    sent, options = spin["handled"][-1]
    assert sent is request
    assert options.connect == 10 * 10**9
    assert options.first_byte == 10 * 10**9
    assert options.between_bytes == 10 * 10**9


def test_a_runtime_that_refuses_one_bound_keeps_the_others(spin):
    """A refused setter used to throw away the bounds already accepted."""
    app = spin["app"]
    FakeRequestOptions.refuse = ("first_byte",)
    app._handle_with_timeout(object(), None)
    options = spin["handled"][-1][1]
    assert options.connect == 10 * 10**9
    assert options.first_byte is None
    assert options.between_bytes == 10 * 10**9


def test_an_sdk_without_the_between_bytes_setter_keeps_the_other_two(spin, monkeypatch):
    app = spin["app"]
    monkeypatch.delattr(FakeRequestOptions, "set_between_bytes_timeout")
    app._handle_with_timeout(object(), None)
    options = spin["handled"][-1][1]
    assert options.connect == 10 * 10**9
    assert options.first_byte == 10 * 10**9
    assert options.between_bytes is None


def test_explicit_outbound_options_pass_through(spin):
    app = spin["app"]
    options = FakeRequestOptions()
    app._handle_with_timeout(object(), options)
    assert spin["handled"][-1][1] is options


def test_a_runtime_that_refuses_every_bound_still_sends(spin):
    app = spin["app"]
    FakeRequestOptions.refuse = ("connect", "first_byte", "between_bytes")
    app._handle_with_timeout(object(), None)
    assert spin["handled"][-1][1] is None
