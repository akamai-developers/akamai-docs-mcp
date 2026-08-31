"""Catalog parsing, id normalization, URL allowlisting, fetching, and cleaning."""

from __future__ import annotations

import http.client
import json
import urllib.error
from email.message import Message

import pytest

from akamai_cloud_docs_mcp.config import cache_dir, index_path
from akamai_cloud_docs_mcp.core import catalog
from akamai_cloud_docs_mcp.core.catalog import (
    FetchError,
    InvalidDocId,
    clean_guide_markdown,
    convert_readme_blocks,
    fetch_url,
    guide_url,
    is_valid_slug,
    normalize_guide_id,
    parse_llms_txt,
    resolve_template_variables,
    slug_from_url,
    strip_frontmatter,
    strip_html_comments,
    validate_docs_url,
)
from akamai_cloud_docs_mcp.core.sections import build_toc, parse_document

DOCS_BASE = "https://techdocs.akamai.com/cloud-computing/docs/"

# The navigation tail every live guide ends with: children first, then peers.
NAV_TAIL = f"""
# Sub pages

* [Pick a size]({DOCS_BASE}pick-a-size.md)
* [Name the widget]({DOCS_BASE}name-the-widget.md)

# Sibling pages

* [Resize a widget]({DOCS_BASE}resize-a-widget.md)
* [Delete a widget]({DOCS_BASE}delete-a-widget.md)
"""

PARAMETERS_BLOCK = """[block:parameters]
{
  "data": {
    "h-0": "Plan",
    "h-1": "Notes",
    "0-0": "Small",
    "0-1": "4 GB | 2 cores<br><br>Best for **tests**",
    "1-0": "Large",
    "1-1": "Line one  \\nLine two"
  },
  "cols": 2,
  "rows": 2,
  "align": ["left", "left"]
}
[/block]"""

PARAMETERS_TABLE = """| Plan | Notes |
| --- | --- |
| Small | 4 GB \\| 2 cores / Best for **tests** |
| Large | Line one / Line two |"""

IMAGE_BLOCK_INLINE = (
    '[block:image]{"images":[{"image":["https://img.example.invalid/dash.png",null,'
    '"The widget dashboard"],"align":"center","sizing":"400px"}]}[/block]'
)


class TestParseLlmsTxt:
    def test_keeps_only_guide_pages(self, llms_txt):
        entries = parse_llms_txt(llms_txt)
        assert [entry.slug for entry in entries] == [
            "create-a-widget",
            "resize-a-widget",
            "delete-a-widget",
            "sprocket-basics",
        ]

    def test_captures_title_and_description(self, llms_txt):
        first = parse_llms_txt(llms_txt)[0]
        assert first.title == "Create a widget"
        assert first.description == "start here"
        assert first.url.endswith("/create-a-widget.md")

    def test_drops_category_index_links(self, llms_txt):
        assert all(not entry.url.endswith("llms.txt") for entry in parse_llms_txt(llms_txt))

    def test_drops_off_host_and_insecure_links(self, llms_txt):
        slugs = {entry.slug for entry in parse_llms_txt(llms_txt)}
        assert "not-ours" not in slugs
        assert "insecure" not in slugs

    def test_empty_input(self):
        assert parse_llms_txt("") == []


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://techdocs.akamai.com/cloud-computing/docs/page.md",
            "https://evil.example.com/cloud-computing/docs/page.md",
            "https://techdocs.akamai.com.evil.com/cloud-computing/docs/page.md",
            "https://techdocs.akamai.com/linode-api/reference/get-linodes",
            "https://techdocs.akamai.com/cloud-computing/docs/../../etc/passwd",
            "https://techdocs.akamai.com/cloud-computing/docs/sub/page.md",
            "https://techdocs.akamai.com/cloud-computing/docs/page.md?x=1",
            "https://techdocs.akamai.com/cloud-computing/docs/page.md#frag",
            "https://user:pw@techdocs.akamai.com/cloud-computing/docs/page.md",
            "https://techdocs.akamai.com:8443/cloud-computing/docs/page.md",
            "https://techdocs.akamai.com/cloud-computing/docs/%2e%2e/secret.md",
            "file:///etc/passwd",
            "",
        ],
    )
    def test_rejects(self, url):
        with pytest.raises(InvalidDocId):
            validate_docs_url(url)

    def test_accepts_guide_url(self):
        url = "https://techdocs.akamai.com/cloud-computing/docs/create-a-widget.md"
        assert validate_docs_url(url) == url

    def test_accepts_url_without_md_suffix(self):
        url = "https://techdocs.akamai.com/cloud-computing/docs/create-a-widget"
        assert validate_docs_url(url) == url


