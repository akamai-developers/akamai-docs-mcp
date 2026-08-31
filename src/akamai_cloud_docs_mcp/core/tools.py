"""The two tools, as plain functions over plain dicts.

Nothing here imports `mcp`. A transport hands every result to `render_result`
for the text a model reads, and a dict carrying an `error` key becomes an
`isError` tool result.

Errors teach. An unknown id comes back with the closest real ids, and a bad
section comes back with the top-level section list, so a model's next call can
succeed.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime

from ..config import (
    API_REFERENCE_BASE,
    DEFAULT_K,
    INDEX_SCHEMA,
    MAX_ID_LEN,
    MAX_K,
    MAX_QUERY_LEN,
    MAX_SECTION_LEN,
    MIN_K,
    SMALL_DOC_BYTES,
    STALE_AFTER_DAYS,
    index_path,
)
from .catalog import InvalidDocId, fetch_guide, guide_url, normalize_guide_id
from .index import SearchIndex
from .sections import build_toc, parse_document, section_text

#: Hard ceiling on returned section text. Guides have a few very long sections.
MAX_SECTION_CHARS = 40_000
#: Cap on the section list returned with a bad-section error.
MAX_VALID_SECTIONS = 60
#: Longest failure reason quoted in a note. The rest of a long message is noise.
MAX_REASON_CHARS = 120


# --- tool descriptions -------------------------------------------------------
#
# These live here rather than next to the MCP server because every transport
# needs them and this module has no third-party imports. The Akamai Functions
# handler runs in a WebAssembly interpreter with no `anyio`, so it cannot import
# the SDK server module at all.
#
# Combined budget for the two descriptions is about 300 tokens. A test asserts it.

INSTRUCTIONS = (
    "Akamai Cloud (Linode) documentation: product guides and the Linode API reference. "
    "Call search_docs first to get a document id. Call fetch_doc(id) for a table of "
    "contents, then fetch_doc(id, section) to read one section at a time. "
    "Pass ids exactly as search_docs returns them."
)

SEARCH_TITLE = "Search Akamai Cloud docs"
FETCH_TITLE = "Read an Akamai Cloud doc"

SEARCH_DESCRIPTION = """Search Akamai Cloud documentation: product guides and Linode API operations.

Workflow:
1. search_docs("resize a database cluster") to find an id.
2. fetch_doc(id) for that document's table of contents.
3. fetch_doc(id, section="9") to read just that section.

Example: search_docs(query="create postgres cluster", k=5)

Returns a list of {id, title, snippet}. Ids that start with an HTTP verb and a
path are API operations; everything else is a guide. Pass an id to fetch_doc
verbatim. No match returns an empty list."""

FETCH_DESCRIPTION = """Read one Akamai Cloud document. Get ids from search_docs.

Omit section to get a table of contents, then call again with a section id from
it. Guides under 8KB return their whole content instead. An API id returns an
operation card.

Example: fetch_doc(id="aiven-manage-database", section="9")

Returns a table of contents as JSON {id, title, preamble, sections}. An entry
carries a summary, or the whole text when the section is short; do not fetch a
section whose entry has content. A section, a small guide, or an operation
card comes back as markdown under one header line. Guide text is fetched live,
so it is never stale."""

# Both tools read a local index. Neither writes, and a repeat call with the same
# arguments returns the same answer, so a client may run them without asking.
SEARCH_ANNOTATIONS = FETCH_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

# The one source for the advertised input schemas. The SDK server generates its
# own from the tool signatures and a test asserts the two are equal; the
# Functions handler and the eval harness use these literals directly. No "title"
# keys anywhere: they cost tokens on every turn and say nothing.
SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to find, in plain words. No search syntax.",
            "maxLength": MAX_QUERY_LEN,
        },
        "k": {
            "type": "integer",
            "description": f"Results to return, {MIN_K} to {MAX_K}. Out of range is clamped.",
            "default": DEFAULT_K,
        },
    },
    "required": ["query"],
}

FETCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "An id from search_docs: a guide slug, a techdocs.akamai.com URL, "
                '"POST /path", an operationId, or a linode-cli command.'
            ),
            "maxLength": MAX_ID_LEN,
        },
        "section": {
            "type": "string",
            "description": 'A section id from the table of contents, such as "2" or "2.1".',
            "default": "",
            "maxLength": MAX_SECTION_LEN,
        },
    },
    "required": ["id"],
}


def _error(message: str, **extra) -> dict:
    """Build a structured, teaching error payload."""
    return {"error": message, **extra}


def short_reason(exc: BaseException) -> str:
    """The first line of an exception's message, capped, for a note a model reads."""
    text = str(exc).strip().splitlines()
    reason = text[0].strip() if text else type(exc).__name__
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[: MAX_REASON_CHARS - 3].rstrip() + "..."
    return reason


