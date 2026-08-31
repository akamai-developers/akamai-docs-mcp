"""The search_docs and fetch_doc contract, exercised without a network."""

from __future__ import annotations

import http.client
import json
import os
import threading

import pytest

from akamai_cloud_docs_mcp.config import INDEX_SCHEMA
from akamai_cloud_docs_mcp.core import tools as core_tools
from akamai_cloud_docs_mcp.core.catalog import FetchError
from akamai_cloud_docs_mcp.core.tools import (
    MAX_SECTION_CHARS,
    MAX_VALID_SECTIONS,
    DocsService,
    render_result,
    staleness_line,
)
from akamai_cloud_docs_mcp.sync import SyncError, write_index

REFERENCE_BASE = "https://techdocs.akamai.com/linode-api/reference/"


def _live(monkeypatch, body: str):
    """Stand in for the live guide fetch with an invented body."""

    def fake_fetch(slug, **kwargs):
        return body, "", f"https://techdocs.akamai.com/cloud-computing/docs/{slug}.md"

    monkeypatch.setattr(core_tools, "fetch_guide", fake_fetch)


class TestSearchDocs:
    def test_finds_a_guide(self, service):
        results = service.search_docs("resize a widget")
        assert results[0]["id"] == "resize-a-widget"

    def test_result_keys_match_the_contract(self, service):
        # kind and score restate the order and the id shape, so they stay out.
        assert set(service.search_docs("sprocket", k=1)[0]) == {"id", "title", "snippet"}

    def test_k_is_clamped_high(self, service):
        assert len(service.search_docs("widget", k=999)) <= 20

    def test_k_is_clamped_low(self, service):
        assert len(service.search_docs("widget", k=0)) == 1

    def test_k_rejects_garbage_without_raising(self, service):
        assert service.search_docs("widget", k="lots") != []

    def test_k_infinity_falls_back_to_the_default(self, service):
        # int(float("inf")) raises OverflowError, not ValueError.
        assert service.search_docs("widget", k=float("inf")) != []

    @pytest.mark.parametrize("query", ["", "   ", "zzzz nonexistent kubernetes helm"])
    def test_empty_result_is_a_list_not_an_error(self, service, query):
        assert service.search_docs(query) == []

    def test_long_query_is_truncated_not_rejected(self, service):
        assert isinstance(service.search_docs("widget " * 500), list)


class TestFetchDocToc:
    def test_returns_a_table_of_contents(self, service, big_guide_live):
        result = service.fetch_doc("create-a-widget")
        assert result["id"] == "create-a-widget"
        assert [section["id"] for section in result["sections"]] == ["1", "2"]
        assert "error" not in result

    def test_toc_carries_no_repeated_fields(self, service, big_guide_live):
        # kind is implied by the id, the url comes with the section, and the
        # hint repeated the tool descriptions.
        result = service.fetch_doc("create-a-widget")
        assert set(result) == {"id", "title", "preamble", "sections"}

    def test_small_guide_returns_content_directly(self, service, small_guide_live):
        result = service.fetch_doc("resize-a-widget")
        assert result["document_small"] is True
        assert result["url"].endswith("/resize-a-widget")
        assert "sprocket" in result["content"].lower() or "widget" in result["content"].lower()
        assert "sections" not in result

    def test_accepts_a_full_url(self, service, big_guide_live):
        result = service.fetch_doc(
            "https://techdocs.akamai.com/cloud-computing/docs/create-a-widget.md"
        )
        assert result["id"] == "create-a-widget"

    def test_mixed_case_slug_resolves(self, service, big_guide_live):
        assert service.fetch_doc("Create-A-Widget")["id"] == "create-a-widget"


