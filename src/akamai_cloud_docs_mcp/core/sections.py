"""Markdown structure: table of contents, section extraction, summaries, snippets.

Section ids are dotted paths over the heading tree: `1`, `2`, `2.1`, `2.1.3`.
A model can read a table of contents and ask for exactly one section, which is
the whole point of the fetch_doc contract.

A table of contents entry is `{"id", "title"}` plus, at the top level, one of
two optional keys. `summary` is the first sentence of the section, flattened to
plain text. `content` replaces it when the section has no children and its body
is under `INLINE_CONTENT_BYTES`: the entry then carries the whole body as raw
markdown, so a caller never spends a second call on a section shorter than the
framing around it. Children carry `id` and `title` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The title group is greedy and the caller strips it. A lazy `(.*?)` followed
# by `[ \t]*$` re-scanned a run of spaces from every position, so one heading
# with 40,000 spaces in it took 6 s to match.
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*))?$")
# Fences may sit at any indentation. Inside a list item a fence is indented by
# the item's width, and the same fence often opens at 3 spaces and closes at 4.
# Matching only 0 to 3 spaces left such a fence open for the rest of the file
# and every later heading disappeared. A fence may also open on the same line
# as its list marker (`- ```python`); missing that opener made its closer open
# a phantom fence with the same result. A backtick fence cannot carry another
# backtick on its line, so "```foo``` is a tag" is a code span in prose, not a
# fence. core/catalog.py states the same rule; keep the two in sync.
_FENCE_RE = re.compile(r"^\s*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?(`{3,}(?![^\n]*`)|~{3,})")

_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
# A backslash before ASCII punctuation is an escape and the backslash goes.
# `mod\_status` in a heading reached the table of contents with its backslash.
_ESCAPE_RE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
# `<https://example.com>` and `<user@example.com>` are links, not tags.
_AUTOLINK_RE = re.compile(
    r"<((?:[a-z][a-z0-9+.-]*://|mailto:)[^\s<>]+|[^\s<>@]+@[^\s<>]+)>", re.IGNORECASE
)
# The span between the markers may cross a lone marker (`5 * 3`), which can
# neither open nor close emphasis, and stops at any other marker. With a plain
# `.+?` every opener that never closes scanned to the end of its line, so a
# 60 KB line of ` *a` took 11 s; stopping at the next marker makes the pass
# linear and the same 60 KB takes under 20 ms.
_EMPHASIS_STAR_RE = re.compile(r"(\*{1,3})(?=\S)((?:[^*\n]|(?<=\s)\*(?!\S))+?)(?<=\S)\1")
# Underscore emphasis cannot be intraword, so `thread_pool_size` keeps its
# underscores. The span may also cross an intraword underscore, which can
# neither open nor close either, so `_foo_bar_` is still emphasis around
# `foo_bar`. Asterisks carry no such rule.
_EMPHASIS_UNDERSCORE_RE = re.compile(
    r"(?<!\w)(_{1,3})(?=\S)((?:[^_\n]|(?<=\w)_(?=\w)|(?<=\s)_(?!\S))+?)(?<=\S)\1(?!\w)"
)
_CODE_SLOT_RE = re.compile("\x00(\\d+)\x00")
# Only the tags the docs actually use are stripped. A bare `<[^>]+>` also ate
# `<placeholder>` values and everything between `a < b` and `c > d`.
_HTML_TAG_NAMES = (
    "a|abbr|b|body|br|caption|code|col|colgroup|dd|details|div|dl|dt|em|h[1-6]|head|hr|html"
    "|i|img|kbd|li|ol|p|pre|s|span|strong|sub|summary|sup|table|tbody|td|th|thead|tr|u|ul"
    "|tab|tabs|akamaitab|akamaitabs"
)
# Comments are not in this pattern: `_strip_closed_comments` below drops them
# with `str.find`, because a `<!--.*?-->` branch scanned to the end of the text
# from every `<!--` that never closed.
_HTML_TAG_RE = re.compile(
    rf"</?(?:{_HTML_TAG_NAMES})(?:[ \t][^<>]{{0,200}})?/?>",
    re.IGNORECASE,
)
_LIST_PREFIX_RE = re.compile(r"^\s{0,8}(?:[-*+]|\d{1,3}[.)])\s+")
_QUOTE_PREFIX_RE = re.compile(r"^\s{0,4}>\s?")
_WS_RE = re.compile(r"\s+")

SUMMARY_CHARS = 160
SNIPPET_CHARS = 240
# A childless top-level section under this size goes into the table of contents
# whole. Fetching it separately costs more in framing than the body itself; 7%
# of top-level sections in the live corpus were this small.
INLINE_CONTENT_BYTES = 300


@dataclass
class Section:
    """One heading and everything under it."""

    id: str
    title: str
    level: int
    start: int  # line index of the heading itself
    end: int  # exclusive, first line past this section's subtree
    children: list[Section] = field(default_factory=list)


@dataclass
class Document:
    """A parsed guide."""

    title: str
    lines: list[str]
    preamble: str
    sections: list[Section]
    flat: dict[str, Section]

    def section_ids(self) -> list[str]:
        return list(self.flat.keys())


# --- text helpers ------------------------------------------------------------


def _strip_closed_comments(text: str) -> str:
    """Drop every `<!-- ... -->` span. An opener that never closes stays as text.

    The first `-->` after an opener closes it, whatever sits between, which is
    how a renderer reads a comment too. Each pass moves past the comment it
    removed, so the cost is one scan of the text.
    """
    out: list[str] = []
    position = 0
    while True:
        start = text.find("<!--", position)
        if start == -1:
            break
        close = text.find("-->", start + 4)
        if close == -1:
            break
        out.append(text[position:start])
        position = close + 3
    out.append(text[position:])
    return "".join(out)


def _strip_closing_hashes(title: str) -> str:
    """Drop a closing `#` sequence from a heading title.

    `## Title ##` is `Title`, but `Using C#` keeps its `#`: the sequence only
    counts after a space, or when it is the whole title. `title` arrives
    stripped. A regex for this backtracked over the whitespace run from every
    position and took 9 s on a title of 20,000 spaces and 20,000 hashes.
    """
    core = title.rstrip("#")
    if core != title and (not core or core[-1] in " \t"):
        return core.strip()
    return title


def flatten_inline(text: str) -> str:
    """Strip inline markup only: images, links, code spans, emphasis, html.

    This is the pass a heading title gets. A title like "1. Create a cluster"
    keeps its ordinal because the list-marker pass never runs on it.

    A code span holds literal text, so its contents are set aside while the
    emphasis passes run. Unwrapped in place, a heading like
    `` `thread_pool_size` `` came back as "threadpoolsize", which is the wrong
    name for a config key a caller is about to type. A backslash-escaped
    character is set aside the same way, after the code spans, so an escaped
    underscore inside a span stays as written and an escaped star outside one
    is a literal star, not emphasis.
    """
    out = _IMAGE_RE.sub(r"\1", text.replace("\x00", ""))
    out = _LINK_RE.sub(r"\1", out)
    out = _AUTOLINK_RE.sub(r"\1", out)

    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    out = _INLINE_CODE_RE.sub(stash, out)
    out = _ESCAPE_RE.sub(stash, out)
    for pattern in (_EMPHASIS_STAR_RE, _EMPHASIS_UNDERSCORE_RE):
        # Innermost pair first: `**a *b* c**` loses `*b*` on the first pass
        # and the outer pair on the second. A single pass stripped one pair
        # and left the other's markers in 12 of 394 guides.
        for _ in range(3):
            out, count = pattern.subn(r"\2", out)
            if not count:
                break
    out = _strip_closed_comments(out)
    out = _HTML_TAG_RE.sub("", out)
    if spans:
        out = _CODE_SLOT_RE.sub(lambda match: spans[int(match.group(1))], out)
    return _WS_RE.sub(" ", out).strip()


def flatten_markdown(text: str) -> str:
    """Reduce markdown to readable plain text for summaries and snippets.

    The inline pass plus the line-prefix pass: a leading quote or list marker
    goes too, so a summary that starts on a bullet reads as a sentence.
    """
    out = _QUOTE_PREFIX_RE.sub("", text)
    out = _LIST_PREFIX_RE.sub("", out)
    return flatten_inline(out)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return f"{cut or text[:limit]}..."


def _iter_prose_lines(lines: list[str]):
    """Yield `(index, line)` for every line outside a fenced code block.

    Fences pair by marker character only: a backtick fence closes on the next
    backtick fence, at any indentation and any length. The section splitter and
    the content iterator share this so they can never disagree about where a
    code block ends.
    """
    fence: str | None = None
    for index, line in enumerate(lines):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        yield index, line


def _iter_content_lines(lines: list[str]) -> list[str]:
    """Lines outside headings and fenced code.

    Table rows and rules pass through. summarize() skips them; snippet() does
    not, so a table-heavy section can open its snippet with a row.
    """
    return [line for _, line in _iter_prose_lines(lines) if not _HEADING_RE.match(line)]


def summarize(text: str, limit: int = SUMMARY_CHARS) -> str:
    """First sentence of the first real paragraph, flattened and truncated."""
    for line in _iter_content_lines(text.splitlines()):
        flat = flatten_markdown(line)
        if len(flat) < 3:
            continue
        if flat.startswith(("|", "---", ":--")):
            continue
        sentence = _first_sentence(flat)
        return _truncate(sentence, limit)
    return ""


def _first_sentence(text: str) -> str:
    """Cut at the first sentence end, ignoring common abbreviations."""
    for match in re.finditer(r"[.!?](?=\s|$)", text):
        head = text[: match.start()]
        tail = head.rsplit(" ", 1)[-1].lower()
        if tail in {"e.g", "i.e", "etc", "vs", "no", "fig", "approx"}:
            continue
        if len(head) < 25:
            continue
        return text[: match.end()]
    return text


def snippet(text: str, terms: list[str], limit: int = SNIPPET_CHARS) -> str:
    """A window of `text` around the first matching term, or its opening lines."""
    content = "\n".join(_iter_content_lines(text.splitlines()))
    flat = flatten_markdown(content)
    if not flat:
        return ""
    lowered = flat.lower()
    position = -1
    for term in terms:
        found = lowered.find(term)
        if found != -1 and (position == -1 or found < position):
            position = found
    if position <= limit // 3:
        return _truncate(flat, limit)
    start = max(0, position - limit // 3)
    start = flat.rfind(" ", 0, start) + 1 if flat.rfind(" ", 0, start) != -1 else start
    window = flat[start : start + limit]
    if len(flat) > start + limit:
        window = window.rsplit(" ", 1)[0] + "..."
    return f"...{window}"


# --- parsing -----------------------------------------------------------------


def _scan_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return `(line_index, level, title)` for every heading outside code fences."""
    headings: list[tuple[int, int, str]] = []
    for index, line in _iter_prose_lines(lines):
        heading_match = _HEADING_RE.match(line)
        if not heading_match:
            continue
        level = len(heading_match.group(1))
        raw_title = (heading_match.group(2) or "").strip()
        title = flatten_inline(_strip_closing_hashes(raw_title))
        if not title:
            continue
        headings.append((index, level, title))
    return headings