def _reference_operation(raw_id: str) -> str | None:
    """The operationId at the end of an API reference URL, or None.

    Every API card carries `API_REFERENCE_BASE + operationId` as its url, and
    models paste that url back as an id.
    """
    if not raw_id.lower().startswith(API_REFERENCE_BASE):
        return None
    tail = raw_id[len(API_REFERENCE_BASE) :]
    return tail.split("#", 1)[0].split("?", 1)[0].strip("/")


def prebuilt_problem(index: dict) -> str:
    """Why `index` cannot be served from its `prebuilt` block, or "" when it can.

    One rule for two callers. `DocsService` falls back to building the search
    index from the documents when this is non-empty, so an index written by an
    older `sync` keeps working on a laptop, a quarter of a second slower. The
    Functions handler refuses instead, because there the build is paid on every
    request, and it puts this sentence in its 503.
    """
    schema = index.get("schema")
    if schema != INDEX_SCHEMA:
        return f"it was written with index schema {schema!r}; this version reads {INDEX_SCHEMA}"
    if not isinstance(index.get("prebuilt"), dict):
        return "it carries no prebuilt search index"
    return ""


class DocsService:
    """Read-only query surface over one loaded index."""

    def __init__(self, index: dict):
        self.built_at: str = index.get("built_at", "")
        self.sources: dict = index.get("sources", {})
        docs = index.get("docs", [])
        if prebuilt_problem(index):
            self.search_index = SearchIndex(docs)
        else:
            self.search_index = SearchIndex.from_prebuilt(docs, index["prebuilt"])
        # Not stored in the index: building this table takes 0.4 ms for 449
        # operations, and parsing it from JSON would take longer than that.
        self._api_aliases = self._build_api_aliases()

    # -- id resolution --------------------------------------------------------

    def _build_api_aliases(self) -> dict[str, str]:
        """Map operationId and `linode-cli ...` strings onto API document ids."""
        aliases: dict[str, str] = {}
        for doc in self.search_index.docs:
            if doc.get("kind") != "api":
                continue
            doc_id = doc["id"]
            operation_id = doc.get("operation_id")
            if operation_id:
                aliases.setdefault(operation_id.lower(), doc_id)
            commands = [doc.get("cli"), *(doc.get("cli_aliases") or [])]
            for command in commands:
                if not command:
                    continue
                aliases.setdefault(command.lower(), doc_id)
                aliases.setdefault(command.lower().removeprefix("linode-cli ").strip(), doc_id)
            aliases.setdefault(doc_id.lower(), doc_id)
        return aliases

    def resolve(self, raw_id: str) -> dict | None:
        """Find a document from any accepted id form. None when unknown."""
        candidate = (raw_id or "").strip()
        if not candidate:
            return None

        direct = self.search_index.get(candidate)
        if direct is not None:
            return direct

        alias = self._api_aliases.get(candidate.lower())
        if alias:
            return self.search_index.get(alias)

        operation = _reference_operation(candidate)
        if operation:
            alias = self._api_aliases.get(operation.lower())
            if alias:
                return self.search_index.get(alias)

        # `post /linode/instances` and `POST  /linode/instances` both resolve.
        parts = candidate.split(None, 1)
        if len(parts) == 2 and parts[1].startswith("/"):
            normalized = f"{parts[0].upper()} {parts[1]}"
            found = self.search_index.get(normalized)
            if found is not None:
                return found

        try:
            slug = normalize_guide_id(candidate)
        except InvalidDocId:
            return None
        return self.search_index.get(slug) or self.search_index.get(slug.lower())

    # -- freshness ------------------------------------------------------------

    def index_age_days(self) -> float | None:
        """Age of the loaded index in days, or None when it cannot be read.

        `fromisoformat` rather than `strptime`: the latter lazily imports
        `_strptime`, which is not present in the WebAssembly build used on
        Akamai Functions, so it fails there and nowhere else.
        """
        if not self.built_at:
            return None
        try:
            built = datetime.fromisoformat(self.built_at)
        except ValueError:
            return None
        if built.tzinfo is None:
            built = built.replace(tzinfo=UTC)
        return (datetime.now(UTC) - built).total_seconds() / 86400

    def staleness_note(self) -> str:
        """A short fact for an API card when the index is past its freshness window.

        Only API cards carry it. Guide text is fetched live, so a note there
        would tell the model nothing. What to do about an old index is operator
        advice and goes to stderr through `staleness_line`.
        """
        age = self.index_age_days()
        if age is None or age <= STALE_AFTER_DAYS:
            return ""
        return f"From an index built {age:.0f} days ago."

    # -- search_docs ----------------------------------------------------------

    def search_docs(self, query: str, k: int = DEFAULT_K) -> list[dict]:
        """Rank documents across both catalogs. Never raises, never errors.

        `kind` and `score` are dropped here, not in `SearchIndex`, so the rank
        checks keep seeing them. The list is already in score order, and an
        API id always starts with an HTTP verb, so both fields only restated
        what the model can see.
        """
        if not isinstance(query, str):
            return []
        trimmed = query.strip()[:MAX_QUERY_LEN]
        if not trimmed:
            return []
        try:
            count = int(k)
        except (TypeError, ValueError, OverflowError):
            count = DEFAULT_K
        count = max(MIN_K, min(MAX_K, count))
        return [
            {key: value for key, value in hit.items() if key not in ("kind", "score")}
            for hit in self.search_index.search(trimmed, count)
        ]

    # -- fetch_doc ------------------------------------------------------------

    def fetch_doc(self, id: str, section: str = "") -> dict:  # noqa: A002
        """Read a document, its table of contents, or one of its sections."""
        raw_id = (id or "").strip()
        if not raw_id:
            return _error(
                "id is required. Call search_docs first, then pass an id from its results.",
                example='fetch_doc(id="aiven-manage-database")',
            )
        if len(raw_id) > MAX_ID_LEN:
            return _error(f"id must be {MAX_ID_LEN} characters or fewer.")

        requested_section = (section or "").strip()
        if len(requested_section) > MAX_SECTION_LEN:
            return _error(f"section must be {MAX_SECTION_LEN} characters or fewer.")

        doc = self.resolve(raw_id)
        if doc is None:
            operation = _reference_operation(raw_id)
            if operation is not None:
                return _error(
                    "No API operation matches that reference URL.",
                    did_you_mean=self.search_index.suggest(operation),
                    hint="Ids come from search_docs results. Use the id field verbatim.",
                )
            # Fuzzy-matching a rejected URL against document titles produces
            # nonsense suggestions. Say why the URL was refused instead.
            if raw_id.startswith(("http://", "https://")):
                try:
                    normalize_guide_id(raw_id)
                except InvalidDocId as exc:
                    return _error(
                        f"That URL is not a readable Akamai Cloud guide: {exc}.",
                        hint="Prefer the slug from a search_docs result, such as "
                        '"aiven-manage-database".',
                    )
            return _error(
                f"No document matches id {raw_id!r}.",
                did_you_mean=self.search_index.suggest(raw_id),
                hint="Ids come from search_docs results. Use the id field verbatim.",
            )

        if doc.get("kind") == "api":
            return self._api_result(doc)
        return self._guide_result(doc, requested_section)

    def _api_result(self, doc: dict) -> dict:
        """An operation card. Cards are small, so they are never sectioned."""
        result = {
            "id": doc["id"],
            "kind": "api",
            "title": doc.get("title", ""),
            "url": doc.get("url", ""),
            "content": doc.get("card") or doc.get("text", ""),
        }
        if doc.get("cli"):
            result["cli"] = doc["cli"]
        note = self.staleness_note()
        if note:
            result["note"] = note
        return result

    def _guide_result(self, doc: dict, requested_section: str) -> dict:
        slug = doc["id"]
        page_url = guide_url(slug, markdown=False)

        body, note = self._load_guide_body(doc)
        if body is None:
            return _error(f"Could not read guide {slug!r}: {note}", url=page_url)

        document = parse_document(body, fallback_title=doc.get("title", ""))
        title = document.title or doc.get("title", "")

        if requested_section:
            target = document.flat.get(requested_section)
            if target is None:
                return _error(
                    f"Section {requested_section!r} does not exist in {slug!r}.",
                    valid_sections=self._valid_sections(document),
                    hint="Pick a section id from valid_sections. Omit section to see "
                    "subsections too.",
                )
            content = section_text(document, target)
            truncated = len(content) > MAX_SECTION_CHARS
            if truncated:
                content = content[:MAX_SECTION_CHARS]
            result = {
                "id": slug,
                "kind": "guide",
                "title": title,
                "url": page_url,
                "section_id": target.id,
                "section_title": target.title,
                "content": content,
            }
            if truncated:
                result["truncated"] = True
            if note:
                result["note"] = note
            return result

        if len(body) < SMALL_DOC_BYTES or not document.sections:
            # The same ceiling as a section, flagged the same way, so a long
            # page with no headings still says where the rest is.
            truncated = len(body) > MAX_SECTION_CHARS
            result = {
                "id": slug,
                "kind": "guide",
                "title": title,
                "url": page_url,
                "content": body[:MAX_SECTION_CHARS] if truncated else body,
                "document_small": True,
            }
            if truncated:
                result["truncated"] = True
            if note:
                result["note"] = note
            return result

        # No kind, url or hint here. The id says which kind it is, the section
        # result carries the url, and the descriptions already give the workflow.
        result = {
            "id": slug,
            "title": title,
            "preamble": document.preamble[:2000],
            "sections": build_toc(document),
        }
        if note:
            result["note"] = note
        return result

    def _load_guide_body(self, doc: dict) -> tuple[str | None, str]:
        """Live markdown for a guide, falling back to the indexed copy.

        Guides are always read live so an agent never quotes a stale page. When
        the fetch fails for any reason, a reset connection and an unknown
        charset included, the indexed copy is better than no answer, and the
        result says so.
        """
        try:
            body, _, _ = fetch_guide(doc["id"])
            return body, ""
        except Exception as exc:
            reason = short_reason(exc)
            stored = doc.get("text", "")
            if stored:
                return stored, f"Live fetch failed ({reason}); this is the indexed copy."
            return None, reason

    @staticmethod
    def _valid_sections(document) -> list[dict]:
        """Top-level sections only. The full outline is what the model just had."""
        return [
            {"id": section.id, "title": section.title}
            for section in document.sections[:MAX_VALID_SECTIONS]
        ]