class TestFetchDocSection:
    def test_returns_one_section(self, service, big_guide_live):
        result = service.fetch_doc("create-a-widget", section="2")
        assert result["section_id"] == "2"
        assert result["section_title"] == "Verify the widget"
        assert "Run the checker" in result["content"]
        assert "Pick a size" not in result["content"]

    def test_section_keeps_the_page_url(self, service, big_guide_live):
        # A model that went search then section never saw a table of contents.
        result = service.fetch_doc("create-a-widget", section="2")
        assert result["url"].endswith("/create-a-widget")
        assert result["kind"] == "guide"

    def test_nested_section(self, service, big_guide_live):
        result = service.fetch_doc("create-a-widget", section="2.1.1")
        assert result["section_title"] == "Failure codes"
        assert "Code 7" in result["content"]


class TestTeachingErrors:
    def test_unknown_id_suggests_close_matches(self, service):
        result = service.fetch_doc("resize-widget")
        assert "error" in result
        assert result["did_you_mean"][0]["id"] == "resize-a-widget"
        assert "hint" in result

    def test_suggestions_stay_at_three(self, service):
        assert len(service.fetch_doc("widget")["did_you_mean"]) <= 3

    def test_empty_id(self, service):
        result = service.fetch_doc("")
        assert "error" in result
        assert "example" in result

    def test_oversized_id(self, service):
        assert "error" in service.fetch_doc("x" * 300)

    def test_oversized_section(self, service):
        assert "error" in service.fetch_doc("create-a-widget", section="1" * 300)

    def test_bad_section_lists_top_level_sections_only(self, service, big_guide_live):
        result = service.fetch_doc("create-a-widget", section="99")
        assert "error" in result
        ids = [section["id"] for section in result["valid_sections"]]
        assert ids == ["1", "2"]
        assert all(section["title"] for section in result["valid_sections"])
        assert "valid_sections" in result["hint"]

    def test_traversal_id_is_rejected(self, service):
        assert "error" in service.fetch_doc("../../../etc/passwd")

    def test_off_host_url_is_rejected(self, service):
        result = service.fetch_doc("https://example.com/cloud-computing/docs/x.md")
        assert "error" in result
        assert "techdocs.akamai.com" in result["error"]

    def test_rejected_url_does_not_get_fuzzy_suggestions(self, service):
        # Fuzzy-matching a URL against titles suggests unrelated pages.
        result = service.fetch_doc("https://evil.example.com/cloud-computing/docs/x.md")
        assert "did_you_mean" not in result
        assert "slug" in result["hint"]

    def test_valid_but_unindexed_url_still_reports_no_match(self, service):
        result = service.fetch_doc(
            "https://techdocs.akamai.com/cloud-computing/docs/not-in-the-index.md"
        )
        assert "error" in result
        assert "did_you_mean" in result

    def test_unknown_reference_url_gets_its_own_message(self, service):
        result = service.fetch_doc(f"{REFERENCE_BASE}does-not-exist")
        assert "reference URL" in result["error"]
        assert "/cloud-computing/docs/" not in result["error"]
        assert "did_you_mean" in result

    def test_no_result_carries_a_traceback(self, service):
        result = service.fetch_doc("../../../etc/passwd")
        assert "Traceback" not in str(result)


