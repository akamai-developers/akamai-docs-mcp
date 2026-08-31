"""Guide catalog: llms.txt parsing, id validation, and markdown fetching.

Guide pages on techdocs.akamai.com serve raw markdown when `.md` is appended to
the page URL. `fetch_doc` only ever fetches those pages. `fetch_url` below is
also used by `sync` (and by auto-sync on first start) to pull the guides
llms.txt index and the Linode OpenAPI spec from raw.githubusercontent.com.
There is no HTML parsing anywhere.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..config import (
    DOCS_BASE,
    DOCS_HOST,
    DOCS_PATH_PREFIX,
    MAX_ID_LEN,
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT,
    USER_AGENT,
)


class CatalogError(Exception):
    """Base class for catalog failures that are safe to show a caller."""


class InvalidDocId(CatalogError):
    """The supplied id is not a slug or an allowed techdocs URL."""


class FetchError(CatalogError):
    """A page could not be fetched."""


@dataclass(frozen=True)
class LinkEntry:
    """One `- [Title](url): description` line from an llms.txt index."""

    slug: str
    title: str
    url: str
    description: str = ""


# `- [Title](https://...): optional trailing description`
_LINK_RE = re.compile(r"^\s*-\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)\)\s*(?::\s*(?P<desc>.*))?$")

# Slugs seen in the wild are lowercase alphanumerics with hyphens. Underscores and
# dots are allowed defensively; `..` is rejected outright below.
# `\Z` rather than `$`, because `$` also matches before a trailing newline, which
# would let "slug\n" through.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# Every guide page and the guides llms.txt carry this line. It is navigation
# boilerplate, not content, and it skews term frequencies if kept.
_BOILERPLATE_RE = re.compile(
    r"^Fetch the complete documentation index at:.*$",
    re.MULTILINE,
)

# techdocs expands `<<NAME>>` variables when it renders HTML, but the raw
# markdown endpoint serves them unexpanded. Only names seen in the corpus are
# mapped; anything else stays as written, since `<< EOF >>` inside a shell
# heredoc looks the same and must survive. The dash variable joins an
# appositive ("a widget<<CJAR_DASH_LONG>>a thing that holds sprockets"), which
# reads as a comma in plain text. A live example is on the guide
# access-buckets-and-files-through-urls.
_TEMPLATE_VARIABLES = {
    "AKAMAI CLOUD": "Akamai Cloud",
    "CJAR_DASH_LONG": ", ",
}
_TEMPLATE_VARIABLE_RE = re.compile(r"<<([A-Za-z_ ]+)>>")


def resolve_template_variables(text: str) -> str:
    """Replace the known techdocs `<<NAME>>` variables with their plain text."""

    def replace(match: re.Match[str]) -> str:
        return _TEMPLATE_VARIABLES.get(match.group(1), match.group(0))

    return _TEMPLATE_VARIABLE_RE.sub(replace, text)


_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", re.DOTALL)

# Every guide ends with link lists to its neighbours: `Sub pages` for children,
# then `Sibling pages` for peers, and on a few pages a `What's next` list after
# those. They are navigation, already reachable through search_docs, and their
# titles skew term frequencies on every page that carries them.
_SIBLING_RE = re.compile(r"^#{1,6}[ \t]+(?:Sub|Sibling) pages[ \t]*$", re.MULTILINE | re.IGNORECASE)
# The link text runs to the first `](`, so a title may carry `]` but the
# pattern never tries a second split. With `\[.*\]` it retried the URL part
# from every `](` on the line and a 120 KB line took 3 s.
_NAV_LINK_RE = re.compile(r"^\s*[-*+]\s+\[(?:[^\]\n]|\](?!\())*\]\(\S+\)\s*$")
_HEADING_LINE_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+\S")
_NEXT_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+\S", re.MULTILINE)

# Fences pair by marker character at any indentation. A fence may open on its
# list-marker line, and a backtick fence line carries no second backtick, so
# "```foo``` is a tag" is prose. core/sections.py states the same rule; keep
# the two in sync so they can never disagree about where a code block ends.
_FENCE_RE = re.compile(r"^\s*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?(`{3,}(?![^\n]*`)|~{3,})")

# readme.io stores tables, figures and raw html as `[block:kind]` followed by a
# JSON object and `[/block]`. Most sit on their own lines, pretty-printed; about
# a third are squeezed onto one line.
_BLOCK_OPEN_RE = re.compile(r"\[block:(?P<kind>[a-z-]+)\]")
_BLOCK_CLOSE = "[/block]"
_CELL_KEY_RE = re.compile(r"^(h|\d+)-(\d+)$")
_CELL_BREAK_RE = re.compile(r"(?:<br\s*/?>|\n)+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


# --- ids and URLs ------------------------------------------------------------


def is_valid_slug(slug: str) -> bool:
    """True when `slug` is a safe single path segment."""
    if not slug or len(slug) > MAX_ID_LEN:
        return False
    if ".." in slug or "/" in slug or "\\" in slug:
        return False
    return bool(_SLUG_RE.match(slug))


def validate_docs_url(url: str) -> str:
    """Return `url` unchanged if it is a fetchable guide URL, else raise.

    Requires https, the exact techdocs host, and a path under
    `/cloud-computing/docs/`. Rejects traversal, credentials, ports, queries,
    and fragments so a caller cannot steer the fetcher off the allowlist.
    """
    if not url or len(url) > MAX_ID_LEN * 4:
        raise InvalidDocId("URL is empty or too long")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise InvalidDocId(f"only https URLs are allowed, got {parts.scheme or 'none'!r}")
    if parts.hostname != DOCS_HOST:
        raise InvalidDocId(f"only {DOCS_HOST} URLs are allowed")
    if parts.port is not None or parts.username or parts.password:
        raise InvalidDocId("URL must not carry a port or credentials")
    if parts.query or parts.fragment:
        raise InvalidDocId("URL must not carry a query string or fragment")
    path = parts.path
    if not path.startswith(DOCS_PATH_PREFIX):
        raise InvalidDocId(f"URL path must start with {DOCS_PATH_PREFIX}")
    if ".." in path or "//" in path or "%" in path:
        raise InvalidDocId("URL path contains a disallowed sequence")
    remainder = path[len(DOCS_PATH_PREFIX) :]
    if not remainder or "/" in remainder:
        raise InvalidDocId("URL must point at a single guide page")
    return url


def slug_from_url(url: str) -> str:
    """Extract the guide slug from a techdocs URL. Validates first."""
    validate_docs_url(url)
    tail = urlsplit(url).path[len(DOCS_PATH_PREFIX) :]
    if tail.endswith(".md"):
        tail = tail[:-3]
    if not is_valid_slug(tail):
        raise InvalidDocId(f"{tail!r} is not a valid guide slug")
    return tail


def guide_url(slug: str, markdown: bool = True) -> str:
    """Build the canonical guide URL for `slug`."""
    if not is_valid_slug(slug):
        raise InvalidDocId(f"{slug!r} is not a valid guide slug")
    return f"{DOCS_BASE}{slug}.md" if markdown else f"{DOCS_BASE}{slug}"


def normalize_guide_id(raw: str) -> str:
    """Turn a slug or a full techdocs URL into a bare slug.

    Slugs are preferred everywhere in tool output. This accepts a URL because
    models copy them out of search results and prose.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise InvalidDocId("id is empty")
    if len(candidate) > MAX_ID_LEN:
        raise InvalidDocId(f"id is longer than {MAX_ID_LEN} characters")
    if candidate.startswith(("http://", "https://")):
        # techdocs serves the page with a fragment or a trailing slash, and
        # models copy both out of prose. A query string is still rejected.
        candidate = candidate.split("#", 1)[0]
        if candidate.endswith("/"):
            candidate = candidate[:-1]
        return slug_from_url(candidate)
    if candidate.endswith(".md"):
        candidate = candidate[:-3]
    if not is_valid_slug(candidate):
        raise InvalidDocId(f"{raw!r} is not a guide slug or a {DOCS_HOST} URL")
    return candidate