# --- rendering ---------------------------------------------------------------


def render_result(payload) -> str:
    """The text a model reads for one tool result.

    Search lists, tables of contents and errors are structured data a model
    indexes into, so they stay compact JSON. A section, a small guide and an
    API card are markdown already; they go out as markdown under one header
    line rather than as a JSON string with every newline escaped, which
    measured 5 to 13 percent more tokens for the same text.
    """
    if isinstance(payload, dict) and "error" not in payload:
        if payload.get("kind") == "api":
            return _render_api(payload)
        if "section_id" in payload:
            header = (
                f"# {payload.get('title', '')} > {payload.get('section_title', '')} "
                f"[{payload.get('id', '')} #{payload['section_id']}] {payload.get('url', '')}"
            )
            return _render_text(header, payload)
        if payload.get("document_small"):
            header = f"# {payload.get('title', '')} [{payload.get('id', '')}] {payload.get('url', '')}"
            return _render_text(header, payload)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _render_text(header: str, payload: dict) -> str:
    """A header line, then the note and truncation lines a model must not miss."""
    lines = [header]
    if payload.get("note"):
        lines.append(payload["note"])
    if payload.get("truncated"):
        lines.append(
            f"Truncated at {MAX_SECTION_CHARS:,} characters. The rest is at the URL above."
        )
    return "\n".join(lines) + "\n\n" + payload.get("content", "")