class TestLiveFetchFallback:
    def test_falls_back_to_the_indexed_copy(self, service, monkeypatch):
        def boom(slug, **kwargs):
            raise FetchError("simulated outage")

        monkeypatch.setattr("akamai_cloud_docs_mcp.core.tools.fetch_guide", boom)
        result = service.fetch_doc("sprocket-basics")
        assert "error" not in result
        assert "indexed copy" in result["note"]
        assert "simulated outage" in result["note"]

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionResetError(104, "Connection reset by peer"),
            http.client.IncompleteRead(b"partial"),
            http.client.RemoteDisconnected("Remote end closed connection"),
            LookupError("unknown encoding: x-foo"),
        ],
        ids=["reset", "incomplete", "disconnected", "charset"],
    )
    def test_every_read_error_falls_back(self, service, monkeypatch, exc):
        # The contract is "the indexed copy is better than no answer", and it
        # has to hold for errors that are not CatalogError.
        def boom(slug, **kwargs):
            raise exc

        monkeypatch.setattr("akamai_cloud_docs_mcp.core.tools.fetch_guide", boom)
        result = service.fetch_doc("sprocket-basics")
        assert "error" not in result
        assert "indexed copy" in result["note"]

    def test_the_reason_is_one_short_line(self, service, monkeypatch):
        def boom(slug, **kwargs):
            raise FetchError("first line " + "x" * 500 + "\nsecond line")

        monkeypatch.setattr("akamai_cloud_docs_mcp.core.tools.fetch_guide", boom)
        note = service.fetch_doc("sprocket-basics")["note"]
        assert "\n" not in note
        assert "second line" not in note
        assert len(note) < 200

    def test_errors_when_there_is_no_stored_text(self, monkeypatch, index_payload):
        for doc in index_payload["docs"]:
            doc["text"] = ""
        service = DocsService(index_payload)

        def boom(slug, **kwargs):
            raise FetchError("simulated outage")

        monkeypatch.setattr("akamai_cloud_docs_mcp.core.tools.fetch_guide", boom)
        result = service.fetch_doc("sprocket-basics")
        assert "error" in result


class TestApiDocuments:
    def test_fetch_by_method_and_path(self, service):
        result = service.fetch_doc("POST /widgets")
        assert result["kind"] == "api"
        assert result["cli"] == "linode-cli widgets create"

    def test_method_case_is_normalized(self, service):
        assert service.fetch_doc("post /widgets")["id"] == "POST /widgets"

    def test_fetch_by_cli_command(self, service):
        assert service.fetch_doc("linode-cli widgets create")["id"] == "POST /widgets"

    def test_fetch_by_operation_id(self, service):
        assert service.fetch_doc("post-widgets")["id"] == "POST /widgets"

    def test_the_reference_url_a_card_emits_resolves_back(self, service):
        card = service.fetch_doc("POST /widgets")
        assert card["url"].startswith(REFERENCE_BASE)
        assert service.fetch_doc(card["url"])["id"] == card["id"]

    @pytest.mark.parametrize("suffix", ["/", "#request-body", "?tab=cli"])
    def test_reference_url_variants_resolve(self, service, suffix):
        url = service.fetch_doc("POST /widgets")["url"]
        assert service.fetch_doc(url + suffix)["id"] == "POST /widgets"

    def test_reference_url_case_is_forgiven(self, service):
        url = service.fetch_doc("POST /widgets")["url"]
        assert service.fetch_doc(url.upper())["id"] == "POST /widgets"


class TestStaleness:
    def test_fresh_index_has_no_note(self, service, big_guide_live):
        assert "note" not in service.fetch_doc("create-a-widget")
        assert "note" not in service.fetch_doc("POST /widgets")

    def test_old_index_marks_an_api_card(self, index_payload):
        index_payload["built_at"] = "2020-01-01T00:00:00Z"
        note = DocsService(index_payload).fetch_doc("POST /widgets")["note"]
        assert "days ago" in note
        # Operator advice goes to stderr, not to the model.
        assert "sync" not in note

    def test_old_index_leaves_guide_results_alone(self, index_payload, big_guide_live):
        # Guide text is fetched live, so a staleness note there says nothing.
        index_payload["built_at"] = "2020-01-01T00:00:00Z"
        service = DocsService(index_payload)
        assert "note" not in service.fetch_doc("create-a-widget")
        assert "note" not in service.fetch_doc("create-a-widget", section="1")

    def test_old_index_leaves_a_small_guide_alone(self, index_payload, small_guide_live):
        index_payload["built_at"] = "2020-01-01T00:00:00Z"
        assert "note" not in DocsService(index_payload).fetch_doc("resize-a-widget")

    def test_fallback_note_is_the_only_note_on_an_old_index(self, index_payload, monkeypatch):
        index_payload["built_at"] = "2020-01-01T00:00:00Z"

        def boom(slug, **kwargs):
            raise FetchError("simulated outage")

        monkeypatch.setattr(core_tools, "fetch_guide", boom)
        note = DocsService(index_payload).fetch_doc("sprocket-basics")["note"]
        assert note.startswith("Live fetch failed")
        assert "days ago" not in note

    def test_naive_built_at_is_treated_as_utc(self, index_payload):
        index_payload["built_at"] = "2020-01-01T00:00:00"
        service = DocsService(index_payload)
        assert service.index_age_days() > 365
        assert "days ago" in service.fetch_doc("POST /widgets")["note"]

    def test_unparsable_built_at_is_not_fatal(self, index_payload):
        index_payload["built_at"] = "whenever"
        service = DocsService(index_payload)
        assert service.staleness_note() == ""
        assert staleness_line(service) == ""

    def test_missing_built_at_is_not_fatal(self, index_payload):
        del index_payload["built_at"]
        assert DocsService(index_payload).index_age_days() is None

    def test_staleness_line_is_for_operators(self, index_payload, service):
        assert staleness_line(service) == ""
        index_payload["built_at"] = "2020-01-01T00:00:00Z"
        line = staleness_line(DocsService(index_payload))
        assert "days ago" in line
        assert "akamai-cloud-docs-mcp sync" in line