# --- llms.txt ----------------------------------------------------------------


def parse_llms_txt(text: str) -> list[LinkEntry]:
    """Parse an llms.txt index into guide entries.

    Keeps only links to individual guide pages. Category index links (which end
    in `llms.txt`) and off-site links are dropped. Duplicates collapse to the
    first occurrence, which is the one under its own category heading.
    """
    entries: list[LinkEntry] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _LINK_RE.match(line)
        if not match:
            continue
        url = match.group("url").strip()
        if url.endswith("llms.txt"):
            continue
        try:
            slug = slug_from_url(url)
        except InvalidDocId:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        entries.append(
            LinkEntry(
                slug=slug,
                title=match.group("title").strip(),
                url=guide_url(slug),
                description=(match.group("desc") or "").strip(),
            )
        )
    return entries


# --- fetching ----------------------------------------------------------------


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target before following it.

    A redirect is an attacker-controlled URL as far as this process is
    concerned, so the target has to clear the same bar as the original request.
    The allowed hosts ride on the Request object because urllib builds handlers
    per opener, not per call.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        allowed = getattr(req, "allowed_hosts", None) or set()
        parts = urlsplit(newurl)
        if parts.scheme != "https":
            raise FetchError(f"refused a redirect to a non-https URL: {parts.scheme}")
        if parts.hostname not in allowed:
            raise FetchError(f"refused a redirect to an unexpected host: {parts.hostname}")
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None:
            new_request.allowed_hosts = allowed
        return new_request