def _render_api(payload: dict) -> str:
    """The card alone. It already carries the CLI line and the reference URL."""
    text = payload.get("content", "")
    if payload.get("note"):
        text = text.rstrip("\n") + "\n\n" + payload["note"]
    return text


# --- process-wide service ----------------------------------------------------

_service: DocsService | None = None
#: Modification time of the index file the service was loaded from. None for a
#: service installed with `set_service`, which turns the reload check off.
_service_mtime: float | None = None
#: The last exception a load raised, so a transport can report a failed
#: background build instead of waiting for one that will never finish.
_load_error: Exception | None = None
_service_lock = threading.Lock()


def is_ready() -> bool:
    """True once a service is installed.

    Never takes the lock. The stdio transport builds the index in a thread that
    holds the lock for the whole build, and a tool body asks this to answer
    "still building" instead of queueing behind it.
    """
    return _service is not None


def load_error() -> Exception | None:
    """The exception from the most recent failed load, or None."""
    return _load_error


def current_service() -> DocsService | None:
    """The installed service, or None. Never takes the lock and never loads.

    A reload that fails leaves the previous service in place, so this is what
    a health check should report after a half-written index file.
    """
    return _service


def load_service(*, auto_sync: bool = True, quiet: bool = False) -> DocsService:
    """Return the process-wide service, building the index once if needed.

    stdio clients get an automatic first-use sync, which takes 20 to 60 seconds.
    Server deployments should run `sync` at boot instead.

    A service loaded from disk is replaced when the index file's modification
    time changes, so a `sync` on a timer reaches a running HTTP server without
    a restart. `write_index` writes to a temporary file and renames it, so the
    check never sees a half-written index. A reload that fails anyway, on a
    file some other writer damaged, keeps the previous service and records
    the error; only a first load raises.
    """
    global _service, _service_mtime, _load_error
    current = _service
    if current is not None and not _index_changed():
        return current
    with _service_lock:
        if _service is None or _index_changed():
            # Stat before reading. If the file is replaced between the two,
            # the recorded time is older than the content, and the next call
            # reloads once more, which is harmless. The other order could
            # record a time newer than the content and never reload.
            before = _current_mtime()
            # Cleared before the attempt, not after it. A transport that finds
            # no service and no error answers "still building", which is the
            # truth while the stdio builder's retry runs; the failure it
            # would otherwise report is the one that retry is fixing.
            _load_error = None
            try:
                index = _read_index(auto_sync=auto_sync, quiet=quiet)
                # Inside the try: a damaged prebuilt block fails here, and the
                # stdio builder thread reports whatever is recorded.
                service = DocsService(index)
            except Exception as exc:
                _load_error = exc
                if _service is None:
                    raise
                # A reload, not a first load. The file's time is recorded so
                # it is parsed once, not on every call; the next sync changes
                # the time and gets its own attempt. One stderr line, since
                # the tool results carry no sign of it.
                if before is not None:
                    _service_mtime = before
                print(
                    f"Reloading the index failed: {short_reason(exc)}. Still serving the "
                    f"index built at {_service.built_at or 'unknown'}.",
                    file=sys.stderr,
                )
                return _service
            _service = service
            _service_mtime = before if before is not None else _current_mtime()
    return _service