class TestRenderResult:
    def test_section_renders_as_markdown_under_a_header(self, service, big_guide_live):
        text = render_result(service.fetch_doc("create-a-widget", section="2"))
        header, blank, *body = text.split("\n", 2)
        assert header == (
            "# Create a widget > Verify the widget [create-a-widget #2] "
            "https://techdocs.akamai.com/cloud-computing/docs/create-a-widget"
        )
        assert blank == ""
        assert "Run the checker" in body[0]
        assert "\\n" not in text
        assert text.startswith("#")

    def test_small_guide_renders_under_a_header(self, service, small_guide_live):
        text = render_result(service.fetch_doc("resize-a-widget"))
        first = text.split("\n", 1)[0]
        assert first.startswith("# ")
        assert "[resize-a-widget]" in first
        assert first.endswith("/resize-a-widget")
        assert "Deleting a widget" in text

    def test_api_card_renders_as_the_card_alone(self, service):
        payload = service.fetch_doc("POST /widgets")
        text = render_result(payload)
        assert text == payload["content"]
        assert text.startswith("## POST /widgets")

    def test_old_index_note_follows_the_card(self, index_payload):
        index_payload["built_at"] = "2020-01-01T00:00:00Z"
        text = render_result(DocsService(index_payload).fetch_doc("POST /widgets"))
        assert text.startswith("## POST /widgets")
        assert text.rstrip().endswith("days ago.")

    def test_note_and_truncation_get_their_own_lines(self):
        payload = {
            "id": "x",
            "kind": "guide",
            "title": "T",
            "url": "https://example.invalid/x",
            "section_id": "1",
            "section_title": "S",
            "content": "body text",
            "note": "Live fetch failed (timed out); this is the indexed copy.",
            "truncated": True,
        }
        lines = render_result(payload).split("\n")
        assert lines[0] == "# T > S [x #1] https://example.invalid/x"
        assert lines[1] == payload["note"]
        assert lines[2].startswith("Truncated at 40,000 characters")
        assert lines[3] == ""
        assert lines[4] == "body text"

    def test_search_results_stay_compact_json(self, service):
        text = render_result(service.search_docs("widget"))
        assert text.startswith("[{")
        assert ": " not in text.split("snippet")[0]
        assert isinstance(json.loads(text), list)

    def test_toc_stays_json(self, service, big_guide_live):
        payload = json.loads(render_result(service.fetch_doc("create-a-widget")))
        assert payload["sections"][0]["id"] == "1"

    def test_errors_stay_json(self, service):
        payload = json.loads(render_result(service.fetch_doc("resize-widget")))
        assert "error" in payload

    def test_empty_list_renders(self):
        assert render_result([]) == "[]"


