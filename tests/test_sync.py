"""Index building with monkeypatched fetchers. No network."""

from __future__ import annotations

import argparse
import http.client
import io
import json
import urllib.error
from pathlib import Path

import pytest

from akamai_cloud_docs_mcp import sync as sync_module
from akamai_cloud_docs_mcp.config import INDEX_SCHEMA, MIN_GUIDES
from akamai_cloud_docs_mcp.core.catalog import FetchError, LinkEntry, clean_guide_markdown
from akamai_cloud_docs_mcp.core.index import SearchIndex
from akamai_cloud_docs_mcp.core.tools import prebuilt_problem
from akamai_cloud_docs_mcp.sync import (
    SyncError,
    TransientError,
    _post_json,
    _redirect_handler,
    build_guide_docs,
    build_index,
    fetch_guide_links,
    load_index,
    main,
    resume_command,
    run,
    sync,
    write_index,
)


@pytest.fixture
def fake_sources(llms_txt, guide_markdown):
    """A link fetcher and a guide fetcher over invented content."""

    def link_fetcher(url: str) -> str:
        return llms_txt

    def guide_fetcher(slug: str) -> tuple[str, str, str]:
        if slug == "delete-a-widget":
            raise FetchError("simulated 404")
        raw = guide_markdown.replace("Create a widget", f"Widget page {slug}")
        body, updated_at = clean_guide_markdown(raw)
        return body, updated_at, f"https://techdocs.akamai.com/cloud-computing/docs/{slug}.md"

    return link_fetcher, guide_fetcher


class TestFetchGuideLinks:
    def test_parses(self, llms_txt):
        entries = fetch_guide_links(fetcher=lambda url: llms_txt)
        assert len(entries) == 4

    def test_raises_when_nothing_parses(self):
        with pytest.raises(SyncError, match="no guide links"):
            fetch_guide_links(fetcher=lambda url: "# empty index\n")


class TestBuildGuideDocs:
    def test_skips_failures_and_reports_them(self, fake_sources, llms_txt):
        _, guide_fetcher = fake_sources
        entries = fetch_guide_links(fetcher=lambda url: llms_txt)
        docs, failed = build_guide_docs(entries, fetcher=guide_fetcher, concurrency=2)
        assert failed == ["delete-a-widget"]
        assert len(docs) == 3

    def test_document_shape(self, fake_sources, llms_txt):
        _, guide_fetcher = fake_sources
        entries = fetch_guide_links(fetcher=lambda url: llms_txt)
        docs, _ = build_guide_docs(entries, fetcher=guide_fetcher, concurrency=2)
        doc = docs[0]
        assert doc["kind"] == "guide"
        assert doc["id"] == "create-a-widget"
        assert doc["url"].endswith("create-a-widget.md")
        assert doc["updated_at"] == "2026-02-01T10:00:00.000Z"
        assert not doc["text"].startswith("---")
        assert "Sibling pages" not in doc["text"]

    def test_skips_an_empty_body(self, llms_txt, capsys):
        entries = fetch_guide_links(fetcher=lambda url: llms_txt)

        def blank(slug: str) -> tuple[str, str, str]:
            return "   \n", "", f"https://techdocs.akamai.com/cloud-computing/docs/{slug}.md"

        docs, failed = build_guide_docs(entries, fetcher=blank, concurrency=2)
        assert docs == []
        assert len(failed) == 4
        assert "empty body" in capsys.readouterr().err

    def test_progress_callback_fires(self, fake_sources, llms_txt):
        _, guide_fetcher = fake_sources
        entries = fetch_guide_links(fetcher=lambda url: llms_txt)
        seen: list[tuple[int, int]] = []
        build_guide_docs(
            entries,
            fetcher=guide_fetcher,
            concurrency=2,
            on_progress=lambda d, t: seen.append((d, t)),
        )
        assert seen[-1] == (4, 4)