def set_service(service: DocsService | None) -> None:
    """Install a service directly. Used by tests and by the Functions handler.

    An installed service is never replaced by a change on disk; there may be no
    index file at all.
    """
    global _service, _service_mtime, _load_error
    with _service_lock:
        _service = service
        _service_mtime = None
        _load_error = None


def _current_mtime() -> float | None:
    try:
        return index_path().stat().st_mtime
    except OSError:
        return None


def _index_changed() -> bool:
    """True when the service came from disk and the file has since changed.

    A file that has gone missing does not count as a change. The loaded index
    is still a good answer, and a reload would only fail.
    """
    if _service_mtime is None:
        return False
    current = _current_mtime()
    return current is not None and current != _service_mtime


def _read_index(*, auto_sync: bool, quiet: bool) -> dict:
    from ..sync import SyncError, load_index, sync

    try:
        return load_index()
    except (FileNotFoundError, NotADirectoryError):
        if not auto_sync:
            raise
    except SyncError as exc:
        # load_index names the file in its message. Re-raise as the kind of
        # error a truncated file gives, so a transport maps both to one
        # sentence with no path in it.
        raise ValueError("the index file is not a valid index") from exc
    sync(quiet=quiet)
    return load_index()


def staleness_line(service: DocsService) -> str:
    """One sentence for an operator when the index is past its freshness window.

    Empty when fresh. Meant for stderr at startup or a status page, never for a
    tool result: what to run is advice for the person who owns the cache.
    """
    age = service.index_age_days()
    if age is None or age <= STALE_AFTER_DAYS:
        return ""
    return f"Index built {age:.0f} days ago; run akamai-cloud-docs-mcp sync to rebuild."


def search_docs(query: str, k: int = DEFAULT_K) -> list[dict]:
    """Module-level convenience wrapper over the loaded service."""
    return load_service().search_docs(query, k)


def fetch_doc(id: str, section: str = "") -> dict:  # noqa: A002
    """Module-level convenience wrapper over the loaded service."""
    return load_service().fetch_doc(id, section)