class TestUncoveredBranches:
    def test_long_section_is_truncated_and_flagged(self, service, monkeypatch):
        body = "# Long part\n\n" + "word " * 12_000 + "\n\n# Short part\n\nThe end.\n"
        _live(monkeypatch, body)
        result = service.fetch_doc("create-a-widget", section="1")
        assert result["truncated"] is True
        assert len(result["content"]) == MAX_SECTION_CHARS

    def test_short_section_is_not_flagged(self, service, big_guide_live):
        assert "truncated" not in service.fetch_doc("create-a-widget", section="2")

    def test_big_guide_without_headings_returns_whole_content(self, service, monkeypatch):
        body = "Just prose, no headings at all. " * 400
        assert len(body) > 8 * 1024
        _live(monkeypatch, body)
        result = service.fetch_doc("create-a-widget")
        assert result["document_small"] is True
        assert "sections" not in result
        assert result["content"].startswith("Just prose")
        assert "truncated" not in result

    def test_big_guide_without_headings_is_truncated_and_flagged(self, service, monkeypatch):
        body = "Just prose, no headings at all. " * 2_000
        assert len(body) > MAX_SECTION_CHARS
        _live(monkeypatch, body)
        result = service.fetch_doc("create-a-widget")
        assert result["document_small"] is True
        assert result["truncated"] is True
        assert len(result["content"]) == MAX_SECTION_CHARS
        # The same line a long section gets, so the model knows to follow the URL.
        second_line = render_result(result).splitlines()[1]
        assert second_line.startswith("Truncated at 40,000 characters")

    def test_valid_sections_list_is_capped(self, service, monkeypatch):
        body = "".join(f"# Part {n}\n\nText for part {n}.\n\n" for n in range(1, 81))
        _live(monkeypatch, body)
        result = service.fetch_doc("create-a-widget", section="999")
        assert len(result["valid_sections"]) == MAX_VALID_SECTIONS
        assert result["valid_sections"][0] == {"id": "1", "title": "Part 1"}

    def test_guide_read_failure_reports_the_url(self, monkeypatch, index_payload):
        for doc in index_payload["docs"]:
            doc["text"] = ""

        def boom(slug, **kwargs):
            raise ConnectionResetError(104, "Connection reset by peer")

        monkeypatch.setattr(core_tools, "fetch_guide", boom)
        result = DocsService(index_payload).fetch_doc("sprocket-basics")
        assert "error" in result
        assert result["url"].endswith("/sprocket-basics")
        assert "Traceback" not in str(result)


@pytest.fixture
def no_service():
    """No installed service before and after, so load_service starts cold."""
    core_tools.set_service(None)
    yield
    core_tools.set_service(None)


