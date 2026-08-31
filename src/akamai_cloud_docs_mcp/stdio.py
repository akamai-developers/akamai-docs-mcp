"""stdio transport, for a local MCP client that launches this as a subprocess.

When the cache is empty the index is built on a background thread from the
moment the process starts, so the client's first tool call gets a short
"still building" answer instead of blocking past its timeout. When the cache
is older than `STALE_AFTER_DAYS` the same kind of thread rebuilds it while the
old index keeps serving; the running server picks the new file up by mtime.
Nothing is ever written to stdout except the protocol itself; progress and
warnings go to stderr, which every client either shows or discards.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import sync
from .config import index_path
from .core import tools as core_tools
from .server import build_server

DEFAULT_LOG_LEVEL = "warning"

#: `built_at` is the second key the sync writes, well inside the first 4 KB.
#: Reading that much gives the age without parsing 3 MB of JSON.
_HEADER_BYTES = 4096
_BUILT_AT = re.compile(r'"built_at"\s*:\s*"([^"]*)"')

#: Seconds to wait before the second and third build attempts. A client that
#: launches this before the network is up needs a retry, not a restart.
BUILD_RETRY_DELAYS = (30, 60)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _build_index_in_background(*, sleep: Callable[[float], None] = time.sleep) -> None:
    """Build and install the service. Progress goes to stderr, failures too.

    Each attempt is one `load_service` call, which holds the service lock for
    the whole build, so tool calls answer "still building" or the last failure
    meanwhile. Each failure is one stderr line; the last one names the way out.
    A daemon thread, so a client that disconnects mid-build takes the process
    down with it. `write_index` is atomic, so that leaves no half-written file.
    """
    for delay in (*BUILD_RETRY_DELAYS, None):
        try:
            core_tools.load_service(auto_sync=True, quiet=False)
            return
        except Exception as exc:
            if delay is None:
                _log(
                    f"Building the index failed: {exc}. Run `akamai-cloud-docs-mcp sync` "
                    "in another shell, or restart the server."
                )
                return
            _log(f"Building the index failed: {exc}. Retrying in {delay} seconds.")
            sleep(delay)


def _refresh_index_in_background() -> None:
    """Rebuild a stale index while the old one keeps serving.

    `sync` writes the file atomically and `load_service` reloads on the next
    tool call once the mtime changes, so nothing here touches the service.
    A daemon thread, like the builder, for the same reason.
    """
    _log("Rebuilding the index in the background; the old one serves until it is done.")
    try:
        sync.sync(quiet=False)
    except Exception as exc:
        _log(f"Rebuilding the index failed: {exc}")


def staleness_line_from_file(path: Path) -> str:
    """The startup staleness sentence for an index on disk, without loading it."""
    try:
        with path.open("rb") as handle:
            header = handle.read(_HEADER_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""
    match = _BUILT_AT.search(header)
    if not match:
        return ""
    return core_tools.staleness_line(core_tools.DocsService({"built_at": match.group(1)}))


def run(*, auto_sync: bool = True, log_level: str | None = None) -> None:
    """Serve MCP over stdio until the client disconnects."""
    server = build_server(auto_sync=auto_sync, log_level=log_level or DEFAULT_LOG_LEVEL)
    path = index_path()
    if path.exists():
        stale = staleness_line_from_file(path)
        if stale:
            _log(stale)
            if auto_sync:
                threading.Thread(
                    target=_refresh_index_in_background, name="index-refresher", daemon=True
                ).start()
    elif auto_sync:
        threading.Thread(
            target=_build_index_in_background, name="index-builder", daemon=True
        ).start()
    server.run(transport="stdio")


if __name__ == "__main__":
    run()