_opener = urllib.request.build_opener(_AllowlistRedirectHandler)


def fetch_url(
    url: str,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    timeout: float = REQUEST_TIMEOUT,
    allowed_redirect_hosts: set[str] | None = None,
) -> str:
    """Fetch `url` as text with a size cap and an explicit User-Agent.

    Only https is allowed. Redirects may only land on `allowed_redirect_hosts`,
    which defaults to the host of `url`. Host allowlisting for the original URL
    is the caller's job; the guide path goes through `fetch_guide`, which
    validates against the docs allowlist first.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchError("only https URLs can be fetched")
    if not parts.hostname:
        raise FetchError("URL has no host")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    request.allowed_hosts = allowed_redirect_hosts or {parts.hostname}
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            charset = response.headers.get_content_charset() or "utf-8"
        if len(raw) > max_bytes:
            raise FetchError(f"{url} exceeded the {max_bytes} byte response cap")
        return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url} returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise FetchError(f"{url} could not be reached: {exc.reason}") from None
    except TimeoutError:
        raise FetchError(f"{url} timed out after {timeout:.0f}s") from None
    except (OSError, http.client.HTTPException, LookupError) as exc:
        # A reset or a short read during response.read(), or a charset the
        # codec registry does not know. None of these carry a response body.
        # Every caller treats FetchError as "use the indexed copy instead".
        raise FetchError(f"{url} read failed: {exc}") from None


def strip_frontmatter(text: str) -> tuple[str, str]:
    """Split leading YAML frontmatter off a guide body.

    Returns `(body, updated_at)`. `updated_at` is empty when absent.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text, ""
    updated_at = ""
    for line in match.group("body").splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "updatedAt":
            updated_at = value.strip()
            break
    return text[match.end() :], updated_at


