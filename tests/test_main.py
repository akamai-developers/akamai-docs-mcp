"""The command line, the status subcommand, and the stdio transport.

The subprocess test at the end launches the real entry point and drives it
with the SDK's stdio client. It runs in a separate process, so the suite's
socket block does not reach it; the prebuilt index and an API id keep it off
the network anyway.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

import anyio
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from akamai_cloud_docs_mcp import __main__ as cli
from akamai_cloud_docs_mcp import stdio, sync
from akamai_cloud_docs_mcp.core import tools as core_tools


class TestParser:
    def test_no_arguments_means_stdio_with_auto_sync(self):
        args = cli.build_parser().parse_args([])
        assert args.http is False
        assert args.command is None
        assert args.auto_sync is None
        # Only an explicit --no-auto-sync turns the stdio default off.
        assert args.auto_sync is not False

    def test_http_defaults_to_no_auto_sync(self):
        args = cli.build_parser().parse_args(["--http"])
        assert args.http is True
        assert bool(args.auto_sync) is False

    def test_auto_sync_flags_are_exclusive(self, capsys):
        with pytest.raises(SystemExit) as failure:
            cli.build_parser().parse_args(["--auto-sync", "--no-auto-sync"])
        assert failure.value.code == 2

    def test_http_with_sync_is_rejected(self, capsys):
        with pytest.raises(SystemExit) as failure:
            cli.main(["--http", "sync"])
        assert failure.value.code == 2
        assert "--http" in capsys.readouterr().err

    def test_http_with_status_is_rejected(self):
        with pytest.raises(SystemExit) as failure:
            cli.main(["--http", "status"])
        assert failure.value.code == 2

    @pytest.mark.parametrize("level", cli.LOG_LEVELS)
    def test_log_level_round_trips(self, level):
        assert cli.build_parser().parse_args(["--log-level", level]).log_level == level

    def test_log_level_defaults_to_none_so_each_mode_picks_its_own(self):
        assert cli.build_parser().parse_args([]).log_level is None
        assert cli.build_parser().parse_args(["sync"]).log_level is None

    @pytest.mark.parametrize(
        "argv", [["--log-level", "debug", "sync"], ["sync", "--log-level", "debug"]]
    )
    def test_log_level_is_accepted_on_either_side_of_sync(self, argv):
        assert cli.build_parser().parse_args(argv).log_level == "debug"

    def test_sync_honours_the_log_level(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: seen.update(kwargs))
        monkeypatch.setattr(sync, "run", lambda args: 0)
        assert cli.main(["sync", "--log-level", "debug"]) == 0
        assert seen["level"] == "DEBUG"
        seen.clear()
        assert cli.main(["sync"]) == 0
        assert seen == {}

    def test_http_path_must_start_with_a_slash(self, capsys):
        with pytest.raises(SystemExit) as failure:
            cli.main(["--http", "--path", "mcp"])
        assert failure.value.code == 2
        assert "slash" in capsys.readouterr().err

    def test_http_path_loses_its_trailing_slash(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("akamai_cloud_docs_mcp.http.run", lambda **kwargs: seen.update(kwargs))
        assert cli.main(["--http", "--path", "/docs-mcp/"]) == 0
        assert seen["path"] == "/docs-mcp"

    def test_unknown_log_level_is_rejected(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["--log-level", "loud"])

    def test_allowed_hosts_and_origins_repeat(self):
        args = cli.build_parser().parse_args(
            [
                "--http",
                "--allowed-host",
                "docs.example.com",
                "--allowed-host",
                "docs.example.com:*",
                "--allowed-origin",
                "https://docs.example.com",
            ]
        )
        assert args.allowed_hosts == ["docs.example.com", "docs.example.com:*"]
        assert args.allowed_origins == ["https://docs.example.com"]

    def test_allowed_lists_default_empty(self):
        args = cli.build_parser().parse_args([])
        assert args.allowed_hosts == []
        assert args.allowed_origins == []

    def test_sync_help_carries_a_description(self, capsys):
        with pytest.raises(SystemExit) as done:
            cli.main(["sync", "--help"])
        assert done.value.code == 0
        assert "OpenAPI" in capsys.readouterr().out

    def test_http_flags_are_grouped(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        out = capsys.readouterr().out
        assert "HTTP mode (with --http)" in out
        assert "{sync,status}" in out

    def test_http_mode_passes_every_flag_through(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "akamai_cloud_docs_mcp.http.run", lambda **kwargs: seen.update(kwargs)
        )
        code = cli.main(
            ["--http", "--port", "9", "--log-level", "debug", "--allowed-host", "h:*"]
        )
        assert code == 0
        assert seen == {
            "host": "127.0.0.1",
            "port": 9,
            "path": "/mcp",
            "auto_sync": False,
            "allowed_hosts": ["h:*"],
            "allowed_origins": [],
            "log_level": "debug",
        }

    def test_stdio_mode_passes_auto_sync_and_log_level(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("akamai_cloud_docs_mcp.stdio.run", lambda **kwargs: seen.update(kwargs))
        assert cli.main(["--no-auto-sync", "--log-level", "info"]) == 0
        assert seen == {"auto_sync": False, "log_level": "info"}
        assert cli.main([]) == 0
        assert seen == {"auto_sync": True, "log_level": None}


# --- status ------------------------------------------------------------------


def write_index(cache_env, index_payload, **overrides):
    payload = {**index_payload, **overrides}
    return sync.write_index(payload, cache_env / "index.json")


class TestStatus:
    def test_missing_index_exits_2_and_names_the_sync_command(self, cache_env, capsys):
        assert cli.main(["status"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert str(cache_env / "index.json") in captured.err
        assert "akamai-cloud-docs-mcp sync" in captured.err

    def test_fresh_index_exits_0(self, cache_env, index_payload, capsys):
        write_index(cache_env, index_payload, sources={"api_version": "4.0.1"})
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "STALE" not in out
        assert "guides:      3" in out
        assert "api:         1" in out
        assert "api_version: 4.0.1" in out
        assert str(cache_env / "index.json") in out

    def test_stale_index_exits_1_and_says_so(self, cache_env, index_payload, capsys):
        write_index(cache_env, index_payload, built_at="2020-01-01T00:00:00Z")
        assert cli.main(["status"]) == 1
        out = capsys.readouterr().out
        assert "STALE" in out
        assert "akamai-cloud-docs-mcp sync" in out

    def test_json_shape(self, cache_env, index_payload, capsys):
        path = write_index(cache_env, index_payload, built_at="2020-01-01T00:00:00Z")
        assert cli.main(["status", "--json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert set(report) == {
            "path",
            "size_bytes",
            "built_at",
            "age_days",
            "stale",
            "guides",
            "api",
            "api_version",
        }
        assert report["path"] == str(path)
        assert report["size_bytes"] == path.stat().st_size
        assert report["built_at"] == "2020-01-01T00:00:00Z"
        assert report["age_days"] > 365
        assert report["stale"] is True
        assert (report["guides"], report["api"]) == (3, 1)

    def test_unparsable_built_at_is_reported_not_fatal(self, cache_env, index_payload, capsys):
        write_index(cache_env, index_payload, built_at="yesterday")
        assert cli.main(["status", "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["age_days"] is None
        assert report["stale"] is False

    def test_corrupt_index_exits_2(self, cache_env, capsys):
        cache_env.mkdir(parents=True)
        (cache_env / "index.json").write_text("{not json", encoding="utf-8")
        assert cli.main(["status"]) == 2
        assert "akamai-cloud-docs-mcp sync" in capsys.readouterr().err

    def test_index_without_docs_exits_2(self, cache_env, capsys):
        cache_env.mkdir(parents=True)
        (cache_env / "index.json").write_text('{"built_at": "x"}', encoding="utf-8")
        assert cli.main(["status"]) == 2
        assert "sync" in capsys.readouterr().err


# --- stdio -------------------------------------------------------------------


class _FakeServer:
    def __init__(self):
        self.transport = None

    def run(self, transport):
        self.transport = transport


@pytest.fixture
def fake_server(monkeypatch):
    """Stand in for the MCP server so `stdio.run` returns at once."""
    made = {}

    def build(**kwargs):
        made["kwargs"] = kwargs
        made["server"] = _FakeServer()
        return made["server"]

    monkeypatch.setattr(stdio, "build_server", build)
    # The background builder and the stale-index refresher would both try the
    # network. Tests that care about them install their own stand-ins.
    monkeypatch.setattr(core_tools, "load_service", lambda **kwargs: None)
    monkeypatch.setattr(stdio.sync, "sync", lambda **kwargs: None)
    return made


def _watch_sync(monkeypatch):
    """Replace `sync.sync` with a recorder; returns (started event, seen kwargs)."""
    started = threading.Event()
    seen = {}

    def fake_sync(**kwargs):
        seen.update(kwargs)
        seen["thread"] = threading.current_thread().name
        started.set()

    monkeypatch.setattr(stdio.sync, "sync", fake_sync)
    return started, seen


class TestStdioStartup:
    def test_serves_over_stdio_with_the_warning_default(self, fake_server, cache_env):
        stdio.run()
        assert fake_server["server"].transport == "stdio"
        assert fake_server["kwargs"] == {"auto_sync": True, "log_level": "warning"}

    def test_explicit_log_level_wins(self, fake_server, cache_env):
        stdio.run(auto_sync=False, log_level="debug")
        assert fake_server["kwargs"] == {"auto_sync": False, "log_level": "debug"}

    def test_missing_index_builds_in_the_background(self, fake_server, cache_env, monkeypatch):
        started = threading.Event()
        seen = {}

        def fake_load(**kwargs):
            seen.update(kwargs)
            seen["thread"] = threading.current_thread().name
            started.set()

        monkeypatch.setattr(core_tools, "load_service", fake_load)
        stdio.run(auto_sync=True)
        assert started.wait(5)
        assert seen["auto_sync"] is True
        assert seen["quiet"] is False, "progress belongs on stderr, which stdio can afford"
        assert seen["thread"] != threading.current_thread().name

    def test_missing_index_stays_missing_without_auto_sync(self, fake_server, cache_env, monkeypatch):
        def explode(**kwargs):
            raise AssertionError("must not build")

        monkeypatch.setattr(core_tools, "load_service", explode)
        stdio.run(auto_sync=False)

    def test_existing_index_is_not_loaded_at_startup(
        self, fake_server, cache_env, index_payload, monkeypatch, capsys
    ):
        write_index(cache_env, index_payload)

        def explode(**kwargs):
            raise AssertionError("must not load")

        monkeypatch.setattr(core_tools, "load_service", explode)
        stdio.run(auto_sync=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_old_index_prints_the_staleness_line(self, fake_server, cache_env, index_payload, capsys):
        write_index(cache_env, index_payload, built_at="2020-01-01T00:00:00Z")
        stdio.run(auto_sync=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "sync" in captured.err

    def test_a_failed_background_build_is_one_stderr_line_per_attempt(self, monkeypatch, capsys):
        attempts = []

        def fail(**kwargs):
            attempts.append(kwargs)
            raise sync.SyncError("no guide links parsed")

        monkeypatch.setattr(core_tools, "load_service", fail)
        slept = []
        stdio._build_index_in_background(sleep=slept.append)
        captured = capsys.readouterr()
        assert captured.out == ""
        lines = captured.err.strip().splitlines()
        assert len(lines) == len(attempts) == 3
        assert slept == list(stdio.BUILD_RETRY_DELAYS) == [30, 60]
        assert lines[0] == "Building the index failed: no guide links parsed. Retrying in 30 seconds."
        assert lines[1] == "Building the index failed: no guide links parsed. Retrying in 60 seconds."
        assert lines[2].startswith("Building the index failed: no guide links parsed. Run ")
        assert "akamai-cloud-docs-mcp sync" in lines[2]

    def test_a_build_that_fails_once_succeeds_on_the_retry(self, monkeypatch, capsys):
        # The laptop-before-VPN case: the first attempt cannot reach the site,
        # the second can.
        attempts = []

        def flaky(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise sync.SyncError("llms.txt could not be reached")

        monkeypatch.setattr(core_tools, "load_service", flaky)
        slept = []
        stdio._build_index_in_background(sleep=slept.append)
        assert len(attempts) == 2
        assert slept == [30]
        lines = capsys.readouterr().err.strip().splitlines()
        assert lines == [
            "Building the index failed: llms.txt could not be reached. Retrying in 30 seconds."
        ]

    def test_stale_index_rebuilds_in_the_background(
        self, fake_server, cache_env, index_payload, monkeypatch, capsys
    ):
        write_index(cache_env, index_payload, built_at="2020-01-01T00:00:00Z")
        started, seen = _watch_sync(monkeypatch)

        def explode(**kwargs):
            raise AssertionError("the old index serves; nothing loads at startup")

        monkeypatch.setattr(core_tools, "load_service", explode)
        stdio.run(auto_sync=True)
        assert started.wait(5)
        assert seen["quiet"] is False
        assert seen["thread"] != threading.current_thread().name
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Index built" in captured.err

    def test_fresh_index_does_not_rebuild(self, fake_server, cache_env, index_payload, monkeypatch):
        write_index(cache_env, index_payload)
        started, _ = _watch_sync(monkeypatch)
        stdio.run(auto_sync=True)
        assert not started.wait(0.2)

    def test_stale_index_stays_stale_without_auto_sync(
        self, fake_server, cache_env, index_payload, monkeypatch, capsys
    ):
        write_index(cache_env, index_payload, built_at="2020-01-01T00:00:00Z")
        started, _ = _watch_sync(monkeypatch)
        stdio.run(auto_sync=False)
        assert not started.wait(0.2)
        # The operator still hears about it.
        assert "sync" in capsys.readouterr().err

    def test_missing_index_uses_the_builder_not_the_refresher(
        self, fake_server, cache_env, monkeypatch
    ):
        started, _ = _watch_sync(monkeypatch)
        built = threading.Event()
        monkeypatch.setattr(core_tools, "load_service", lambda **kwargs: built.set())
        stdio.run(auto_sync=True)
        assert built.wait(5)
        assert not started.wait(0.2)

    def test_a_failed_background_rebuild_is_one_stderr_line(self, monkeypatch, capsys):
        def fail(**kwargs):
            raise sync.SyncError("no guide links parsed")

        monkeypatch.setattr(stdio.sync, "sync", fail)
        stdio._refresh_index_in_background()
        captured = capsys.readouterr()
        assert captured.out == ""
        lines = captured.err.strip().splitlines()
        assert lines[0].startswith("Rebuilding the index in the background")
        assert lines[1] == "Rebuilding the index failed: no guide links parsed"
        assert len(lines) == 2


class TestStalenessFromFile:
    def test_reads_built_at_without_parsing_the_whole_file(self, tmp_path):
        path = tmp_path / "index.json"
        # A fake body far larger than the header read, and not valid JSON at
        # all past the header, to prove only the header is consulted.
        path.write_bytes(b'{"schema":1,"built_at":"2020-01-01T00:00:00Z","docs":[' + b"x" * 100_000)
        assert "sync" in stdio.staleness_line_from_file(path)

    def test_fresh_file_is_silent(self, cache_env, index_payload):
        path = write_index(cache_env, index_payload)
        assert stdio.staleness_line_from_file(path) == ""

    def test_garbage_is_silent(self, tmp_path):
        path = tmp_path / "index.json"
        path.write_bytes(b"\xff\xfe not an index")
        assert stdio.staleness_line_from_file(path) == ""

    def test_missing_file_is_silent(self, tmp_path):
        assert stdio.staleness_line_from_file(tmp_path / "nope.json") == ""


# --- the real entry point over stdio -----------------------------------------


def text_of(result) -> str:
    return "".join(block.text for block in result.content if getattr(block, "text", None))


class TestStdioSubprocess:
    def test_real_process_answers_without_touching_the_network(
        self, cache_env, index_payload, tmp_path
    ):
        sync.write_index(index_payload, cache_env / "index.json")
        stderr_path = tmp_path / "stderr.txt"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "akamai_cloud_docs_mcp", "--no-auto-sync"],
            env={"AKAMAI_DOCS_MCP_CACHE_DIR": str(cache_env), "PYTHONUNBUFFERED": "1"},
        )

        async def drive():
            with stderr_path.open("w") as errlog:
                async with Client(stdio_client(params, errlog=errlog)) as client:
                    tools = await client.list_tools()
                    search = await client.call_tool("search_docs", {"query": "resize a widget"})
                    fetch = await client.call_tool("fetch_doc", {"id": "POST /widgets"})
                    return tools, search, fetch

        tools, search, fetch = anyio.run(drive)

        assert {tool.name for tool in tools.tools} == {"search_docs", "fetch_doc"}
        assert search.is_error is not True
        assert "resize-a-widget" in text_of(search)
        assert fetch.is_error is not True
        assert "POST /widgets" in text_of(fetch)
        stderr = stderr_path.read_text()
        assert "Traceback" not in stderr
        assert str(cache_env) not in stderr

    def test_version_flag_goes_to_stdout(self):
        completed = subprocess.run(
            [sys.executable, "-m", "akamai_cloud_docs_mcp", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0
        assert completed.stdout.startswith("akamai-cloud-docs-mcp ")
