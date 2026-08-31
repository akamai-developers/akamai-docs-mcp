"""API catalog: the Linode OpenAPI spec turned into compact operation cards.

The reference pages do not serve markdown, so the API catalog is built from the
spec instead. The spec is 7.9MB and is read only during sync. What ships in the
index is one rendered card per operation, each a couple of hundred bytes.

Card rendering is deliberately lossy. Response schemas are the largest thing in
any OpenAPI document and the least useful to an agent answering "how do I call
this", so they are left out.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import (
    API_REFERENCE_BASE,
    MAX_SPEC_BYTES,
    MIN_OPERATIONS,
    OPENAPI_URL,
)
from .catalog import fetch_url
from .sections import flatten_markdown, summarize

HTTP_METHODS = ("get", "post", "put", "delete", "patch", "options", "head")

#: Stripped from every path. It is a URL artifact, always `v4`, and it would
#: otherwise appear as a required parameter on all 449 operations.
API_VERSION_PREFIX = "/{apiVersion}"
API_VERSION_PARAM = "apiVersion"

#: Hosts the spec download may be redirected to. GitHub serves raw files from
#: several hosts, so the redirect allowlist is wider than a single name.
SPEC_REDIRECT_HOSTS = {
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "github.com",
}

#: Nesting depth for request body objects. Deeper objects collapse to a count.
MAX_BODY_DEPTH = 2
#: Optional body fields are listed by name only, and only this many.
MAX_OPTIONAL_NAMES = 20
MAX_PARAM_DESC = 110


class ApiCatalogError(Exception):
    """The API catalog could not be built."""


def _first_line(text: str, limit: int = MAX_PARAM_DESC) -> str:
    """One flattened line of prose from a spec description."""
    if not text:
        return ""
    summary = summarize(text, limit)
    return summary or flatten_markdown(text)[:limit]


def _type_of(schema: dict) -> str:
    """A short type label for a schema."""
    if not isinstance(schema, dict):
        return "any"
    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((entry for entry in declared if entry != "null"), None)
    if declared == "array":
        items = schema.get("items") or {}
        inner = _type_of(items) if isinstance(items, dict) else "any"
        return f"array[{inner}]"
    if declared:
        return str(declared)
    if schema.get("enum"):
        return "string"
    if schema.get("properties"):
        return "object"
    for combinator in ("oneOf", "anyOf", "allOf"):
        options = schema.get(combinator)
        if isinstance(options, list) and options:
            return _type_of(options[0])
    return "any"


# --- parameters --------------------------------------------------------------


def collect_parameters(path_item: dict, operation: dict) -> list[dict]:
    """Merge path-level and operation-level parameters, dropping the version."""
    merged: dict[tuple[str, str], dict] = {}
    for parameter in list(path_item.get("parameters") or []) + list(
        operation.get("parameters") or []
    ):
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        if not name or name == API_VERSION_PARAM:
            continue
        merged[(name, parameter.get("in", ""))] = parameter
    return list(merged.values())


def render_parameters(parameters: list[dict]) -> list[str]:
    """`name (type, required): description` lines, path parameters first."""
    order = {"path": 0, "query": 1, "header": 2, "cookie": 3}
    ordered = sorted(parameters, key=lambda p: (order.get(p.get("in", ""), 9), p.get("name", "")))
    lines: list[str] = []
    for parameter in ordered:
        kind = _type_of(parameter.get("schema") or {})
        required = "required" if parameter.get("required") else "optional"
        description = _first_line(parameter.get("description") or "")
        suffix = f": {description}" if description else ""
        lines.append(f"- {parameter['name']} ({kind}, {required}){suffix}")
    return lines


# --- request body ------------------------------------------------------------


def _body_schema(operation: dict) -> tuple[dict, bool]:
    """The JSON request body schema and whether the body is required."""
    body = operation.get("requestBody") or {}
    content = body.get("content") or {}
    for media_type, entry in content.items():
        if "json" in media_type and isinstance(entry, dict):
            schema = entry.get("schema")
            if isinstance(schema, dict):
                return schema, bool(body.get("required"))
    return {}, False


def render_body(schema: dict, depth: int = 1, indent: str = "") -> list[str]:
    """Required fields in full, optional fields by name.

    Nesting stops at `MAX_BODY_DEPTH`. A deeper object becomes
    `field (object, required, N fields)` so the shape is still visible without
    paying for it.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return []

    required_names = [name for name in (schema.get("required") or []) if name in properties]
    lines: list[str] = []

    for name in required_names:
        field = properties.get(name) or {}
        kind = _type_of(field)
        description = _first_line(field.get("description") or "")
        nested = field.get("properties") if isinstance(field, dict) else None

        if kind == "object" and isinstance(nested, dict) and nested:
            if depth >= MAX_BODY_DEPTH:
                lines.append(f"{indent}- {name} (object, required, {len(nested)} fields)")
                continue
            suffix = f": {description}" if description else ""
            lines.append(f"{indent}- {name} (object, required){suffix}")
            lines.extend(render_body(field, depth + 1, indent + "  "))
            continue

        suffix = f": {description}" if description else ""
        lines.append(f"{indent}- {name} ({kind}, {required_label(True)}){suffix}")

    optional_names = [name for name in properties if name not in set(required_names)]
    if optional_names:
        shown = optional_names[:MAX_OPTIONAL_NAMES]
        remainder = len(optional_names) - len(shown)
        tail = f", +{remainder} more" if remainder > 0 else ""
        lines.append(f"{indent}- optional: {', '.join(shown)}{tail}")
    return lines