def strip_sibling_pages(text: str) -> str:
    """Drop the trailing navigation lists (`Sub pages`, `Sibling pages`).

    Cuts at the first navigation heading with nothing but link lines and more
    headings over link lines after it, which also takes the `What's next` list
    a few pages carry last. When prose follows every navigation heading, each
    navigation heading whose own block is only link lines goes on its own and
    the rest of the page stays. A heading with prose under it is content and
    stays either way.
    """
    matches = list(_SIBLING_RE.finditer(text))
    for match in matches:
        tail = text[match.end() :].splitlines()
        if all(_is_nav_line(line) for line in tail):
            return text[: match.start()]
    # A single cut at the last navigation heading used to be the fallback. It
    # also took the prose after it, so each link-only block is removed on its
    # own instead. Later blocks go first so earlier offsets stay valid.
    for match in reversed(matches):
        next_heading = _NEXT_HEADING_RE.search(text, match.end())
        block_end = next_heading.start() if next_heading else len(text)
        block = text[match.end() : block_end].splitlines()
        if all(not line.strip() or _NAV_LINK_RE.match(line) for line in block):
            text = text[: match.start()] + text[block_end:]
    return text


def _is_nav_line(line: str) -> bool:
    return not line.strip() or bool(_NAV_LINK_RE.match(line) or _HEADING_LINE_RE.match(line))


def strip_html_comments(text: str) -> str:
    """Drop `<!-- ... -->` spans, and everything after a `<!--` that never closes.

    A comment is invisible on the rendered page, so it is not documentation.
    One live guide carried two whole sections inside a comment that was never
    closed, and search ranked the page first on that hidden text. Fenced code
    is left alone so a sample that shows a comment survives. A comment that
    opens outside a fence still runs to the next `-->` wherever it is, which is
    how a markdown renderer reads it too.
    """
    out: list[str] = []
    fence: str | None = None
    open_comment = False
    for line in text.split("\n"):
        if open_comment:
            close = line.find("-->")
            if close == -1:
                continue
            line = line[close + 3 :]
            open_comment = False
        else:
            fence_match = _FENCE_RE.match(line)
            if fence_match:
                marker = fence_match.group(1)[0]
                if fence is None:
                    fence = marker
                elif marker == fence:
                    fence = None
                out.append(line)
                continue
            if fence is not None:
                out.append(line)
                continue
        while True:
            start = line.find("<!--")
            if start == -1:
                break
            close = line.find("-->", start + 4)
            if close == -1:
                line = line[:start]
                open_comment = True
                break
            line = line[:start] + line[close + 3 :]
        out.append(line)
    return "\n".join(out)


def _table_cell(value: object) -> str:
    """One pipe-table cell: breaks become ` / `, pipes are escaped."""
    text = "" if value is None else str(value)
    parts = [part.strip() for part in _CELL_BREAK_RE.split(text)]
    joined = " / ".join(part for part in parts if part)
    return _WS_RE.sub(" ", joined).replace("|", "\\|")


def _render_parameters(block: dict) -> str | None:
    """A `[block:parameters]` table as a pipe table.

    Cells are keyed `h-<col>` for the header row and `<row>-<col>` for the body.
    The `cols`, `rows` and `align` keys are ignored; the cell keys are the
    truth about the table's shape.
    """
    data = block.get("data")
    if not isinstance(data, dict):
        return None
    header: dict[int, str] = {}
    rows: dict[int, dict[int, str]] = {}
    for key, value in data.items():
        match = _CELL_KEY_RE.match(str(key))
        if not match:
            return None
        column = int(match.group(2))
        if match.group(1) == "h":
            header[column] = _table_cell(value)
        else:
            rows.setdefault(int(match.group(1)), {})[column] = _table_cell(value)
    columns = list(header) + [column for cells in rows.values() for column in cells]
    if not columns:
        return None
    width = max(columns) + 1
    lines = [
        "| " + " | ".join(header.get(column, "") for column in range(width)) + " |",
        "|" + " --- |" * width,
    ]
    for row in sorted(rows):
        cells = (rows[row].get(column, "") for column in range(width))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _render_image(block: dict) -> str | None:
    """A `[block:image]` figure as its caption, or its alt text, one line each.

    The URL, alignment and sizing are for a browser. A figure with neither a
    caption nor alt text is a bare screenshot and renders as nothing.
    """
    images = block.get("images")
    if not isinstance(images, list):
        return None
    lines: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            return None
        source = image.get("image")
        alt = source[2] if isinstance(source, list) and len(source) > 2 else ""
        text = image.get("caption") or alt
        if isinstance(text, str) and text.strip():
            lines.append(_WS_RE.sub(" ", text).strip())
    return "\n\n".join(lines)


