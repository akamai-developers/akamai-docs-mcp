"""Synthetic fixtures.

Every byte of documentation text here is invented for the tests. No Akamai
documentation is copied into this repository, and no test touches the network.
"""

from __future__ import annotations

import socket

import pytest

DOCS_BASE = "https://techdocs.akamai.com/cloud-computing/docs/"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that tries to reach the network.

    The suite must run offline and must never depend on a live site, so a test
    that reaches out is a bug in the test. Only outbound connection paths are
    blocked. `socket.socket` itself stays intact because the event loop needs a
    local socketpair for its self-pipe.
    """

    def blocked(*args, **kwargs):
        raise RuntimeError("network access is not allowed in tests")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

# A stand-in llms.txt. Shape matches the real one: a heading, a blockquote, the
# index boilerplate line, category headings, and `- [Title](url): note` links.
LLMS_TXT = f"""# Widget Cloud Documentation

> Invented product used only for tests.

Fetch the complete documentation index at: https://techdocs.akamai.com/cloud-computing/llms.txt. Use this file to discover all available pages before exploring further.

## Guides: Widgets

- [Widget index]({DOCS_BASE}widgets/llms.txt): full category index
- [Create a widget]({DOCS_BASE}create-a-widget.md): start here
- [Resize a widget]({DOCS_BASE}resize-a-widget.md)
- [Delete a widget]({DOCS_BASE}delete-a-widget.md)

## Guides: Sprockets