def required_label(required: bool) -> str:
    return "required" if required else "optional"


# --- CLI and scopes ----------------------------------------------------------


def cli_actions(operation: dict) -> list[str]:
    """`x-linode-cli-action` is a string on most operations and a list on some."""
    action = operation.get("x-linode-cli-action")
    if isinstance(action, str):
        return [action]
    if isinstance(action, list):
        return [entry for entry in action if isinstance(entry, str)]
    return []


def cli_commands(path_item: dict, operation: dict) -> list[str]:
    """Full `linode-cli ...` strings for an operation, canonical form first.

    The command lives on the path item and the action on the operation. An
    operation flagged `x-linode-cli-skip` is not exposed by the CLI at all, so it
    gets nothing rather than a command that does not exist.
    """
    if operation.get("x-linode-cli-skip"):
        return []
    command = path_item.get("x-linode-cli-command") or operation.get("x-linode-cli-command")
    if not isinstance(command, str) or not command:
        return []
    return [f"linode-cli {command} {action}" for action in cli_actions(operation)]


def oauth_scopes(operation: dict) -> list[str]:
    """OAuth scopes required by an operation."""
    scopes: list[str] = []
    for requirement in operation.get("security") or []:
        if not isinstance(requirement, dict):
            continue
        for values in requirement.values():
            if isinstance(values, list):
                scopes.extend(value for value in values if isinstance(value, str))
    return sorted(set(scopes))


# --- card --------------------------------------------------------------------


def render_card(
    *,
    method: str,
    path: str,
    summary: str,
    description: str,
    parameters: list[dict],
    body_schema: dict,
    body_required: bool,
    cli: list[str],
    scopes: list[str],
    url: str,
) -> str:
    """Render one operation as compact markdown."""
    lines = [f"## {method} {path}", ""]
    if summary:
        lines += [summary, ""]
    one_liner = _first_line(description, 220)
    if one_liner and one_liner != summary:
        lines += [one_liner, ""]
    if cli:
        lines += [f"CLI: `{cli[0]}`", ""]

    parameter_lines = render_parameters(parameters)
    if parameter_lines:
        lines += ["Parameters:", *parameter_lines, ""]

    body_lines = render_body(body_schema)
    if body_lines:
        header = "Body (required):" if body_required else "Body:"
        lines += [header, *body_lines, ""]

    if scopes:
        lines += [f"OAuth scopes: {', '.join(scopes)}", ""]
    lines.append(f"Reference: {url}")
    return "\n".join(lines).strip() + "\n"