def _render_html(block: dict) -> str | None:
    """A `[block:html]` block as the html it wraps, unescaped."""
    html = block.get("html")
    return html.strip() if isinstance(html, str) else None


_BLOCK_RENDERERS = {
    "parameters": _render_parameters,
    "image": _render_image,
    "html": _render_html,
}


def _render_block(kind: str, raw: str) -> str | None:
    renderer = _BLOCK_RENDERERS.get(kind)
    if renderer is None:
        return None
    try:
        block = json.loads(raw)
    except ValueError:
        return None
    return renderer(block) if isinstance(block, dict) else None


def convert_readme_blocks(text: str) -> str:
    """Rewrite readme.io `[block:...]` JSON as markdown.

    A parameters block becomes a pipe table, an image block its caption or alt
    text, an html block the html itself. The JSON form was a third larger than
    the table it encoded, and its first line became the section summary. A
    block that does not parse, or a kind this does not know, stays exactly as
    it was.
    """
    out: list[str] = []
    position = 0
    while True:
        opener = _BLOCK_OPEN_RE.search(text, position)
        if opener is None:
            break
        close = text.find(_BLOCK_CLOSE, opener.end())
        if close == -1:
            break
        rendered = _render_block(opener.group("kind"), text[opener.end() : close])
        if rendered is None:
            out.append(text[position : opener.end()])
            position = opener.end()
            continue
        out.append(text[position : opener.start()])
        out.append(rendered)
        position = close + len(_BLOCK_CLOSE)
    out.append(text[position:])
    return "".join(out)


def clean_guide_markdown(text: str) -> tuple[str, str]:
    """Reduce a raw guide page to its documentation.

    Frontmatter, the index boilerplate line, HTML comments and the trailing
    navigation lists go; readme.io block JSON becomes markdown and known
    techdocs `<<NAME>>` variables become their text. Line endings are
    normalized first so every pattern below can assume `\\n`. Blocks are
    converted before comments are stripped, so a comment inside the html of a
    `[block:html]` goes the same way as one in the page. The other order left
    a JSON-escaped `<!--` in place, and a literal one inside the JSON ate the
    block and everything after it.

    Returns `(body, updated_at)`. Raises `FetchError` when the response is HTML,
    which is what techdocs serves for a page that does not exist.
    """
    text = text.replace("\r\n", "\n")
    if text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        raise FetchError("response was HTML, not markdown; the page may not exist")
    body, updated_at = strip_frontmatter(text)
    body = _BOILERPLATE_RE.sub("", body)
    body = convert_readme_blocks(body)
    body = strip_html_comments(body)
    body = resolve_template_variables(body)
    body = strip_sibling_pages(body)
    return body.strip("\n"), updated_at


def fetch_guide(slug_or_url: str, *, timeout: float = REQUEST_TIMEOUT) -> tuple[str, str, str]:
    """Fetch one guide page as cleaned markdown.

    Returns `(body, updated_at, url)`. The id is normalized to a slug first, so
    a caller can never steer this at a URL outside the allowlist.
    """
    slug = normalize_guide_id(slug_or_url)
    url = guide_url(slug)
    validate_docs_url(url)
    body, updated_at = clean_guide_markdown(fetch_url(url, timeout=timeout))
    return body, updated_at, url
