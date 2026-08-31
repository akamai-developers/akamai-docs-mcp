"""Table of contents parsing and section extraction."""

from __future__ import annotations

from akamai_cloud_docs_mcp.core.catalog import clean_guide_markdown
from akamai_cloud_docs_mcp.core.sections import (
    INLINE_CONTENT_BYTES,
    build_toc,
    flatten_inline,
    flatten_markdown,
    get_section,
    parse_document,
    section_text,
    snippet,
    summarize,
)


class TestParseDocument:
    def test_top_level_sections_and_children(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        doc = parse_document(body, fallback_title="Create a widget")

        assert [(s.id, s.title) for s in doc.sections] == [
            ("1", "Create a widget"),
            ("2", "Verify the widget"),
        ]
        assert [(c.id, c.title) for c in doc.sections[0].children] == [
            ("1.1", "Pick a size"),
            ("1.2", "Name the widget"),
        ]

    def test_dotted_ids_nest_three_deep(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        doc = parse_document(body)
        assert "2.1.1" in doc.flat
        assert doc.flat["2.1.1"].title == "Failure codes"

    def test_fallback_title_used_when_no_distinct_title_heading(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        doc = parse_document(body, fallback_title="Create a widget")
        assert doc.title == "Create a widget"

    def test_single_shallow_heading_becomes_the_title(self, small_guide):
        doc = parse_document(small_guide)
        assert doc.title == "Delete a widget"
        assert doc.sections == []
        assert "cannot be undone" in doc.preamble

    def test_headings_inside_code_fences_are_ignored(self, fenced_markdown):
        doc = parse_document(fenced_markdown)
        titles = [s.title for s in doc.flat.values()]
        assert "Apply the change" in titles
        assert "Confirm the change" in titles
        assert not any("comment" in title for title in titles)
        assert not any("also not a heading" in title for title in titles)

    def test_bare_title_at_section_level_is_not_an_empty_section(self):
        """A lone title line above same-level sections is a title, not section 1.

        Pages that use one heading level for the title and every section used to
        yield an empty section 1, so asking for it returned a heading and no
        content.
        """
        body = "# Billing FAQ\n\n# How do I pay?\n\nBy card.\n\n# When am I billed?\n\nMonthly.\n"
        doc = parse_document(body, fallback_title="Billing FAQ")

        assert doc.title == "Billing FAQ"
        assert [(s.id, s.title) for s in doc.sections] == [
            ("1", "How do I pay?"),
            ("2", "When am I billed?"),
        ]
        assert "By card." in section_text(doc, doc.flat["1"])

    def test_first_heading_with_content_stays_a_section(self):
        """Only a *bare* heading is promoted. One with prose under it is a section."""
        body = "# Manage widgets\n\nIntro prose.\n\n# Resize a widget\n\nSteps.\n"
        doc = parse_document(body, fallback_title="Manage widgets")

        assert [s.title for s in doc.sections] == ["Manage widgets", "Resize a widget"]
        assert "Intro prose." in section_text(doc, doc.flat["1"])

    def test_first_heading_with_a_child_stays_a_section(self):
        """A heading whose content lives in a subsection is not a bare title."""
        body = "# Overview\n\n## Details\n\nText.\n\n# Next\n\nMore.\n"
        doc = parse_document(body, fallback_title="Guide")

        assert doc.title == "Guide"
        assert [s.title for s in doc.sections] == ["Overview", "Next"]
        assert doc.flat["1.1"].title == "Details"

    def test_empty_document(self):
        doc = parse_document("")
        assert doc.sections == []
        assert doc.flat == {}

    def test_ordinal_heading_keeps_its_number(self):
        """A numbered step heading is a title, not a list item."""
        body = "# Page\n\n## 1. Create a thing\n\nDo it.\n\n## 2. Check the thing\n\nLook.\n"
        doc = parse_document(body)
        assert [s.title for s in doc.sections] == ["1. Create a thing", "2. Check the thing"]

    def test_trailing_hash_inside_a_title_survives(self):
        doc = parse_document("# Page\n\n## Using C#\n\nBody.\n\n## F# too\n\nBody.\n")
        assert [s.title for s in doc.sections] == ["Using C#", "F# too"]

    def test_closing_hash_sequence_is_still_dropped(self):
        doc = parse_document(
            "# Page\n\n## Title ##\n\nBody.\n\n## Other #\n\nBody.\n\n## ###\n\nBody.\n"
        )
        assert [s.title for s in doc.sections] == ["Title", "Other"]

    def test_only_the_last_hash_sequence_closes(self):
        doc = parse_document("# Page\n\n## a ## #\n\nBody.\n\n## b\t##\n\nBody.\n")
        assert [s.title for s in doc.sections] == ["a ##", "b"]

    def test_heading_with_a_long_run_of_spaces_keeps_its_title(self):
        """The heading pattern used to re-scan a whitespace run from every position."""
        doc = parse_document("# Page\n\n## Step" + " " * 50_000 + "b\n\nBody.\n")
        assert [s.title for s in doc.sections] == ["Step b"]

    def test_fence_opened_at_three_spaces_and_closed_at_four(self):
        """A list item fence can open and close at different indents.

        The closing line used to fall outside the fence regex, so the fence
        never closed and every later heading vanished.
        """
        body = (
            "# Page\n\n## First\n\n1. Run it:\n\n   ```bash\n   widgetctl apply\n    ```\n\n"
            "## Second\n\nAfter the fence.\n\n## Third\n\nEnd.\n"
        )
        doc = parse_document(body)
        assert [s.title for s in doc.sections] == ["First", "Second", "Third"]
        assert "widgetctl apply" in section_text(doc, doc.flat["1"])

    def test_fence_that_opens_on_its_list_marker_line(self):
        """`- ```python` opens a fence. Missing it made the closer open one.

        The comment line then became a heading, the phantom fence swallowed the
        rest of the file, and heading B vanished.
        """
        body = "# T\n\n## A\n\n- ```python\n  # not heading\n  ```\n\n## B\nprose\n"
        doc = parse_document(body)
        assert [s.title for s in doc.sections] == ["A", "B"]
        assert "not heading" in section_text(doc, doc.flat["1"])
        assert section_text(doc, doc.flat["2"]) == "## B\nprose"

    def test_fence_on_an_ordered_list_marker_line(self):
        body = "# T\n\n## A\n\n1. ~~~sh\n   # not heading\n   ~~~\n\n## B\n\nprose\n"
        doc = parse_document(body)
        assert [s.title for s in doc.sections] == ["A", "B"]

    def test_inline_triple_backtick_span_is_not_a_fence(self):
        """A backtick fence line carries no second backtick, so this is prose."""
        body = "# T\n\n## A\n\n```foo``` is a language tag\n\n## B\nprose\n"
        doc = parse_document(body)
        assert [s.title for s in doc.sections] == ["A", "B"]
        assert summarize(section_text(doc, doc.flat["1"], include_heading=False)) == (
            "foo is a language tag"
        )

    def test_tilde_fence_keeps_the_plain_rule(self):
        """Only the backtick branch checks for a later backtick on the line."""
        body = "# T\n\n## A\n\n~~~ a `span` here\n# code\n~~~\n\n## B\n\nprose\n"
        doc = parse_document(body)
        assert [s.title for s in doc.flat.values()] == ["A", "B"]

    def test_deeply_indented_fence_hides_its_comment_lines(self):
        body = "# Page\n\n## Only\n\n- step:\n\n     ```sh\n# not a heading\n     ```\n\nDone.\n"
        doc = parse_document(body)
        assert [s.title for s in doc.flat.values()] == ["Only"]

    def test_fences_pair_by_marker_character(self):
        """A tilde fence inside a backtick fence is code, not a closing fence."""
        body = "# Page\n\n## Only\n\n```md\n~~~\n# still code\n~~~\n```\n\n## Second\n\nText.\n"
        doc = parse_document(body)
        assert [s.title for s in doc.sections] == ["Only", "Second"]


class TestSectionText:
    def test_section_includes_its_children(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        doc = parse_document(body)
        text = section_text(doc, doc.flat["2"])
        assert text.startswith("# Verify the widget")
        assert "Read the output" in text
        assert "Failure codes" in text
        assert "Pick a size" not in text

    def test_section_stops_before_the_next_sibling(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        doc = parse_document(body)
        text = section_text(doc, doc.flat["1.1"])
        assert "Choose small, medium, or large" in text
        assert "Names must be lowercase" not in text

    def test_can_drop_the_heading_line(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        doc = parse_document(body)
        text = section_text(doc, doc.flat["1.1"], include_heading=False)
        assert not text.startswith("#")

    def test_unknown_section_returns_none(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        doc = parse_document(body)
        assert get_section(doc, "99") is None
        assert get_section(doc, "") is None


class TestToc:
    def test_shape(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        toc = build_toc(parse_document(body))
        assert toc[0]["id"] == "1"
        assert toc[0]["title"] == "Create a widget"
        assert "sprockets" in toc[0]["summary"].lower()
        assert [child["id"] for child in toc[0]["children"]] == ["1.1", "1.2"]

    def test_children_carry_no_summary(self, guide_markdown):
        body, _ = clean_guide_markdown(guide_markdown)
        toc = build_toc(parse_document(body))
        assert "summary" not in toc[0]["children"][0]

    def test_small_childless_section_carries_its_content(self):
        """A section shorter than the framing around it ships in the table of contents."""
        body = "Run `widgetctl prune` once a week. Pruned sprockets cannot be recovered."
        doc = parse_document(
            f"# Page\n\n## Prune sprockets\n\n{body}\n\n## Big\n\n{'word ' * 200}\n"
        )
        toc = build_toc(doc)

        assert toc[0]["content"] == body
        assert "summary" not in toc[0]
        assert "content" not in toc[1]
        assert toc[1]["summary"].startswith("word word")

    def test_section_with_children_keeps_a_summary(self):
        doc = parse_document("# Page\n\n## Parent\n\nShort intro.\n\n### Child\n\nTiny.\n")
        toc = build_toc(doc)

        assert toc[0]["summary"] == "Short intro."
        assert "content" not in toc[0]
        assert "content" not in toc[0]["children"][0]

    def test_content_threshold_is_measured_in_bytes(self):
        at_limit = "x" * INLINE_CONTENT_BYTES
        under = "x" * (INLINE_CONTENT_BYTES - 1)
        doc = parse_document(f"# Page\n\n## A\n\n{at_limit}\n\n## B\n\n{under}\n")
        toc = build_toc(doc)

        assert "content" not in toc[0]
        assert toc[1]["content"] == under

    def test_whitespace_only_body_carries_no_content(self):
        """Blank lines with spaces are not a body; the entry gets no `content`."""
        doc = parse_document("# T\n\n## A\n   \n\t\n## B\n\ntext\n")
        toc = build_toc(doc)
        assert toc[0] == {"id": "1", "title": "A"}
        assert toc[1]["content"] == "text"

    def test_small_table_only_section_carries_content(self):
        """A table has no first sentence, so it used to get no summary at all."""
        table = "| Size | Cost |\n| --- | --- |\n| small | 1 |\n| large | 3 |"
        doc = parse_document(f"# Page\n\n## Prices\n\n{table}\n")
        assert build_toc(doc)[0]["content"] == table


class TestTextHelpers:
    def test_flatten_markdown(self):
        raw = "See the [docs](https://example.com) for **bold** and `code` values."
        assert flatten_markdown(raw) == "See the docs for bold and code values."

    def test_flatten_strips_list_markers(self):
        assert flatten_markdown("- item one") == "item one"
        assert flatten_markdown("1. item one") == "item one"

    def test_flatten_keeps_underscores_inside_a_word(self):
        """A config key is the exact string a caller is about to type."""
        assert flatten_markdown("`thread_pool_size`") == "thread_pool_size"
        assert flatten_markdown("Configure the `chunk_store_config` block") == (
            "Configure the chunk_store_config block"
        )
        assert flatten_markdown("`entity_ids` and `entity_regions`") == (
            "entity_ids and entity_regions"
        )
        assert flatten_markdown("foo_bar_baz stays whole") == "foo_bar_baz stays whole"

    def test_flatten_still_strips_real_emphasis(self):
        assert flatten_markdown("the *entire* month") == "the entire month"
        assert flatten_markdown("value _must_ be set.") == "value must be set."
        assert flatten_markdown("**strong** and _em_") == "strong and em"
        assert flatten_markdown("__bold__") == "bold"
        # Emphasis may wrap a word that itself carries an underscore.
        assert flatten_markdown("_foo_bar_") == "foo_bar"

    def test_flatten_treats_a_code_span_as_literal(self):
        """Code spans are literal Markdown, so no later pass may rewrite them."""
        assert flatten_markdown("in the format of `<major>.<minor>`.") == (
            "in the format of <major>.<minor>."
        )
        assert flatten_markdown("run `a *b* c`") == "run a *b* c"
        assert flatten_markdown("a real <span> tag goes") == "a real tag goes"

    def test_flatten_ignores_stray_nulls(self):
        assert flatten_markdown("a\x001\x00b `x_y`") == "a1b x_y"

    def test_flatten_keeps_angle_bracket_placeholders(self):
        """`<placeholder>` is a value the reader fills in, not a tag."""
        assert flatten_markdown("set <your_region> in <path/to/config.yaml>") == (
            "set <your_region> in <path/to/config.yaml>"
        )
        assert flatten_markdown("when a < b and c > d, stop") == "when a < b and c > d, stop"
        assert flatten_markdown("<LOOPBACK,UP> flags") == "<LOOPBACK,UP> flags"

    def test_flatten_strips_known_html_tags(self):
        assert flatten_markdown("one<br>two <b>three</b>") == "onetwo three"
        assert flatten_markdown('<span class="x">y</span> <img src="a.png" />') == "y"
        assert (
            flatten_markdown('<AkamaiTabs><AkamaiTab title="Cloud">z</AkamaiTab></AkamaiTabs>')
            == "z"
        )
        assert flatten_markdown("<table><tr><td>cell</td></tr></table>") == "cell"
        assert flatten_markdown("keep <!-- not this --> this") == "keep this"

    def test_flatten_unwraps_autolinks(self):
        assert flatten_markdown("mail <support@example.com> now") == "mail support@example.com now"
        assert (
            flatten_markdown("see <https://example.com/a?b=1>.") == "see https://example.com/a?b=1."
        )

    def test_flatten_inline_keeps_line_prefixes(self):
        """Titles get the inline pass only, so an ordinal or quote marker stays."""
        assert flatten_inline("1. Create a *thing*") == "1. Create a thing"
        assert flatten_inline("- [x](u)") == "- x"
        assert flatten_markdown("1. Create a *thing*") == "Create a thing"

    def test_backslash_escapes_are_removed(self):
        """`mod\\_status` in a heading reached the table of contents with its backslash."""
        assert flatten_inline("Autoconfigure mod\\_status popup") == "Autoconfigure mod_status popup"
        assert flatten_inline("Baz\\#") == "Baz#"
        assert flatten_inline("a\\_b\\_c and 50\\%") == "a_b_c and 50%"
        # Only ASCII punctuation can be escaped; a backslash before a letter stays.
        assert flatten_inline("C:\\path\\to") == "C:\\path\\to"
        doc = parse_document("# Page\n\n## Autoconfigure mod\\_status popup\n\nBody.\n")
        assert build_toc(doc)[0]["title"] == "Autoconfigure mod_status popup"

    def test_escaped_markup_is_literal_not_markup(self):
        assert flatten_inline("\\*not emphasis\\*") == "*not emphasis*"
        assert flatten_inline("\\_not emphasis\\_") == "_not emphasis_"
        assert flatten_inline("\\<b\\>tag\\</b\\>") == "<b>tag</b>"

    def test_escape_inside_a_code_span_stays_literal(self):
        assert flatten_inline("run `a\\_b`") == "run a\\_b"
        assert flatten_markdown("set `path\\\\to` then \\_go\\_") == "set path\\\\to then _go_"

    def test_nested_same_marker_emphasis_loses_both_pairs(self):
        """Innermost first. One pass stripped one pair and left the other's stars."""
        assert flatten_markdown("**Hosting the domain *example.com*.**") == (
            "Hosting the domain example.com."
        )
        assert flatten_markdown("*Only click the *Recycle All Nodes* button.*") == (
            "Only click the Recycle All Nodes button."
        )
        assert flatten_markdown("__a _b_ c__ and *a **b** c*") == "a b c and a b c"

    def test_a_lone_marker_sits_inside_emphasis(self):
        """`5 * 3` can neither open nor close, so the span crosses it."""
        assert flatten_markdown("*5 * 3 is 15* and _a _ b_") == "5 * 3 is 15 and a _ b"

    def test_a_marker_after_a_space_does_not_close(self):
        """`**Attention: **` is not bold. The old pattern paired it with a later marker."""
        assert flatten_markdown("**Attention: ** Backups are **not** encrypted") == (
            "**Attention: ** Backups are not encrypted"
        )
        assert flatten_markdown("Maximum *sustained* CPU | Maximum *burst* CPU") == (
            "Maximum sustained CPU | Maximum burst CPU"
        )

    def test_unclosed_emphasis_is_linear(self):
        """Every opener that never closes used to scan to the end of the line."""
        import time

        for line in (" *a" * 33_334, " _a" * 33_334, "<!--" * 25_000):
            started = time.perf_counter()
            assert flatten_inline(line) == line.strip()
            assert time.perf_counter() - started < 1

    def test_comment_closes_at_the_first_arrow(self):
        assert flatten_inline("a <!-- b <!-- c --> d") == "a d"
        assert flatten_inline("a <!-- b") == "a <!-- b"
        assert flatten_inline("<!-- x --><b>y</b><!-- z -->") == "y"

    def test_section_title_keeps_an_identifier(self):
        doc = parse_document("# Page\n\n## Set `thread_pool_size`\n\nBody text here.\n")
        assert doc.flat["1"].title == "Set thread_pool_size"
        assert build_toc(doc)[0]["title"] == "Set thread_pool_size"

    def test_summarize_takes_the_first_sentence(self):
        text = "A widget holds sprockets in place. A second sentence follows here."
        assert summarize(text) == "A widget holds sprockets in place."

    def test_summarize_skips_headings_and_fences(self, fenced_markdown):
        doc = parse_document(fenced_markdown)
        summary = summarize(section_text(doc, doc.flat["1"], include_heading=False))
        assert "Restart the widget" in summary
        assert "widgetctl" not in summary

    def test_summaries_and_snippets_skip_a_fence_closed_at_a_deeper_indent(self):
        text = "   ```sh\n   widgetctl apply\n    ```\n\nThe widget restarts after the run.\n"
        assert summarize(text) == "The widget restarts after the run."
        assert snippet(text, ["absent"]) == "The widget restarts after the run."

    def test_summarize_truncates(self):
        long_text = "word " * 200
        assert len(summarize(long_text)) <= 164

    def test_snippet_centers_on_a_match(self):
        text = "intro line. " * 30 + "the sprocket tolerance is four millimetres. " + "tail. " * 30
        result = snippet(text, ["sprocket"])
        assert "sprocket tolerance" in result
        assert len(result) <= 250

    def test_snippet_from_the_start_when_no_match(self):
        result = snippet("A widget holds sprockets.", ["absent"])
        assert result.startswith("A widget")

    def test_snippet_on_empty_text(self):
        assert snippet("", ["anything"]) == ""