def _is_bare_title(lines: list[str], headings: list[tuple[int, int, str]]) -> bool:
    """True when the first heading is a lone title line above the first section.

    Many techdocs pages use one heading level for both the page title and every
    section, so depth alone cannot tell them apart. The title is still a title
    when nothing at all sits between it and the next heading of its own level.
    Without this, that heading becomes an empty section 1 and a model that asks
    for it gets back a heading and no content.
    """
    first_index, first_level, _ = headings[0]
    next_index, next_level, _ = headings[1]
    if next_level > first_level:
        return False
    return all(not line.strip() for line in lines[first_index + 1 : next_index])


def parse_document(markdown: str, fallback_title: str = "") -> Document:
    """Parse a guide into a title, a preamble, and a heading tree.

    The first heading is treated as the document title when it is the shallowest
    heading in the file, or when it is a bare title line above a section of its
    own level. Everything after it, up to the first section heading, is the
    preamble.
    """
    lines = markdown.splitlines()
    headings = _scan_headings(lines)

    title = fallback_title
    body_start = 0
    if headings:
        first_index, first_level, first_title = headings[0]
        deeper = [level for _, level, _ in headings[1:]]
        if (
            not deeper
            or first_level < min(deeper)
            or (first_level == min(deeper) and _is_bare_title(lines, headings))
        ):
            title = first_title
            body_start = first_index + 1
            headings = headings[1:]

    if not headings:
        preamble = "\n".join(lines[body_start:]).strip()
        return Document(title=title, lines=lines, preamble=preamble, sections=[], flat={})

    top_level = min(level for _, level, _ in headings)
    preamble = "\n".join(lines[body_start : headings[0][0]]).strip()

    sections: list[Section] = []
    flat: dict[str, Section] = {}
    stack: list[Section] = []
    counters: dict[str, int] = {}

    for position, (line_index, level, heading_title) in enumerate(headings):
        while stack and stack[-1].level >= level:
            stack.pop()
        parent_id = stack[-1].id if stack else ""
        counters[parent_id] = counters.get(parent_id, 0) + 1
        section_id = f"{parent_id}.{counters[parent_id]}" if parent_id else str(counters[parent_id])

        end = len(lines)
        for next_index, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break

        section = Section(
            id=section_id, title=heading_title, level=level, start=line_index, end=end
        )
        flat[section_id] = section
        if stack:
            stack[-1].children.append(section)
        elif level == top_level:
            sections.append(section)
        else:
            sections.append(section)
        stack.append(section)

    return Document(title=title, lines=lines, preamble=preamble, sections=sections, flat=flat)