class TestBuildIndex:
    def test_builds_with_a_lowered_guard(self, fake_sources):
        link_fetcher, guide_fetcher = fake_sources
        index = build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            include_api=False,
            quiet=True,
        )
        assert index["schema"] == INDEX_SCHEMA
        assert index["built_at"].endswith("Z")
        assert len(index["docs"]) == 3
        assert index["sources"]["guides_llms"].endswith("llms.txt")
        assert prebuilt_problem(index) == ""
        assert len(index["prebuilt"]["lengths"]) == 3

    def test_prebuilt_block_answers_like_a_build_from_the_documents(self, fake_sources):
        link_fetcher, guide_fetcher = fake_sources
        index = build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            include_api=False,
            quiet=True,
        )
        block = json.loads(json.dumps(index["prebuilt"]))
        from_block = SearchIndex.from_prebuilt(index["docs"], block)
        from_docs = SearchIndex(index["docs"])
        for query in ("create a widget", "sprocket", "resize", "delete-a-widget"):
            assert from_block.search(query, k=5) == from_docs.search(query, k=5), query

    def test_the_log_line_counts_the_search_terms(self, fake_sources, capsys):
        link_fetcher, guide_fetcher = fake_sources
        build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            include_api=False,
        )
        assert " search terms in " in capsys.readouterr().err

    def test_progress_and_totals_go_to_stderr(self, fake_sources, capsys):
        link_fetcher, guide_fetcher = fake_sources
        build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            include_api=False,
        )
        captured = capsys.readouterr()
        assert captured.out == "", "stdout is the stdio transport and must stay clean"
        assert "4 guide pages listed" in captured.err
        assert "fetched 4/4 guides" in captured.err
        assert "1 pages failed and were skipped" in captured.err
        assert "Built index: 3 guides, 0 API operations" in captured.err

    def test_guard_rejects_a_thin_index(self, fake_sources):
        link_fetcher, guide_fetcher = fake_sources
        with pytest.raises(SyncError, match="expected at least"):
            build_index(
                link_fetcher=link_fetcher,
                guide_fetcher=guide_fetcher,
                concurrency=2,
                min_guides=300,
                include_api=False,
                quiet=True,
            )


class TestWriteAndLoad:
    def test_round_trip(self, tmp_path, fake_sources):
        link_fetcher, guide_fetcher = fake_sources
        index = build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            include_api=False,
            quiet=True,
        )
        target = write_index(index, tmp_path / "sub" / "index.json")
        assert target.exists()
        assert load_index(target)["docs"] == index["docs"]

    def test_write_leaves_no_temp_file(self, tmp_path, fake_sources):
        link_fetcher, guide_fetcher = fake_sources
        index = build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            include_api=False,
            quiet=True,
        )
        write_index(index, tmp_path / "index.json")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_write_and_load_keep_the_prebuilt_block(self, tmp_path):
        path = write_index(TINY_INDEX, tmp_path / "index.json")
        loaded = load_index(path)
        assert loaded["prebuilt"] == TINY_INDEX["prebuilt"]
        assert prebuilt_problem(loaded) == ""

    def test_load_accepts_an_index_from_an_older_sync(self, tmp_path):
        """The local transports build at load, so an old file keeps serving."""
        path = write_index(without_block(TINY_INDEX, schema=1), tmp_path / "index.json")
        assert load_index(path)["docs"] == TINY_INDEX["docs"]

    def test_load_rejects_a_non_index_file(self, tmp_path):
        bad = tmp_path / "index.json"
        bad.write_text(json.dumps({"nope": True}), encoding="utf-8")
        with pytest.raises(SyncError, match="not a valid index"):
            load_index(bad)

    def test_sync_writes_to_the_cache_dir(self, cache_env, fake_sources):
        link_fetcher, guide_fetcher = fake_sources
        target = sync(
            quiet=True,
            min_guides=3,
            include_api=False,
            concurrency=2,
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
        )
        assert target == cache_env / "index.json"
        assert target.exists()

    def test_sync_reports_the_size_when_not_quiet(self, cache_env, fake_sources, capsys):
        link_fetcher, guide_fetcher = fake_sources
        sync(
            min_guides=3,
            include_api=False,
            concurrency=2,
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
        )
        assert "Wrote " in capsys.readouterr().err

    def test_patching_the_module_attribute_swaps_the_fetcher(self, fake_sources, monkeypatch):
        link_fetcher, guide_fetcher = fake_sources
        monkeypatch.setattr("akamai_cloud_docs_mcp.sync.fetch_url", link_fetcher)
        monkeypatch.setattr("akamai_cloud_docs_mcp.sync.fetch_guide", guide_fetcher)
        index = build_index(min_guides=3, include_api=False, concurrency=2, quiet=True)
        assert len(index["docs"]) == 3