- [Sprocket basics]({DOCS_BASE}sprocket-basics.md)
- [Create a widget]({DOCS_BASE}create-a-widget.md): duplicate, should collapse
- [Off site page](https://example.com/not-ours.md): wrong host
- [Bad scheme page](http://techdocs.akamai.com/cloud-computing/docs/insecure.md)
"""

GUIDE_WITH_FRONTMATTER = """---
updatedAt: 2026-02-01T10:00:00.000Z
---

Fetch the complete documentation index at: https://techdocs.akamai.com/cloud-computing/llms.txt. Use this file to discover all available pages before exploring further.

# Create a widget

Widgets hold sprockets. This page explains how to make one.

## Pick a size

Choose small, medium, or large. Large widgets cost more.

## Name the widget

Names must be lowercase.

# Verify the widget

Run the checker after creation.

## Read the output

The checker prints one line per sprocket.

### Failure codes

Code 7 means the sprocket is missing.

# Sibling pages

* [Resize a widget](https://techdocs.akamai.com/cloud-computing/docs/resize-a-widget.md)
* [Delete a widget](https://techdocs.akamai.com/cloud-computing/docs/delete-a-widget.md)
"""

GUIDE_WITH_FENCED_HEADINGS = """# Configure the sprocket

Set the values below.

```yaml
# this is a comment, not a heading
## neither is this
sprockets: 4
```

## Apply the change

Restart the widget.

~~~bash
# also not a heading
widgetctl apply
~~~

## Confirm the change

Check the status output.
"""

SMALL_GUIDE = """# Delete a widget

Deleting a widget removes its sprockets too. This cannot be undone.
"""


@pytest.fixture
def llms_txt() -> str:
    return LLMS_TXT


@pytest.fixture
def guide_markdown() -> str:
    return GUIDE_WITH_FRONTMATTER


@pytest.fixture
def fenced_markdown() -> str:
    return GUIDE_WITH_FENCED_HEADINGS


@pytest.fixture
def small_guide() -> str:
    return SMALL_GUIDE


@pytest.fixture
def sample_docs() -> list[dict]:
    """A tiny corpus for search tests."""
    return [
        {
            "id": "create-a-widget",
            "kind": "guide",
            "title": "Create a widget",
            "url": f"{DOCS_BASE}create-a-widget.md",
            "text": "# Create a widget\n\nWidgets hold sprockets. Pick a size and name the widget.\n",
        },
        {
            "id": "resize-a-widget",
            "kind": "guide",
            "title": "Resize a widget",
            "url": f"{DOCS_BASE}resize-a-widget.md",
            "text": (
                "# Resize a widget\n\nResizing a widget changes its plan. "
                "You can resize widgets up but never down.\n"
            ),
        },
        {
            "id": "sprocket-basics",
            "kind": "guide",
            "title": "Sprocket basics",
            "url": f"{DOCS_BASE}sprocket-basics.md",
            "text": "# Sprocket basics\n\nA sprocket turns inside a widget. Sprockets wear out.\n",
        },
        {
            "id": "POST /widgets",
            "kind": "api",
            "title": "Create a widget",
            "operation_id": "post-widgets",
            "url": "https://techdocs.akamai.com/linode-api/reference/post-widgets",
            "cli": "linode-cli widgets create",
            "tags": ["Widgets"],
            "card": (
                "## POST /widgets\n\nCreate a widget.\n\n"
                "**Body**\n- label (string, required): the widget label\n"
                "- region (string, required): where the widget runs\n\n"
                "**CLI**: `linode-cli widgets create`\n"
            ),
            "text": "POST /widgets Create a widget Creates a widget in a region. Widgets label size region linode-cli widgets create",
        },
    ]


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    """Point the cache directory at a temporary path."""
    monkeypatch.setenv("AKAMAI_DOCS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


# --- service fixtures --------------------------------------------------------
#
# `create-a-widget` is padded past the 8KB small-document threshold so it takes
# the table-of-contents path. `resize-a-widget` stays short so it takes the
# whole-content path.

_PADDING = "\nSprockets are measured in millimetres and seated by hand.\n" * 160


@pytest.fixture
def big_guide_body() -> str:
    body, _ = _clean(GUIDE_WITH_FRONTMATTER)
    return body.replace(
        "Widgets hold sprockets. This page explains how to make one.",
        "Widgets hold sprockets. This page explains how to make one." + _PADDING,
    )


@pytest.fixture
def index_payload(sample_docs) -> dict:
    """An index as `sync` writes it today: documents plus the prebuilt block."""
    from datetime import UTC, datetime

    from akamai_cloud_docs_mcp.config import INDEX_SCHEMA
    from akamai_cloud_docs_mcp.core.index import SearchIndex

    docs = [dict(doc) for doc in sample_docs]
    return {
        "schema": INDEX_SCHEMA,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {"guides_llms": "https://techdocs.akamai.com/cloud-computing/docs/llms.txt"},
        "docs": docs,
        "prebuilt": SearchIndex(docs).prebuilt(),
    }


@pytest.fixture
def service(index_payload):
    """A DocsService over invented documents, isolated per test."""
    from akamai_cloud_docs_mcp.core import tools as core_tools

    core_tools.set_service(None)
    built = core_tools.DocsService(index_payload)
    yield built
    core_tools.set_service(None)


@pytest.fixture
def big_guide_live(monkeypatch, big_guide_body):
    """Stand in for the live guide fetch with a document past the size threshold."""
    from akamai_cloud_docs_mcp.core import tools as core_tools

    def fake_fetch(slug, **kwargs):
        return (
            big_guide_body,
            "2026-02-01T10:00:00.000Z",
            f"{DOCS_BASE}{slug}.md",
        )

    monkeypatch.setattr(core_tools, "fetch_guide", fake_fetch)
    return big_guide_body


@pytest.fixture
def small_guide_live(monkeypatch):
    """Stand in for the live guide fetch with a document under the threshold."""
    from akamai_cloud_docs_mcp.core import tools as core_tools

    def fake_fetch(slug, **kwargs):
        return SMALL_GUIDE, "2026-02-01T10:00:00.000Z", f"{DOCS_BASE}{slug}.md"

    monkeypatch.setattr(core_tools, "fetch_guide", fake_fetch)
    return SMALL_GUIDE


def _clean(raw: str) -> tuple[str, str]:
    from akamai_cloud_docs_mcp.core.catalog import clean_guide_markdown

    return clean_guide_markdown(raw)


# --- OpenAPI fixture ---------------------------------------------------------
#
# An invented spec shaped like the real one. It exercises the parts that matter:
# the CLI command on the path item with the action on the operation, an action
# given as a list, x-linode-cli-skip, a missing action, a nested request body
# three levels deep, and a response schema that must never reach a card.

_VERSION_PARAM = {
    "name": "apiVersion",
    "in": "path",
    "required": True,
    "description": "__Enum__ Call either the `v4` URL, or `v4beta`.",
    "schema": {"type": "string", "enum": ["v4", "v4beta"]},
}

FAKE_SPEC = {
    "openapi": "3.0.1",
    "info": {"title": "Widget API", "version": "4.215.0"},
    "servers": [{"url": "https://api.example.invalid"}],
    "paths": {
        "/{apiVersion}/widgets": {
            "parameters": [_VERSION_PARAM],
            "x-linode-cli-command": "widgets",
            "post": {
                "operationId": "post-widgets",
                "summary": "Create a widget",
                "description": "**Make a widget**\n\nUse this operation to create a widget.",
                "tags": ["Widgets"],
                "x-linode-cli-action": "create",
                "security": [{"personalAccessToken": []}, {"oauth": ["widgets:read_write"]}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["label", "size", "placement"],
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "A name for the widget.",
                                    },
                                    "size": {
                                        "type": "string",
                                        "enum": ["small", "large"],
                                        "description": "How big the widget is.",
                                    },
                                    "placement": {
                                        "type": "object",
                                        "description": "Where the widget goes.",
                                        "required": ["region", "coordinates"],
                                        "properties": {
                                            "region": {
                                                "type": "string",
                                                "description": "Region identifier.",
                                            },
                                            "coordinates": {
                                                "type": "object",
                                                "required": ["latitude", "longitude"],
                                                "properties": {
                                                    "latitude": {"type": "number"},
                                                    "longitude": {"type": "number"},
                                                },
                                            },
                                        },
                                    },
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                    "notes": {
                                        "type": "string",
                                        "description": "Free-form notes about the widget.",
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "The created widget.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"widget_id_returned": {"type": "integer"}},
                                }
                            }
                        },
                    }
                },
            },
            "get": {
                "operationId": "get-widgets",
                "summary": "List widgets",
                "description": "Returns a paginated list of widgets.",
                "tags": ["Widgets"],
                "x-linode-cli-action": ["list", "ls"],
                "security": [{"oauth": ["widgets:read_only"]}],
                "parameters": [
                    {
                        "name": "page",
                        "in": "query",
                        "description": "The page of a collection to return.",
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "page_size",
                        "in": "query",
                        "description": "How many items per page.",
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {"200": {"description": "A list."}},
            },
        },
        "/{apiVersion}/widgets/{widgetId}": {
            "parameters": [
                _VERSION_PARAM,
                {
                    "name": "widgetId",
                    "in": "path",
                    "required": True,
                    "description": "The widget to act on.",
                    "schema": {"type": "integer"},
                },
            ],
            "x-linode-cli-command": "widgets",
            "get": {
                "operationId": "get-widget",
                "summary": "Get a widget",
                "description": "Returns one widget.",
                "tags": ["Widgets"],
                "x-linode-cli-action": "view",
                "responses": {"200": {"description": "One widget."}},
            },
            "delete": {
                "operationId": "delete-widget",
                "summary": "Delete a widget",
                "description": "Removes a widget.",
                "tags": ["Widgets"],
                "responses": {"204": {"description": "Gone."}},
            },
        },
        "/{apiVersion}/internal/rebuild": {
            "parameters": [_VERSION_PARAM],
            "x-linode-cli-command": "internal",
            "post": {
                "operationId": "post-internal-rebuild",
                "summary": "Rebuild internals",
                "description": "Not exposed by the CLI.",
                "tags": ["Internal"],
                "x-linode-cli-action": "rebuild",
                "x-linode-cli-skip": True,
                "responses": {"202": {"description": "Accepted."}},
            },
        },
    },
}


@pytest.fixture
def fake_spec() -> dict:
    import copy

    return copy.deepcopy(FAKE_SPEC)