class TestServiceLoading:
    def test_set_service_installs_a_singleton(self, service):
        core_tools.set_service(service)
        assert core_tools.load_service() is service
        assert core_tools.search_docs("widget") != []

    def test_module_wrappers_use_the_installed_service(self, service, big_guide_live):
        core_tools.set_service(service)
        assert core_tools.fetch_doc("create-a-widget")["id"] == "create-a-widget"

    def test_is_ready_follows_set_service(self, service, no_service):
        assert core_tools.is_ready() is False
        core_tools.set_service(service)
        assert core_tools.is_ready() is True

    def test_missing_index_without_auto_sync_raises(self, cache_env, no_service):
        with pytest.raises(FileNotFoundError):
            core_tools.load_service(auto_sync=False)
        assert isinstance(core_tools.load_error(), FileNotFoundError)
        assert core_tools.is_ready() is False

    def test_auto_sync_builds_then_loads(self, cache_env, no_service, index_payload, monkeypatch):
        seen = {}

        def fake_sync(path=None, *, quiet=False, **kwargs):
            seen["quiet"] = quiet
            return write_index(index_payload, path)

        monkeypatch.setattr("akamai_cloud_docs_mcp.sync.sync", fake_sync)
        service = core_tools.load_service(auto_sync=True, quiet=True)
        assert seen["quiet"] is True
        assert service.search_docs("widget") != []
        assert core_tools.load_service() is service
        assert core_tools.load_error() is None

    def test_truncated_index_is_a_value_error(self, cache_env, no_service):
        cache_env.mkdir(parents=True)
        (cache_env / "index.json").write_text('{"docs": [', encoding="utf-8")
        with pytest.raises(ValueError):
            core_tools.load_service(auto_sync=False)

    def test_wrong_shape_index_is_a_value_error_without_the_path(self, cache_env, no_service):
        cache_env.mkdir(parents=True)
        (cache_env / "index.json").write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError) as caught:
            core_tools.load_service(auto_sync=False)
        assert str(cache_env) not in str(caught.value)

    def test_reloads_when_the_index_file_changes(self, cache_env, no_service, index_payload):
        path = write_index(index_payload)
        first = core_tools.load_service(auto_sync=False)
        assert core_tools.load_service() is first

        index_payload["docs"].append(
            {
                "id": "paint-a-widget",
                "kind": "guide",
                "title": "Paint a widget",
                "url": "https://techdocs.akamai.com/cloud-computing/docs/paint-a-widget.md",
                "text": "# Paint a widget\n\nPaint dries slowly on a widget.\n",
            }
        )
        # A sync writes the block for the documents it wrote, so do the same.
        index_payload["prebuilt"] = core_tools.SearchIndex(index_payload["docs"]).prebuilt()
        write_index(index_payload)
        # Two writes inside one clock tick could share a modification time.
        later = os.stat(path).st_mtime + 10
        os.utime(path, (later, later))

        second = core_tools.load_service()
        assert second is not first
        assert second.resolve("paint-a-widget") is not None

    def test_a_vanished_index_file_keeps_the_loaded_service(self, cache_env, no_service, index_payload):
        path = write_index(index_payload)
        first = core_tools.load_service(auto_sync=False)
        path.unlink()
        assert core_tools.load_service(auto_sync=False) is first

    def test_set_service_turns_the_reload_check_off(self, cache_env, no_service, index_payload, service):
        core_tools.set_service(service)
        path = write_index(index_payload)
        later = os.stat(path).st_mtime + 10
        os.utime(path, (later, later))
        assert core_tools.load_service() is service

    def test_a_retry_clears_the_last_failure_while_it_runs(
        self, cache_env, no_service, index_payload, monkeypatch
    ):
        """The stdio builder retries a failed build. While the retry runs, a
        tool call must find no error and answer "still building", not the
        failure the retry is fixing."""
        attempts = []
        inside = threading.Event()
        gate = threading.Event()

        def fake_sync(path=None, *, quiet=False, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise SyncError("no guide links parsed")
            inside.set()
            assert gate.wait(5), "the test never let the retry finish"
            return write_index(index_payload, path)

        monkeypatch.setattr("akamai_cloud_docs_mcp.sync.sync", fake_sync)
        with pytest.raises(SyncError):
            core_tools.load_service(auto_sync=True, quiet=True)
        assert isinstance(core_tools.load_error(), SyncError)

        retry = threading.Thread(
            target=lambda: core_tools.load_service(auto_sync=True, quiet=True), daemon=True
        )
        retry.start()
        assert inside.wait(5)
        assert core_tools.is_ready() is False
        assert core_tools.load_error() is None
        gate.set()
        retry.join(5)
        assert core_tools.is_ready() is True
        assert core_tools.load_error() is None

    def test_a_failed_reload_keeps_the_previous_service(
        self, cache_env, no_service, index_payload, monkeypatch, capsys
    ):
        """A file damaged after a good load is parsed once and never served."""
        from akamai_cloud_docs_mcp import sync as sync_module

        path = write_index(index_payload)
        first = core_tools.load_service(auto_sync=False)

        damaged = {**index_payload, "prebuilt": {**index_payload["prebuilt"], "lengths": []}}
        write_index(damaged)
        later = os.stat(path).st_mtime + 10
        os.utime(path, (later, later))
        reads = []
        real_load_index = sync_module.load_index
        monkeypatch.setattr(
            sync_module, "load_index", lambda p=None: (reads.append(1), real_load_index(p))[1]
        )

        assert core_tools.load_service(auto_sync=False) is first
        assert isinstance(core_tools.load_error(), ValueError)
        assert core_tools.current_service() is first
        assert core_tools.is_ready() is True
        # Parsed once, not on every call.
        assert core_tools.load_service() is first
        assert core_tools.load_service() is first
        assert len(reads) == 1
        err = capsys.readouterr().err
        assert err.count("Reloading the index failed") == 1
        assert index_payload["built_at"] in err
        assert str(cache_env) not in err

        # The next sync changes the time and gets its own attempt.
        write_index(index_payload)
        later += 10
        os.utime(path, (later, later))
        second = core_tools.load_service()
        assert second is not first
        assert core_tools.load_error() is None
        assert len(reads) == 2


class TestPrebuiltIndex:
    """`DocsService` loads the prebuilt block when it can and builds when it cannot."""

    @staticmethod
    def without_block(index_payload: dict, schema=INDEX_SCHEMA) -> dict:
        older = {key: value for key, value in index_payload.items() if key != "prebuilt"}
        older["schema"] = schema
        return older

    def test_a_current_index_is_served_from_its_block(self, index_payload, monkeypatch):
        def never(self):
            raise AssertionError("the search index was built at load")

        monkeypatch.setattr(core_tools.SearchIndex, "_build", never)
        service = DocsService(index_payload)
        assert service.search_docs("resize a widget")[0]["id"] == "resize-a-widget"
        assert service.resolve("post-widgets")["id"] == "POST /widgets"

    def test_an_index_from_an_older_sync_still_builds_at_load(self, index_payload):
        older = self.without_block(index_payload, schema=1)
        problem = core_tools.prebuilt_problem(older)
        assert problem == f"it was written with index schema 1; this version reads {INDEX_SCHEMA}"
        query = "resize a widget"
        assert DocsService(older).search_docs(query) == DocsService(index_payload).search_docs(query)

    def test_a_block_under_another_schema_number_is_not_trusted(self, index_payload, monkeypatch):
        """A ranking change bumps the schema; the old block must not serve it."""
        stale = {**index_payload, "schema": INDEX_SCHEMA + 1}
        assert "schema" in core_tools.prebuilt_problem(stale)
        built = []
        original = core_tools.SearchIndex._build
        monkeypatch.setattr(
            core_tools.SearchIndex, "_build", lambda self: (built.append(1), original(self))[1]
        )
        DocsService(stale)
        assert built == [1]

    def test_a_missing_block_is_named(self, index_payload):
        assert core_tools.prebuilt_problem(index_payload) == ""
        without = self.without_block(index_payload)
        assert core_tools.prebuilt_problem(without) == "it carries no prebuilt search index"
        assert core_tools.prebuilt_problem({**index_payload, "prebuilt": "x"}) != ""

    def test_a_damaged_block_is_a_value_error(self, index_payload):
        damaged = {**index_payload, "prebuilt": {**index_payload["prebuilt"], "lengths": []}}
        with pytest.raises(ValueError):
            DocsService(damaged)

    def test_load_service_records_a_damaged_block_as_a_value_error(
        self, cache_env, no_service, index_payload
    ):
        """The transports map a ValueError to "not readable; run sync"."""
        damaged = {**index_payload, "prebuilt": {**index_payload["prebuilt"], "lengths": []}}
        write_index(damaged)
        with pytest.raises(ValueError):
            core_tools.load_service(auto_sync=False)
        assert isinstance(core_tools.load_error(), ValueError)
        assert not core_tools.is_ready()