class TestApiBranch:
    def test_include_api_adds_operations_from_the_spec(self, fake_sources, fake_spec, monkeypatch):
        # `build_api_docs` resolves `fetch_url` from its own module, so the
        # patch has to land there and not on `sync.fetch_url`.
        link_fetcher, guide_fetcher = fake_sources
        seen: list[str] = []

        def fake_fetch_url(url: str, **kwargs) -> str:
            seen.append(url)
            return json.dumps(fake_spec)

        monkeypatch.setattr("akamai_cloud_docs_mcp.core.api_catalog.fetch_url", fake_fetch_url)
        index = build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            min_operations_guard=1,
            include_api=True,
            quiet=True,
        )
        assert seen == [index["sources"]["openapi_url"]]
        assert index["sources"]["api_version"] == "4.215.0"
        kinds = {doc["kind"] for doc in index["docs"]}
        assert "guide" in kinds
        assert kinds - {"guide"}, "the spec should contribute at least one non-guide document"

    def test_guides_only_leaves_the_api_source_blank(self, fake_sources):
        link_fetcher, guide_fetcher = fake_sources
        index = build_index(
            link_fetcher=link_fetcher,
            guide_fetcher=guide_fetcher,
            concurrency=2,
            min_guides=3,
            include_api=False,
            quiet=True,
        )
        assert index["sources"]["openapi_url"] == ""
        assert index["sources"]["api_version"] == ""


# --- the operator command ----------------------------------------------------


_TINY_DOCS = [{"id": "create-a-widget", "kind": "guide", "title": "Create a widget", "text": "x"}]
TINY_INDEX = {
    "schema": INDEX_SCHEMA,
    "built_at": "2026-08-13T00:00:00Z",
    "sources": {},
    "docs": _TINY_DOCS,
    "prebuilt": SearchIndex(_TINY_DOCS).prebuilt(),
}


def without_block(index: dict, schema=INDEX_SCHEMA) -> dict:
    """`index` as a sync before the prebuilt block would have written it."""
    older = {key: value for key, value in index.items() if key != "prebuilt"}
    older["schema"] = schema
    return older