def build_search_text(
    *,
    method: str,
    path: str,
    summary: str,
    description: str,
    parameters: list[dict],
    body_schema: dict,
    cli: list[str],
    tags: list[str],
) -> str:
    """Text used for ranking. Names and prose, no punctuation ceremony."""
    parts = [f"{method} {path}", summary, _first_line(description, 300), " ".join(tags)]
    parts.extend(parameter.get("name", "") for parameter in parameters)
    properties = body_schema.get("properties")
    if isinstance(properties, dict):
        parts.extend(properties.keys())
    parts.extend(cli)
    return " ".join(part for part in parts if part)


# --- spec walking ------------------------------------------------------------


def parse_spec(spec: dict) -> list[dict]:
    """Turn a parsed OpenAPI document into index entries."""
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise ApiCatalogError("spec has no paths object")

    docs: list[dict] = []
    seen: set[str] = set()

    for raw_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        display_path = raw_path
        if display_path.startswith(API_VERSION_PREFIX):
            display_path = display_path[len(API_VERSION_PREFIX) :] or "/"

        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            doc_id = f"{method.upper()} {display_path}"
            if doc_id in seen:
                continue
            seen.add(doc_id)

            operation_id = operation.get("operationId") or ""
            parameters = collect_parameters(path_item, operation)
            body_schema, body_required = _body_schema(operation)
            cli = cli_commands(path_item, operation)
            scopes = oauth_scopes(operation)
            tags = [tag for tag in (operation.get("tags") or []) if isinstance(tag, str)]
            summary = (operation.get("summary") or "").strip()
            description = operation.get("description") or ""
            url = f"{API_REFERENCE_BASE}{operation_id}" if operation_id else API_REFERENCE_BASE

            entry: dict[str, Any] = {
                "id": doc_id,
                "kind": "api",
                "title": summary or doc_id,
                "operation_id": operation_id,
                "tags": tags,
                "url": url,
                "card": render_card(
                    method=method.upper(),
                    path=display_path,
                    summary=summary,
                    description=description,
                    parameters=parameters,
                    body_schema=body_schema,
                    body_required=body_required,
                    cli=cli,
                    scopes=scopes,
                    url=url,
                ),
                "text": build_search_text(
                    method=method.upper(),
                    path=display_path,
                    summary=summary,
                    description=description,
                    parameters=parameters,
                    body_schema=body_schema,
                    cli=cli,
                    tags=tags,
                ),
            }
            if cli:
                entry["cli"] = cli[0]
                if len(cli) > 1:
                    entry["cli_aliases"] = cli[1:]
            docs.append(entry)

    return docs


def build_api_docs(
    *,
    url: str = OPENAPI_URL,
    fetcher=None,
    min_operations: int = MIN_OPERATIONS,
    quiet: bool = False,
) -> tuple[list[dict], str]:
    """Fetch and parse the spec. Returns `(docs, api_version)`."""
    import sys

    if not quiet:
        print(f"Fetching OpenAPI spec from {url}", file=sys.stderr, flush=True)
    raw = (fetcher or fetch_url)(
        url, max_bytes=MAX_SPEC_BYTES, allowed_redirect_hosts=SPEC_REDIRECT_HOSTS
    )
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiCatalogError(f"spec at {url} is not valid JSON: {exc}") from None

    docs = parse_spec(spec)
    if len(docs) < min_operations:
        raise ApiCatalogError(
            f"only {len(docs)} operations parsed, expected at least {min_operations}. "
            "The spec format may have changed; refusing to write a thin index."
        )
    version = str((spec.get("info") or {}).get("version") or "")
    if not quiet:
        with_cli = sum(1 for doc in docs if doc.get("cli"))
        print(
            f"  {len(docs)} operations, {with_cli} with a linode-cli command, spec {version}",
            file=sys.stderr,
            flush=True,
        )
    return docs, version