class TestSlugs:
    @pytest.mark.parametrize(
        "slug", ["create-a-widget", "widget2", "a.b-c_d", "welcome"]
    )
    def test_valid(self, slug):
        assert is_valid_slug(slug)

    @pytest.mark.parametrize(
        "slug",
        [
            "", "../secret", "a/b", "a\\b", "..", "-leading", "a" * 300, "sp ace", "sla/sh",
            # A trailing newline: `$` would accept this, `\Z` does not.
            "widget\n", "widget\r\n",
        ],
    )  # fmt: skip
    def test_invalid(self, slug):
        assert not is_valid_slug(slug)

    def test_slug_from_url_strips_md(self):
        base = "https://techdocs.akamai.com/cloud-computing/docs/"
        assert slug_from_url(f"{base}create-a-widget.md") == "create-a-widget"
        assert slug_from_url(f"{base}create-a-widget") == "create-a-widget"

    def test_guide_url_appends_md(self):
        assert guide_url("create-a-widget").endswith("/create-a-widget.md")

    def test_guide_url_rejects_traversal(self):
        with pytest.raises(InvalidDocId):
            guide_url("../../etc/passwd")


class TestNormalizeGuideId:
    @pytest.mark.parametrize(
        "raw",
        [
            "create-a-widget",
            "create-a-widget.md",
            "  create-a-widget  ",
            f"{DOCS_BASE}create-a-widget.md",
            f"{DOCS_BASE}create-a-widget",
            # What a browser address bar and a rendered page hand a model.
            f"{DOCS_BASE}create-a-widget#pick-a-size",
            f"{DOCS_BASE}create-a-widget.md#pick-a-size",
            f"{DOCS_BASE}create-a-widget/",
            f"{DOCS_BASE}create-a-widget/#pick-a-size",
        ],
    )
    def test_accepts(self, raw):
        assert normalize_guide_id(raw) == "create-a-widget"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "https://example.com/page.md",
            "../../secret",
            "x" * 300,
            # A query string or percent-encoding is still refused, fragment or not.
            f"{DOCS_BASE}create-a-widget?x=1",
            f"{DOCS_BASE}create-a-widget?x=1#frag",
            f"{DOCS_BASE}create-a-widget%2F",
            # Only one trailing slash is forgiven.
            f"{DOCS_BASE}create-a-widget//",
            f"{DOCS_BASE}#frag",
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(InvalidDocId):
            normalize_guide_id(raw)


