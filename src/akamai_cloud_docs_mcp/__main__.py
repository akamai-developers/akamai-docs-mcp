"""Command line entry point.

    akamai-cloud-docs-mcp                 serve MCP over stdio
    akamai-cloud-docs-mcp --http          serve stateless streamable HTTP
    akamai-cloud-docs-mcp sync            build the documentation index
    akamai-cloud-docs-mcp status          report the index on disk
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from . import sync as sync_module
from .config import STALE_AFTER_DAYS, index_path
from .http import DEFAULT_HOST, DEFAULT_PATH, DEFAULT_PORT, normalize_path

LOG_LEVELS = ("debug", "info", "warning", "error")

SYNC_DESCRIPTION = (
    "Fetch the guide index, every guide page, and the Linode OpenAPI spec, then "
    "write index.json to the cache dir. About 45 seconds and 3 MB. Nothing else "
    "writes the index."
)

STATUS_DESCRIPTION = (
    "Report the index on disk: path, size, build time, age, and document counts. "
    f"Exit 0 when fresh, 1 when older than {STALE_AFTER_DAYS} days, 2 when missing."
)


def _log_level_option(default) -> argparse.ArgumentParser:
    """A parent parser carrying --log-level, so `sync` accepts it on either side.

    The top level gets the None default; the subcommand gets SUPPRESS so its
    copy cannot overwrite a value given before `sync`.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default=default,
        help="log verbosity on stderr (default: warning for stdio, info with --http)",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="akamai-cloud-docs-mcp",
        description="MCP server for Akamai Cloud documentation and the Linode API reference.",
        epilog="With no arguments, serves MCP over stdio.",
        parents=[_log_level_option(None)],
    )
    parser.add_argument(
        "--version", action="version", version=f"akamai-cloud-docs-mcp {__version__}"
    )
    parser.add_argument(
        "--http", action="store_true", help="serve stateless streamable HTTP instead of stdio"
    )
    # stdio builds the index on first use. HTTP does not, because an operator
    # should not discover a 45 second index build on a client's first request.
    auto_sync = parser.add_mutually_exclusive_group()
    auto_sync.add_argument(
        "--auto-sync",
        dest="auto_sync",
        action="store_true",
        default=None,
        help=(
            "build a missing index; over stdio also rebuild one older than "
            f"{STALE_AFTER_DAYS} days in the background (default for stdio)"
        ),
    )
    auto_sync.add_argument(
        "--no-auto-sync",
        dest="auto_sync",
        action="store_false",
        help="never build or rebuild the index; a missing index is an error (default for --http)",
    )

    http_group = parser.add_argument_group("HTTP mode (with --http)")
    http_group.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind address (default: {DEFAULT_HOST})"
    )
    http_group.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})"
    )
    http_group.add_argument("--path", default=DEFAULT_PATH, help=f"route (default: {DEFAULT_PATH})")
    http_group.add_argument(
        "--allowed-host",
        dest="allowed_hosts",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            "accept this Host header; repeatable. A bare name matches a Host with no "
            "port, NAME:* matches any port. Give both when the proxy forwards a port, "
            "for example --allowed-host docs.example.com --allowed-host docs.example.com:*. "
            "Loopback names stay accepted. Without this flag or --allowed-origin, a "
            "non-loopback --host accepts every Host and Origin."
        ),
    )
    http_group.add_argument(
        "--allowed-origin",
        dest="allowed_origins",
        action="append",
        default=[],
        metavar="ORIGIN",
        help=(
            "accept this Origin header for browser clients; repeatable, same port rule, "
            "for example https://docs.example.com. Requests with no Origin are always accepted."
        ),
    )

    subcommands = parser.add_subparsers(dest="command")
    sync_parser = subcommands.add_parser(
        "sync",
        help="build the documentation index",
        description=SYNC_DESCRIPTION,
        parents=[_log_level_option(argparse.SUPPRESS)],
    )
    sync_module.add_arguments(sync_parser)
    status_parser = subcommands.add_parser(
        "status", help="report the index on disk", description=STATUS_DESCRIPTION
    )
    status_parser.add_argument("--json", action="store_true", help="print one JSON object")
    return parser


def status(*, as_json: bool = False) -> int:
    """Describe the index on disk. Exit 0 fresh, 1 stale, 2 missing or unreadable."""
    from .core.tools import DocsService

    path = index_path()
    try:
        index = sync_module.load_index(path)
    except (FileNotFoundError, NotADirectoryError):
        print(f"No index at {path}. Run `akamai-cloud-docs-mcp sync`.", file=sys.stderr)
        return 2
    except (sync_module.SyncError, json.JSONDecodeError, OSError) as exc:
        print(f"Cannot read {path}: {exc}. Run `akamai-cloud-docs-mcp sync`.", file=sys.stderr)
        return 2

    service = DocsService(index)
    age = service.index_age_days()
    stale = age is not None and age > STALE_AFTER_DAYS
    docs = index.get("docs", [])
    guides = sum(1 for doc in docs if doc.get("kind") == "guide")
    report = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "built_at": service.built_at,
        "age_days": None if age is None else round(age, 1),
        "stale": stale,
        "guides": guides,
        "api": len(docs) - guides,
        "api_version": service.sources.get("api_version", ""),
    }
    if as_json:
        print(json.dumps(report))
    else:
        age_text = "unknown" if age is None else f"{age:.1f} days"
        print(f"path:        {report['path']}")
        print(f"size:        {report['size_bytes'] / (1024 * 1024):.1f} MB")
        print(f"built_at:    {report['built_at'] or 'unknown'}")
        print(f"age:         {age_text}{'  STALE' if stale else ''}")
        print(f"guides:      {report['guides']}")
        print(f"api:         {report['api']}")
        print(f"api_version: {report['api_version'] or 'unknown'}")
        if stale:
            print("Run `akamai-cloud-docs-mcp sync` to rebuild.")
    return 1 if stale else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command and args.http:
        # `--http sync` used to run sync and never start a server.
        parser.error(f"--http cannot be combined with the {args.command} subcommand")

    if args.command == "sync":
        if args.log_level:
            # sync prints its own progress; the level reaches any library that
            # logs during the crawl.
            logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)
        return sync_module.run(args)

    if args.command == "status":
        return status(as_json=args.json)

    if args.http:
        from .http import run as run_http

        try:
            path = normalize_path(args.path)
        except ValueError as exc:
            parser.error(str(exc))
        run_http(
            host=args.host,
            port=args.port,
            path=path,
            auto_sync=bool(args.auto_sync),
            allowed_hosts=args.allowed_hosts,
            allowed_origins=args.allowed_origins,
            log_level=args.log_level,
        )
        return 0

    from .stdio import run as run_stdio

    run_stdio(auto_sync=args.auto_sync is not False, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