def namespace(**overrides) -> argparse.Namespace:
    """Every field `run()` reads, with the parser's defaults."""
    fields = {
        "quiet": True,
        "guides_only": True,
        "out": None,
        "push": "",
        "push_only": "",
        "token_env": "FUNCTIONS_SYNC_TOKEN",
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


@pytest.fixture
def stub_build(monkeypatch):
    """Replace the live crawl with a tiny index and record the call."""
    calls: list[dict] = []

    def fake_build_index(**kwargs) -> dict:
        calls.append(kwargs)
        return dict(TINY_INDEX)

    monkeypatch.setattr(sync_module, "build_index", fake_build_index)
    return calls


@pytest.fixture
def stub_push(monkeypatch):
    """Replace the upload and record what `run()` handed it."""
    calls: list[dict] = []

    def fake_push_index(index, app_url, token, **kwargs) -> dict:
        calls.append({"index": index, "app_url": app_url, "token": token, **kwargs})
        return {"chunks": 1}

    monkeypatch.setattr(sync_module, "push_index", fake_push_index)
    return calls


class TestSyncRun:
    def test_builds_and_writes_without_a_push(self, tmp_path, stub_build, stub_push):
        out = tmp_path / "index.json"
        assert run(namespace(out=out)) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["docs"] == TINY_INDEX["docs"]
        assert stub_push == []

    def test_push_reads_the_token_from_the_named_env_var(
        self, tmp_path, stub_build, stub_push, monkeypatch
    ):
        out = tmp_path / "index.json"
        monkeypatch.setenv("MY_TOKEN", "s3cret")
        code = run(namespace(out=out, push="https://x.fwf.app", token_env="MY_TOKEN"))
        assert code == 0
        assert out.exists()
        assert len(stub_push) == 1
        assert stub_push[0]["app_url"] == "https://x.fwf.app"
        assert stub_push[0]["token"] == "s3cret"
        assert stub_push[0]["index"]["docs"] == TINY_INDEX["docs"]

    def test_missing_token_is_exit_1_not_a_traceback(
        self, tmp_path, stub_build, monkeypatch, capsys
    ):
        # The real push_index stays in place so its token check fires.
        monkeypatch.delenv("MY_TOKEN", raising=False)
        code = run(
            namespace(out=tmp_path / "index.json", push="https://x.fwf.app", token_env="MY_TOKEN")
        )
        assert code == 1
        captured = capsys.readouterr()
        assert "no sync token" in captured.err
        assert captured.out == ""

    def test_guides_only_skips_the_api_catalog(self, tmp_path, stub_build, stub_push):
        run(namespace(out=tmp_path / "a.json", guides_only=True))
        run(namespace(out=tmp_path / "b.json", guides_only=False))
        assert [call["include_api"] for call in stub_build] == [False, True]
        assert stub_push == []

    def test_quiet_is_passed_through(self, tmp_path, stub_build, stub_push):
        run(namespace(out=tmp_path / "a.json", quiet=False, push="https://x.fwf.app"))
        assert stub_build[0]["quiet"] is False

    def test_progress_goes_to_stderr(self, tmp_path, stub_build, capsys):
        run(namespace(out=tmp_path / "index.json", quiet=False))
        captured = capsys.readouterr()
        assert "Wrote" in captured.err
        assert captured.out == ""

    def test_sync_error_from_build_is_exit_1(self, tmp_path, monkeypatch, capsys):
        def failing_build(**kwargs):
            raise SyncError("only 2 guide pages parsed")

        monkeypatch.setattr(sync_module, "build_index", failing_build)
        assert run(namespace(out=tmp_path / "index.json")) == 1
        err = capsys.readouterr().err
        assert "sync failed: only 2 guide pages parsed" in err
        assert "push-only" not in err, "nothing to resume when no index was written"

    def test_catalog_error_from_build_is_exit_1(self, tmp_path, monkeypatch, capsys):
        def failing_build(**kwargs):
            raise FetchError("simulated 500")

        monkeypatch.setattr(sync_module, "build_index", failing_build)
        assert run(namespace(out=tmp_path / "index.json")) == 1
        assert "sync failed: simulated 500" in capsys.readouterr().err

    def test_api_floor_is_exit_1_not_a_traceback(self, tmp_path, fake_spec, monkeypatch, capsys):
        """The real build, with a spec that parses to too few operations.

        The floor raises ApiCatalogError, which is not a SyncError. Enough
        guides are faked to clear the guide floor so the API floor is the one
        that trips.
        """
        entries = [
            LinkEntry(
                slug=f"guide-{n}",
                title=f"Guide {n}",
                url=f"https://techdocs.akamai.com/cloud-computing/docs/guide-{n}",
            )
            for n in range(MIN_GUIDES)
        ]

        def fake_guide(slug: str) -> tuple[str, str, str]:
            return (
                f"# {slug}\n\nbody",
                "",
                f"https://techdocs.akamai.com/cloud-computing/docs/{slug}.md",
            )

        monkeypatch.setattr(sync_module, "fetch_guide_links", lambda **kwargs: entries)
        monkeypatch.setattr(sync_module, "fetch_guide", fake_guide)
        monkeypatch.setattr(
            "akamai_cloud_docs_mcp.core.api_catalog.fetch_url",
            lambda url, **kwargs: json.dumps(fake_spec),
        )
        out = tmp_path / "index.json"
        assert run(namespace(out=out, guides_only=False)) == 1
        captured = capsys.readouterr()
        assert "sync failed: only " in captured.err
        assert " operations parsed, expected at least " in captured.err
        assert captured.out == ""
        assert not out.exists(), "a thin index is refused, not written"

    def test_failed_push_prints_the_resume_command(self, tmp_path, stub_build, monkeypatch, capsys):
        def failing_push(index, app_url, token, **kwargs):
            raise SyncError("chunk 2 rejected with HTTP 502: bad gateway")

        monkeypatch.setattr(sync_module, "push_index", failing_push)
        monkeypatch.setenv("T", "s3cret-value")
        out = tmp_path / "index.json"
        code = run(namespace(out=out, push="https://x.fwf.app", token_env="T"))
        assert code == 1
        assert out.exists(), "the index on disk survives a failed push"
        err = capsys.readouterr().err
        assert "sync failed: chunk 2 rejected with HTTP 502" in err
        assert (
            f"akamai-cloud-docs-mcp sync --push-only https://x.fwf.app --token-env T --out {out}"
            in err
        )
        assert "s3cret-value" not in err, "the resume command names the variable, never the token"

    def test_push_only_never_calls_build_index(self, tmp_path, stub_build, stub_push, monkeypatch):
        out = tmp_path / "index.json"
        write_index(TINY_INDEX, out)
        monkeypatch.setenv("T", "tok")
        code = run(namespace(out=out, push_only="https://x.fwf.app", token_env="T"))
        assert code == 0
        assert stub_build == []
        assert len(stub_push) == 1
        assert stub_push[0]["index"]["docs"] == TINY_INDEX["docs"]
        assert stub_push[0]["token"] == "tok"

    def test_push_only_reads_the_cache_dir_by_default(
        self, cache_env, stub_build, stub_push, monkeypatch
    ):
        write_index(TINY_INDEX, cache_env / "index.json")
        monkeypatch.setenv("T", "tok")
        assert run(namespace(push_only="https://x.fwf.app", token_env="T")) == 0
        assert stub_build == []
        assert stub_push[0]["index"]["built_at"] == TINY_INDEX["built_at"]

    @pytest.mark.parametrize(
        "older", [without_block(TINY_INDEX, schema=1), without_block(TINY_INDEX)]
    )
    def test_push_only_refuses_an_index_the_app_could_not_serve(
        self, tmp_path, stub_build, stub_push, monkeypatch, capsys, older
    ):
        """Said before the upload, with the command that rebuilds it."""
        out = tmp_path / "index.json"
        write_index(older, out)
        monkeypatch.setenv("T", "tok")
        assert run(namespace(out=out, push_only="https://x.fwf.app", token_env="T")) == 1
        err = capsys.readouterr().err
        assert "sync failed: the index on disk cannot be pushed: it " in err
        # The hint must rebuild into the same file and read the same token
        # variable the operator named, or following it fails on both.
        assert (
            f"Run `akamai-cloud-docs-mcp sync --push https://x.fwf.app --token-env T --out {out}`"
            in err
        )
        assert stub_build == []
        assert stub_push == []

    def test_push_only_with_no_index_on_disk_is_exit_1(
        self, tmp_path, stub_build, stub_push, capsys
    ):
        code = run(namespace(out=tmp_path / "missing.json", push_only="https://x.fwf.app"))
        assert code == 1
        err = capsys.readouterr().err
        assert "sync failed: cannot read the index to push" in err
        assert stub_build == []
        assert stub_push == []

    def test_push_only_with_a_non_index_file_is_exit_1(
        self, tmp_path, stub_build, stub_push, capsys
    ):
        bad = tmp_path / "index.json"
        bad.write_text("[]", encoding="utf-8")
        assert run(namespace(out=bad, push_only="https://x.fwf.app")) == 1
        assert "not a valid index" in capsys.readouterr().err
        assert stub_push == []

    @pytest.mark.parametrize(
        "content",
        [b'{"schema": 2, "docs": [', b"\xff\xfe{}"],
        ids=["truncated-json", "not-utf8"],
    )
    def test_push_only_with_a_corrupt_index_is_exit_1(
        self, tmp_path, stub_build, stub_push, capsys, content
    ):
        # A sync killed mid-write on a filesystem without atomic rename, or a
        # hand edit, leaves a file json.load cannot parse. Both decode errors
        # are ValueErrors, and the operator gets a line, not a traceback.
        bad = tmp_path / "index.json"
        bad.write_bytes(content)
        assert run(namespace(out=bad, push_only="https://x.fwf.app")) == 1
        captured = capsys.readouterr()
        assert "sync failed: cannot read the index to push: " in captured.err
        assert captured.out == ""
        assert stub_build == []
        assert stub_push == []


class TestResumeCommand:
    def test_names_the_url_the_env_var_and_the_out_path(self):
        args = namespace(out=Path("/var/cache/index.json"), token_env="T")
        assert resume_command(args, "https://x.fwf.app") == (
            "akamai-cloud-docs-mcp sync --push-only https://x.fwf.app --token-env T"
            " --out /var/cache/index.json"
        )

    def test_omits_out_when_the_cache_dir_is_used(self):
        assert resume_command(namespace(), "https://x.fwf.app") == (
            "akamai-cloud-docs-mcp sync --push-only https://x.fwf.app --token-env FUNCTIONS_SYNC_TOKEN"
        )

    def test_quotes_a_path_with_a_space(self):
        args = namespace(out=Path("/tmp/my cache/index.json"))
        assert "'/tmp/my cache/index.json'" in resume_command(args, "https://x.fwf.app")

    def test_a_relative_out_path_is_made_absolute(self, tmp_path, monkeypatch):
        # The line is pasted later, often from another directory, so the path
        # it names must not depend on where the failed sync ran.
        monkeypatch.chdir(tmp_path)
        args = namespace(out=Path("index.json"), token_env="T")
        command = resume_command(args, "https://x.fwf.app")
        assert command.endswith(f"--token-env T --out {tmp_path / 'index.json'}")
        assert " --out index.json" not in command


class TestMain:
    def test_parses_and_runs(self, tmp_path, stub_build, stub_push, monkeypatch):
        out = tmp_path / "index.json"
        monkeypatch.setenv("T", "tok")
        argv = [
            "--quiet",
            "--guides-only",
            "--out",
            str(out),
            "--push",
            "https://x.fwf.app",
            "--token-env",
            "T",
        ]
        assert main(argv) == 0
        assert stub_build[0] == {"quiet": True, "include_api": False}
        assert stub_push[0]["app_url"] == "https://x.fwf.app"
        assert stub_push[0]["token"] == "tok"
        assert stub_push[0]["quiet"] is True

    def test_defaults(self, tmp_path, stub_build, stub_push, monkeypatch):
        parser = argparse.ArgumentParser()
        sync_module.add_arguments(parser)
        args = parser.parse_args([])
        assert args.out is None
        assert args.guides_only is False
        assert args.quiet is False
        assert args.push == ""
        assert args.push_only == ""
        assert args.token_env == "FUNCTIONS_SYNC_TOKEN"

    def test_push_and_push_only_are_exclusive(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--push", "https://x.fwf.app", "--push-only", "https://x.fwf.app"])
        assert exc.value.code == 2
        assert "not allowed with" in capsys.readouterr().err

    def test_push_only_from_argv(self, tmp_path, stub_build, stub_push, monkeypatch):
        out = tmp_path / "index.json"
        write_index(TINY_INDEX, out)
        monkeypatch.setenv("FUNCTIONS_SYNC_TOKEN", "tok")
        assert main(["--push-only", "https://x.fwf.app", "--out", str(out)]) == 0
        assert stub_build == []
        assert stub_push[0]["token"] == "tok"


# --- the HTTP client ---------------------------------------------------------


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def patch_open(monkeypatch, outcome):
    """Make the opener return `outcome`, or raise it when it is an exception."""
    import urllib.request

    seen: list = []

    def fake_open(self, request, timeout=None):
        seen.append((request, timeout))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    return seen


class TestPostJson:
    URL = "https://x.fwf.app/admin/sync"
    HEADERS = {"Authorization": "Bearer t", "Content-Type": "application/json"}

    def test_returns_status_and_body_on_success(self, monkeypatch):
        seen = patch_open(monkeypatch, FakeResponse(b'{"ok":true}'))
        assert _post_json(self.URL, b"{}", self.HEADERS) == (200, '{"ok":true}')
        request, timeout = seen[0]
        assert request.get_method() == "POST"
        assert request.data == b"{}"
        assert request.get_header("Authorization") == "Bearer t"
        assert timeout is not None

    def test_http_error_is_returned_not_raised(self, monkeypatch):
        error = urllib.error.HTTPError(
            self.URL, 401, "nope", {}, io.BytesIO(b'{"error":"unauthorized"}')
        )
        patch_open(monkeypatch, error)
        assert _post_json(self.URL, b"{}", self.HEADERS) == (401, '{"error":"unauthorized"}')

    def test_5xx_is_returned_so_the_caller_can_retry(self, monkeypatch):
        error = urllib.error.HTTPError(self.URL, 503, "busy", {}, io.BytesIO(b"busy"))
        patch_open(monkeypatch, error)
        assert _post_json(self.URL, b"{}", self.HEADERS) == (503, "busy")

    def test_url_error_is_a_transient_error(self, monkeypatch):
        patch_open(monkeypatch, urllib.error.URLError("dns failed"))
        with pytest.raises(TransientError, match="could not reach") as exc:
            _post_json(self.URL, b"{}", self.HEADERS)
        assert isinstance(exc.value, SyncError)
        assert "dns failed" in str(exc.value)

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("timed out"),
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            ConnectionResetError(104, "Connection reset by peer"),
            http.client.IncompleteRead(b"partial"),
            http.client.BadStatusLine("garbage"),
        ],
        ids=["timeout-before-headers", "closed-after-request", "reset", "short-body", "bad-status"],
    )
    def test_a_failure_after_the_request_is_sent_is_a_transient_error(self, monkeypatch, error):
        # urllib wraps only the send in URLError. These come out of
        # getresponse() and read() bare, and each one means the app never
        # gave a usable answer, so each one must be retried like a refused
        # connection is.
        patch_open(monkeypatch, error)
        with pytest.raises(TransientError, match="could not reach") as exc:
            _post_json(self.URL, b"{}", self.HEADERS)
        assert str(error) in str(exc.value)
        assert exc.value.__cause__ is None, "no traceback chain for an operator message"

    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_a_redirect_is_refused(self, monkeypatch, code):
        error = urllib.error.HTTPError(
            self.URL, code, "moved", {"Location": "https://evil.example"}, None
        )
        patch_open(monkeypatch, error)
        with pytest.raises(SyncError, match="app URL redirected") as exc:
            _post_json(self.URL, b"{}", self.HEADERS)
        assert "refusing to resend the token" in str(exc.value)
        assert not isinstance(exc.value, TransientError), "a redirect must not be retried"

    def test_the_real_client_never_touches_the_network(self):
        # conftest blocks outbound connections with a RuntimeError. That is
        # outside the family _post_json maps to TransientError (OSError and
        # http.client.HTTPException), so it surfaces as is.
        with pytest.raises(RuntimeError, match="network access is not allowed"):
            _post_json(self.URL, b"{}", self.HEADERS)


class TestRedirectHandler:
    def test_refuses_every_redirect_code(self):
        import urllib.request

        handler = _redirect_handler()
        request = urllib.request.Request("https://x.fwf.app/admin/sync", data=b"{}", method="POST")
        for code in (301, 302, 303, 307, 308):
            with pytest.raises(urllib.error.HTTPError) as exc:
                handler.redirect_request(request, None, code, "moved", {}, "https://evil.example/")
            assert exc.value.code == code

    def test_is_installed_in_the_opener(self, monkeypatch):
        import urllib.request

        built: list = []
        real = urllib.request.build_opener

        def spy(*handlers):
            built.extend(handlers)
            return real(*handlers)

        monkeypatch.setattr(urllib.request, "build_opener", spy)
        patch_open(monkeypatch, FakeResponse(b"ok"))
        _post_json("https://x.fwf.app/admin/sync", b"{}", {})
        assert any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in built)
        assert type(built[0]) is not urllib.request.HTTPRedirectHandler