class _FakeResponse:
    """Enough of an http.client response for fetch_url: read, headers, context."""

    def __init__(
        self,
        body: bytes = b"# ok\n",
        *,
        charset: str | None = None,
        read_error: Exception | None = None,
    ):
        self._body = body
        self._read_error = read_error
        self.headers = Message()
        if charset:
            self.headers["Content-Type"] = f"text/markdown; charset={charset}"

    def read(self, amount: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TestFetchUrl:
    URL = "https://techdocs.akamai.com/cloud-computing/docs/x.md"

    @staticmethod
    def _open(monkeypatch, *, response=None, error=None):
        def fake_open(request, timeout=None):
            if error is not None:
                raise error
            return response

        monkeypatch.setattr(catalog._opener, "open", fake_open)

    def test_url_error_says_unreachable(self, monkeypatch):
        self._open(monkeypatch, error=urllib.error.URLError("name resolution failed"))
        with pytest.raises(FetchError, match="could not be reached"):
            fetch_url(self.URL)

    def test_timeout_says_timed_out(self, monkeypatch):
        self._open(monkeypatch, error=TimeoutError())
        with pytest.raises(FetchError, match="timed out"):
            fetch_url(self.URL, timeout=7)

    @pytest.mark.parametrize(
        "error",
        [
            http.client.IncompleteRead(b"partial"),
            ConnectionResetError(104, "Connection reset by peer"),
            http.client.RemoteDisconnected("Remote end closed connection"),
        ],
        ids=["incomplete-read", "connection-reset", "remote-disconnected"],
    )
    def test_errors_during_read_map_to_fetch_error(self, monkeypatch, error):
        """These escape the HTTPError and URLError clauses because they come from
        response.read(), not from opening the connection."""
        self._open(monkeypatch, response=_FakeResponse(read_error=error))
        with pytest.raises(FetchError, match="read failed"):
            fetch_url(self.URL)

    def test_incomplete_read_does_not_leak_partial_body(self, monkeypatch):
        error = http.client.IncompleteRead(b"secret internals")
        self._open(monkeypatch, response=_FakeResponse(read_error=error))
        with pytest.raises(FetchError) as caught:
            fetch_url(self.URL)
        assert "secret internals" not in str(caught.value)

    def test_unknown_charset_maps_to_fetch_error(self, monkeypatch):
        self._open(monkeypatch, response=_FakeResponse(charset="x-no-such-charset"))
        with pytest.raises(FetchError, match="read failed"):
            fetch_url(self.URL)

    def test_declared_charset_is_honoured(self, monkeypatch):
        body = "# café\n".encode("latin-1")
        self._open(monkeypatch, response=_FakeResponse(body, charset="latin-1"))
        assert fetch_url(self.URL) == "# café\n"

    def test_missing_charset_defaults_to_utf8(self, monkeypatch):
        self._open(monkeypatch, response=_FakeResponse("# café\n".encode()))
        assert fetch_url(self.URL) == "# café\n"


class TestCleanMarkdown:
    def test_extracts_updated_at(self, guide_markdown):
        _, updated_at = strip_frontmatter(guide_markdown)
        assert updated_at == "2026-02-01T10:00:00.000Z"

    def test_no_frontmatter_passes_through(self):
        body, updated_at = strip_frontmatter("# Title\n\nText.\n")
        assert body.startswith("# Title")
        assert updated_at == ""

    def test_strips_boilerplate_and_siblings(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        assert "Fetch the complete documentation index" not in body
        assert "Sibling pages" not in body
        assert body.startswith("# Create a widget")
        assert "Run the checker" in body

    def test_rejects_html(self):
        with pytest.raises(FetchError):
            clean_guide_markdown("<!DOCTYPE html>\n<html><body>404</body></html>")

    def test_strips_sub_pages_and_sibling_pages(self):
        body, _ = clean_guide_markdown("# Create a widget\n\nWidgets hold sprockets.\n" + NAV_TAIL)
        assert body == "# Create a widget\n\nWidgets hold sprockets."
        assert "pick-a-size" not in body

    def test_navigation_link_with_brackets_in_its_title_is_still_navigation(self):
        tail = f"\n# Sibling pages\n\n* [Widgets [beta]]({DOCS_BASE}widgets-beta.md)\n"
        body, _ = clean_guide_markdown("# Create a widget\n\nWidgets hold sprockets.\n" + tail)
        assert body == "# Create a widget\n\nWidgets hold sprockets."

    def test_navigation_line_with_many_link_openers_is_linear(self):
        """`\\[.*\\]` retried the URL part from every `](` on the line."""
        import time

        line = "- [" + "](a" * 33_333 + " x"
        text = "# Create a widget\n\nWidgets hold sprockets.\n\n# Sibling pages\n\n" + line + "\n"
        started = time.perf_counter()
        body, _ = clean_guide_markdown(text)
        assert time.perf_counter() - started < 1
        # The line is not a link, so the heading above it is content and stays.
        assert line in body

    def test_whats_next_footer_after_sibling_pages_goes_too(self):
        text = (
            "# Create a widget\n\nWidgets hold sprockets.\n" + NAV_TAIL
            + f"\n# What's next\n\n* [Pick a size]({DOCS_BASE}pick-a-size.md)\n"
        )
        body, _ = clean_guide_markdown(text)
        assert body == "# Create a widget\n\nWidgets hold sprockets."

    def test_prose_section_after_navigation_survives(self):
        """No heading qualifies for the early cut, so each link-only block goes alone.

        The old fallback cut at the last navigation heading and took the prose
        section after it too.
        """
        text = (
            "# Create a widget\n\nWidgets hold sprockets.\n" + NAV_TAIL
            + "\n# Notes\n\nA sentence that is not a link.\n"
        )
        body, _ = clean_guide_markdown(text)
        assert body == (
            "# Create a widget\n\nWidgets hold sprockets.\n\n# Notes\n\nA sentence that is not a link."
        )
        assert "pick-a-size" not in body
        assert "resize-a-widget" not in body

    def test_whats_next_prose_after_navigation_survives(self):
        text = (
            "# Create a widget\n\nWidgets hold sprockets.\n" + NAV_TAIL
            + "\n# What's next\n\nRead the sprocket guide before you resize.\n"
        )
        body, _ = clean_guide_markdown(text)
        assert "Read the sprocket guide" in body
        assert "Sibling pages" not in body
        assert "Sub pages" not in body

    def test_navigation_heading_with_prose_under_it_stays_whole(self):
        """Prose directly under a navigation heading makes the block content."""
        text = (
            "# Create a widget\n\nWidgets hold sprockets.\n\n# Sibling pages\n\n"
            f"* [Resize a widget]({DOCS_BASE}resize-a-widget.md)\n\n"
            "Every sibling shares the sprocket pool.\n\n# Real section\n\nReal content.\n"
        )
        body, _ = clean_guide_markdown(text)
        assert "Every sibling shares the sprocket pool." in body
        assert "resize-a-widget" in body
        assert body.endswith("# Real section\n\nReal content.")

    def test_sub_pages_heading_with_prose_is_content(self):
        text = (
            "# Create a widget\n\nWidgets hold sprockets.\n\n"
            "# Sub pages\n\nA widget can own sub pages. Each one is a sprocket.\n" + NAV_TAIL
        )
        body, _ = clean_guide_markdown(text)
        assert "Each one is a sprocket." in body
        assert "pick-a-size" not in body
        assert "Sibling pages" not in body

    def test_crlf_input_cleans_the_same(self):
        tail = NAV_TAIL.replace("\n", "\r\n")
        text = "# Create a widget\r\n\r\nWidgets hold sprockets.\r\n" + tail
        body, _ = clean_guide_markdown(text)
        assert body == "# Create a widget\n\nWidgets hold sprockets."
        assert "\r" not in body

    def test_crlf_frontmatter_still_yields_updated_at(self):
        text = "---\r\nupdatedAt: 2026-02-01T10:00:00.000Z\r\n---\r\n\r\n# Title\r\n\r\nText.\r\n"
        body, updated_at = clean_guide_markdown(text)
        assert updated_at == "2026-02-01T10:00:00.000Z"
        assert body == "# Title\n\nText."


class TestTemplateVariables:
    """techdocs serves `<<NAME>>` variables unexpanded on the raw markdown path."""

    def test_product_name_becomes_plain_text(self):
        text = "Access <<AKAMAI CLOUD>>'s S3-compatible Object Storage from a script."
        assert (
            resolve_template_variables(text)
            == "Access Akamai Cloud's S3-compatible Object Storage from a script."
        )

    def test_long_dash_reads_as_a_comma(self):
        text = "Serve a static website<<CJAR_DASH_LONG>>a website with only static files."
        assert (
            resolve_template_variables(text)
            == "Serve a static website, a website with only static files."
        )

    def test_unknown_names_are_left_alone(self):
        heredoc = "cat << EOF >> $log\nDate: $(date)\nEOF\n"
        assert resolve_template_variables(heredoc) == heredoc
        assert resolve_template_variables("<<NOT_A_KNOWN_VAR>> stays") == "<<NOT_A_KNOWN_VAR>> stays"

    def test_every_occurrence_is_replaced(self):
        text = "<<AKAMAI CLOUD>> and <<AKAMAI CLOUD>> again"
        assert resolve_template_variables(text) == "Akamai Cloud and Akamai Cloud again"

    def test_clean_guide_markdown_resolves_them(self):
        text = (
            "# Use the SDK\n\nThe client reaches <<AKAMAI CLOUD>>'s storage.\n\n"
            "```bash\ncat << EOF >> notes.txt\nhello\nEOF\n```\n"
        )
        body, _ = clean_guide_markdown(text)
        assert "Akamai Cloud's storage" in body
        assert "<<AKAMAI CLOUD>>" not in body
        assert "cat << EOF >> notes.txt" in body


class TestHtmlComments:
    def test_unclosed_comment_drops_the_rest_of_the_document(self):
        text = (
            "# Create a widget\n\nVisible sentence.\n\n<!-- draft, do not publish\n\n"
            "# Hidden one\n\nSecret text.\n\n# Hidden two\n\nMore secret text.\n" + NAV_TAIL
        )
        body, _ = clean_guide_markdown(text)
        assert body == "# Create a widget\n\nVisible sentence."
        titles = [entry["title"] for entry in build_toc(parse_document(body))]
        assert not any("Hidden" in title for title in titles)

    def test_closed_comment_goes_and_the_sentence_stays(self):
        text = "Encryption is on by default. <!-- Encryption is off by default. -->\n"
        body, _ = clean_guide_markdown(text)
        assert "on by default" in body
        assert "off by default" not in body
        assert "<!--" not in body

    def test_comment_spanning_lines_is_removed(self):
        text = "Before.\n\n<!--\nhidden line\nanother hidden line\n-->\n\nAfter.\n"
        body, _ = clean_guide_markdown(text)
        assert "hidden" not in body
        assert body.startswith("Before.")
        assert body.endswith("After.")

    def test_two_comments_on_one_line(self):
        assert strip_html_comments("a <!-- x --> b <!-- y --> c") == "a  b  c"

    def test_comment_inside_a_fence_is_kept(self):
        text = "Copy this snippet:\n\n```html\n<!-- keep me -->\n```\n\nAfter.\n"
        body, _ = clean_guide_markdown(text)
        assert "<!-- keep me -->" in body
        assert "After." in body

    def test_unclosed_comment_inside_a_fence_does_not_truncate(self):
        text = "Copy this snippet:\n\n~~~html\n<!-- still open\n~~~\n\nAfter.\n"
        body, _ = clean_guide_markdown(text)
        assert "<!-- still open" in body
        assert "After." in body

    def test_fence_on_a_list_marker_line_keeps_its_comment(self):
        """`- ```html` opens the fence, so the closer does not open a phantom one."""
        text = "- ```html\n  <!-- keep me -->\n  ```\n\nAfter. <!-- gone -->\n"
        stripped = strip_html_comments(text)
        assert "<!-- keep me -->" in stripped
        assert "gone" not in stripped
        assert stripped.endswith("After. \n")

    def test_inline_triple_backtick_span_does_not_open_a_fence(self):
        text = "```foo``` is a tag <!-- gone -->\n\nAfter.\n"
        assert strip_html_comments(text) == "```foo``` is a tag \n\nAfter.\n"

    def test_comment_opened_before_a_fence_swallows_it(self):
        text = "Before.\n<!--\n```\ncode\n```\n-->\nAfter.\n"
        stripped = strip_html_comments(text)
        assert "code" not in stripped
        assert "```" not in stripped
        assert stripped.startswith("Before.\n")
        assert stripped.endswith("\nAfter.\n")


class TestReadmeBlocks:
    def test_parameters_block_becomes_a_pipe_table(self):
        assert convert_readme_blocks(PARAMETERS_BLOCK) == PARAMETERS_TABLE

    def test_table_row_stays_on_one_line_with_pipes_escaped(self):
        rows = convert_readme_blocks(PARAMETERS_BLOCK).splitlines()
        assert len(rows) == 4
        assert rows[2].count(" | ") == 1
        assert "\\|" in rows[2]

    def test_missing_cells_render_empty(self):
        block = '[block:parameters]\n{"data": {"h-0": "A", "h-1": "B", "0-0": "only"}}\n[/block]'
        assert convert_readme_blocks(block).splitlines()[-1] == "| only |  |"

    def test_inline_image_block_becomes_its_alt_text(self):
        text = f"Click **Save**.\n\n{IMAGE_BLOCK_INLINE}\n\n4. Done.\n"
        body = convert_readme_blocks(text)
        assert body == "Click **Save**.\n\nThe widget dashboard\n\n4. Done.\n"
        assert "img.example.invalid" not in body

    def test_image_caption_wins_over_alt_text(self):
        block = json.dumps(
            {
                "images": [
                    {
                        "image": ["https://img.example.invalid/a.png", None, "alt text"],
                        "caption": "A widget with two sprockets.",
                        "sizing": "720px",
                    }
                ]
            }
        )
        assert convert_readme_blocks(f"[block:image]\n{block}\n[/block]") == (
            "A widget with two sprockets."
        )

    def test_image_without_caption_or_alt_renders_nothing(self):
        block = '[block:image]{"images":[{"image":["https://img.example.invalid/a.png","",""]}]}[/block]'
        body = convert_readme_blocks(f"Step one.\n\n{block}\n\nStep two.\n")
        assert body == "Step one.\n\n\n\nStep two.\n"

    def test_html_block_keeps_the_inner_html(self):
        block = '[block:html]\n{\n  "html": "<table>\\n  <tr><td>Sprocket</td></tr>\\n</table>"\n}\n[/block]'
        assert convert_readme_blocks(block) == "<table>\n  <tr><td>Sprocket</td></tr>\n</table>"

    def test_comment_inside_an_html_block_goes_like_any_other(self):
        """Blocks convert before comments strip, so the rendered html gets the same pass.

        A JSON-escaped `<!--` used to reach the index untouched, and 25,000 of
        them hung every search hit on the page.
        """
        hidden = json.dumps({"html": "<p>Shown.</p><!-- hidden secret -->"}).replace("<", "\\u003c")
        text = f"# Widgets\n\nIntro.\n\n## Step\n\n[block:html]{hidden}[/block]\n\nAfter.\n"
        body, _ = clean_guide_markdown(text)
        assert "<p>Shown.</p>" in body
        assert "hidden secret" not in body
        assert "<!--" not in body
        assert body.endswith("After.")

    def test_unclosed_comment_inside_an_html_block_runs_to_the_end(self):
        openers = json.dumps({"html": "<!--" * 25_000}).replace("<", "\\u003c")
        text = f"# Widgets\n\nIntro.\n\n## Step\n\n[block:html]{openers}[/block]\n\n## Hidden\n\nGone.\n"
        body, _ = clean_guide_markdown(text)
        assert "<!--" not in body
        assert "Hidden" not in body
        assert body.endswith("## Step")

    def test_literal_comment_inside_block_json_no_longer_eats_the_block(self):
        """Stripping first cut the JSON at `<!--`, so the block never rendered."""
        block = json.dumps({"html": "<p>Shown.</p><!-- note -->"})
        text = f"# Widgets\n\nIntro.\n\n[block:html]{block}[/block]\n\nAfter.\n"
        body, _ = clean_guide_markdown(text)
        assert "[block:" not in body
        assert "<p>Shown.</p>" in body
        assert "note" not in body
        assert body.endswith("After.")

    @pytest.mark.parametrize(
        "block",
        [
            "[block:parameters]\n{not json at all\n[/block]",
            '[block:parameters]\n{"data": {"weird-key": "x"}}\n[/block]',
            '[block:parameters]\n["a list, not an object"]\n[/block]',
            '[block:callout]\n{"type": "info", "body": "unknown kind"}\n[/block]',
            '[block:image]{"images": []}',
        ],
        ids=["bad-json", "bad-cell-key", "not-an-object", "unknown-kind", "never-closed"],
    )
    def test_block_that_does_not_parse_is_untouched(self, block):
        text = f"Before.\n\n{block}\n\nAfter.\n"
        assert convert_readme_blocks(text) == text

    def test_blocks_after_an_untouched_one_still_convert(self):
        text = f"[block:parameters]\n{{oops\n[/block]\n\n{IMAGE_BLOCK_INLINE}\n"
        body = convert_readme_blocks(text)
        assert body.startswith("[block:parameters]\n{oops\n[/block]")
        assert body.endswith("The widget dashboard\n")

    def test_table_only_section_gets_no_block_summary(self):
        text = f"# Widget plans\n\nEvery plan is listed below.\n\n# Plans\n\n{PARAMETERS_BLOCK}\n\n# Next steps\n\nPick one.\n"
        body, _ = clean_guide_markdown(text + NAV_TAIL)
        assert "[block:" not in body
        toc = build_toc(parse_document(body))
        plans = next(entry for entry in toc if entry["title"] == "Plans")
        assert "summary" not in plans
        assert "[block:" not in json.dumps(toc)


class TestCacheDir:
    def test_env_override(self, cache_env):
        assert cache_dir() == cache_env
        assert index_path() == cache_env / "index.json"

    def test_xdg_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AKAMAI_DOCS_MCP_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert cache_dir() == tmp_path / "akamai-cloud-docs-mcp"