def section_text(document: Document, section: Section, *, include_heading: bool = True) -> str:
    """Raw markdown for a section, children included."""
    start = section.start if include_heading else section.start + 1
    return "\n".join(document.lines[start : section.end]).strip("\n")


def get_section(document: Document, section_id: str) -> Section | None:
    """Look up a section by dotted id."""
    return document.flat.get((section_id or "").strip())


def _toc_entry(document: Document, section: Section, depth: int, with_summary: bool) -> dict:
    entry: dict = {"id": section.id, "title": section.title}
    if with_summary:
        body = section_text(document, section, include_heading=False)
        # section_text strips newlines only, so a body of blank lines with
        # spaces is truthy. Nothing to read means no `content` key.
        if (
            body.strip()
            and not section.children
            and len(body.encode("utf-8")) < INLINE_CONTENT_BYTES
        ):
            entry["content"] = body
        else:
            summary = summarize(body)
            if summary:
                entry["summary"] = summary
    if section.children and depth > 0:
        entry["children"] = [
            _toc_entry(document, child, depth - 1, False) for child in section.children
        ]
    return entry


def build_toc(document: Document, *, depth: int = 2) -> list[dict]:
    """Table of contents: top-level sections with summaries, children by title.

    Summaries live only at the top level. Repeating them for every child is the
    single biggest token cost in a table of contents and buys little. A small
    childless section carries its body as `content` instead; see the module
    docstring.
    """
    return [_toc_entry(document, section, depth - 1, True) for section in document.sections]
