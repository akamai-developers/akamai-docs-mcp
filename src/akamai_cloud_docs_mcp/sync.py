"""Build the search index from live sources.

This is the only writer in the package. Everything else treats the index as
read-only. Progress goes to stderr because stdout is the stdio MCP transport.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    GUIDES_LLMS_URL,
    INDEX_SCHEMA,
    MIN_GUIDES,
    MIN_OPERATIONS,
    OPENAPI_URL,
    REQUEST_TIMEOUT,
    SYNC_CONCURRENCY,
    USER_AGENT,
    index_path,
)
from .core.api_catalog import ApiCatalogError, build_api_docs
from .core.catalog import (
    CatalogError,
    LinkEntry,
    fetch_guide,
    fetch_url,
    parse_llms_txt,
)
from .core.index import SearchIndex
from .core.tools import prebuilt_problem


class SyncError(Exception):
    """The index could not be built or pushed."""


class TransientError(SyncError):
    """A push request that never reached the app. Worth another attempt."""


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- guides ------------------------------------------------------------------


def fetch_guide_links(
    *, url: str = GUIDES_LLMS_URL, fetcher: Callable[[str], str] | None = None
) -> list[LinkEntry]:
    """Fetch and parse the guides llms.txt index."""
    entries = parse_llms_txt((fetcher or fetch_url)(url))
    if not entries:
        raise SyncError(f"no guide links parsed from {url}; the source format may have changed")
    return entries


def build_guide_docs(
    entries: list[LinkEntry],
    *,
    fetcher: Callable[[str], tuple[str, str, str]] | None = None,
    concurrency: int = SYNC_CONCURRENCY,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], list[str]]:
    """Fetch every guide page. Returns `(docs, failed_slugs)`."""
    fetcher = fetcher or fetch_guide
    docs: list[dict] = []
    failed: list[str] = []
    total = len(entries)

    def work(entry: LinkEntry) -> dict | None:
        try:
            body, updated_at, url = fetcher(entry.slug)
        except CatalogError as exc:
            _log(f"  skip {entry.slug}: {exc}")
            return None
        if not body.strip():
            _log(f"  skip {entry.slug}: empty body")
            return None
        return {
            "id": entry.slug,
            "kind": "guide",
            "title": entry.title,
            "url": url,
            "updated_at": updated_at,
            "text": body,
        }

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = zip(entries, pool.map(work, entries), strict=True)
        for done, (entry, result) in enumerate(results, start=1):
            if result is None:
                failed.append(entry.slug)
            else:
                docs.append(result)
            if on_progress:
                on_progress(done, total)
    return docs, failed


# --- index -------------------------------------------------------------------


def build_index(
    *,
    link_fetcher: Callable[[str], str] | None = None,
    guide_fetcher: Callable[[str], tuple[str, str, str]] | None = None,
    concurrency: int = SYNC_CONCURRENCY,
    min_guides: int = MIN_GUIDES,
    min_operations_guard: int = MIN_OPERATIONS,
    include_api: bool = True,
    quiet: bool = False,
) -> dict:
    """Build the full index document.

    Fails loudly when a source yields fewer pages than expected. A silent empty
    index is worse than a failed sync.
    """
    started = time.monotonic()
    # Resolved here rather than as default arguments so a caller or a test can
    # swap them by patching the module attribute.
    link_fetcher = link_fetcher or fetch_url
    guide_fetcher = guide_fetcher or fetch_guide

    def progress(done: int, total: int) -> None:
        if not quiet and (done % 50 == 0 or done == total):
            _log(f"  fetched {done}/{total} guides")

    if not quiet:
        _log(f"Fetching guide index from {GUIDES_LLMS_URL}")
    entries = fetch_guide_links(url=GUIDES_LLMS_URL, fetcher=link_fetcher)
    if not quiet:
        _log(f"  {len(entries)} guide pages listed")

    docs, failed = build_guide_docs(
        entries, fetcher=guide_fetcher, concurrency=concurrency, on_progress=progress
    )
    if len(docs) < min_guides:
        raise SyncError(
            f"only {len(docs)} guide pages parsed, expected at least {min_guides}. "
            "The source format may have changed; refusing to write a thin index."
        )
    if failed and not quiet:
        _log(f"  {len(failed)} pages failed and were skipped")

    sources = {
        "guides_llms": GUIDES_LLMS_URL,
        "openapi_url": OPENAPI_URL if include_api else "",
        "api_version": "",
    }

    if include_api:
        api_docs, api_version = _build_api_docs(quiet=quiet, min_operations=min_operations_guard)
        docs.extend(api_docs)
        sources["api_version"] = api_version

    # The search index is built here, once, and stored next to the documents.
    # A reader then loads it with one json.loads instead of tokenizing every
    # document, which matters on Akamai Functions, where that happens per
    # request. Measured on 843 documents: 0.27 s to build, 0.65 MB written.
    prebuilt = SearchIndex(docs).prebuilt()

    elapsed = time.monotonic() - started
    if not quiet:
        guide_count = sum(1 for doc in docs if doc["kind"] == "guide")
        api_count = len(docs) - guide_count
        _log(
            f"Built index: {guide_count} guides, {api_count} API operations, "
            f"{len(prebuilt['terms'])} search terms in {elapsed:.1f}s"
        )

    return {
        "schema": INDEX_SCHEMA,
        "built_at": _now(),
        "sources": sources,
        "docs": docs,
        "prebuilt": prebuilt,
    }


def _build_api_docs(
    *, quiet: bool = False, min_operations: int = MIN_OPERATIONS
) -> tuple[list[dict], str]:
    """Build API operation documents from the OpenAPI spec."""
    return build_api_docs(quiet=quiet, min_operations=min_operations)


def write_index(index: dict, path: Path | None = None) -> Path:
    """Write the index atomically so a reader never sees a half-written file."""
    target = Path(path) if path else index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(target)
    return target


def load_index(path: Path | None = None) -> dict:
    """Read a built index from disk."""
    target = Path(path) if path else index_path()
    with target.open(encoding="utf-8") as handle:
        index = json.load(handle)
    if not isinstance(index, dict) or "docs" not in index:
        raise SyncError(f"{target} is not a valid index file")
    return index


def sync(path: Path | None = None, *, quiet: bool = False, **kwargs) -> Path:
    """Build the index and write it to the cache directory."""
    index = build_index(quiet=quiet, **kwargs)
    target = write_index(index, path)
    if not quiet:
        size_mb = target.stat().st_size / (1024 * 1024)
        _log(f"Wrote {target} ({size_mb:.1f} MB)")
    return target


# --- push to Akamai Functions ------------------------------------------------

#: Raw bytes per chunk before base64. Each chunk lands in one Akamai Functions
#: key value store value, and the store caps a value at 1 MB. 900 KiB is
#: 921,600 bytes, under that cap whether it is read as 10^6 or 2^20 bytes. The
#: 10 MiB request limit is far away: a base64 chunk body is about 1.2 MB.
CHUNK_BYTES = 900 * 1024

#: Attempts per request. A dropped connection or a 5xx gets two more tries,
#: with a short pause between them. A 4xx is final on the first answer. The
#: worst case for one request is three timeouts of REQUEST_TIMEOUT (30 s) plus
#: 3 s of backoff, 93 s, before the operator sees the failure.
PUSH_ATTEMPTS = 3
PUSH_BACKOFF_SECONDS = 1.0


def _admin_url(app_url: str) -> str:
    """Validate an operator-supplied app URL and return its /admin/sync route."""
    from urllib.parse import urlsplit

    parts = urlsplit(app_url.rstrip("/"))
    if parts.scheme not in ("https", "http"):
        raise SyncError(f"app URL must be http or https, got {parts.scheme or 'none'!r}")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SyncError("plain http is only allowed for a local `spin up`, use https otherwise")
    if not parts.hostname:
        raise SyncError("app URL has no host")
    return f"{parts.scheme}://{parts.netloc}{parts.path}/admin/sync"


def _send_with_retry(
    send: Callable[[str, bytes, dict], tuple[int, str]],
    url: str,
    body: bytes,
    headers: dict,
    *,
    label: str,
    quiet: bool,
    sleep: Callable[[float], None],
) -> tuple[int, str]:
    """Send one request, retrying when the failure could be temporary.

    Retried: a connection that never got an answer, and any 5xx. Not retried:
    a 4xx, because the app has looked at the request and said no, and sending
    the same bytes again will not change its mind.
    """
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            status, text = send(url, body, headers)
        except TransientError as exc:
            if attempt == PUSH_ATTEMPTS:
                raise
            reason = str(exc)
        else:
            if status < 500 or attempt == PUSH_ATTEMPTS:
                return status, text
            reason = f"HTTP {status}"
        delay = PUSH_BACKOFF_SECONDS * attempt
        if not quiet:
            _log(f"  {label}: {reason}; retrying in {delay:.0f}s ({attempt}/{PUSH_ATTEMPTS})")
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def push_index(
    index: dict,
    app_url: str,
    token: str,
    *,
    chunk_bytes: int = CHUNK_BYTES,
    quiet: bool = False,
    poster: Callable[[str, bytes, dict], tuple[int, str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Upload the index in chunks, then publish it.

    Every request carries a random `generation`. The app stores the chunks
    under that generation and writes its meta key last, so a reader either
    sees the previous index or the new one, never a half-uploaded mixture, and
    two pushes at once cannot interleave. The meta also carries a sha256 of the
    whole payload so the app can refuse a spliced index.

    The payload is plain JSON rather than something compressed. The WebAssembly
    interpreter on Akamai Functions has no `zlib`, so the component on the other
    end could not decompress it.
    """
    import base64
    import hashlib
    import secrets

    url = _admin_url(app_url)
    if not token:
        raise SyncError("no sync token. Set the variable named by --token-env.")

    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode()
    chunks = [payload[start : start + chunk_bytes] for start in range(0, len(payload), chunk_bytes)]
    generation = secrets.token_hex(16)
    send = poster or _post_json
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }

    if not quiet:
        _log(f"Pushing {len(payload) / 1024:.0f} KB in {len(chunks)} chunk(s) to {url}")

    for number, blob in enumerate(chunks):
        body = json.dumps(
            {
                "generation": generation,
                "chunk": number,
                "data": base64.b64encode(blob).decode("ascii"),
            }
        ).encode()
        status, text = _send_with_retry(
            send, url, body, headers, label=f"chunk {number}", quiet=quiet, sleep=sleep
        )
        if status != 200:
            raise SyncError(f"chunk {number} rejected with HTTP {status}: {text[:200]}")
        if not quiet:
            _log(f"  chunk {number + 1}/{len(chunks)} ({len(blob)} bytes)")

    meta = {
        "generation": generation,
        "chunks": len(chunks),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "built_at": index.get("built_at", ""),
        "documents": len(index.get("docs", [])),
        "schema": index.get("schema", INDEX_SCHEMA),
        "encoding": "json",
    }
    body = json.dumps({"finish": meta}).encode()
    status, text = _send_with_retry(
        send, url, body, headers, label="publish", quiet=quiet, sleep=sleep
    )
    if status != 200:
        raise SyncError(f"publish rejected with HTTP {status}: {text[:200]}")
    if not quiet:
        _log(f"Published {meta['documents']} documents built at {meta['built_at']}")
    return meta


def _redirect_handler():
    """A redirect handler that refuses every redirect.

    The default one follows a 301, 302 or 303 by turning the POST into a GET
    and sends the Authorization header along to whatever host the redirect
    names. The token must only ever go to the URL the operator typed.
    """
    import urllib.error
    import urllib.request

    class RefuseRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    return RefuseRedirects()


def _post_json(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    """POST bytes and return `(status, text)`.

    An HTTP error status is returned, not raised. A redirect is a `SyncError`.
    No usable answer, whether the connection failed, timed out, or closed
    early, is a `TransientError`, which the caller may retry.
    """
    import http.client
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_redirect_handler())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SyncError(
                f"app URL redirected with HTTP {exc.code}, refusing to resend the token"
            ) from None
        return exc.code, exc.read(4096).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # urllib wraps only the send of the request in URLError. A socket
        # timeout while waiting for the status line comes out as a bare
        # TimeoutError, a peer that closes after reading the body as
        # RemoteDisconnected, a short body as IncompleteRead. None of them is
        # an answer from the app, so all of them are worth another attempt.
        # HTTPError is an OSError too, which is why its branch stays first.
        raise TransientError(f"could not reach {url}: {getattr(exc, 'reason', exc)}") from None


# --- CLI ---------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register `sync` flags on a parser."""
    parser.add_argument("--out", type=Path, default=None, help="index path (default: cache dir)")
    parser.add_argument("--guides-only", action="store_true", help="skip the API catalog")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    push = parser.add_mutually_exclusive_group()
    push.add_argument(
        "--push",
        metavar="APP_URL",
        default="",
        help="after building, upload the index to an Akamai Functions app",
    )
    push.add_argument(
        "--push-only",
        metavar="APP_URL",
        default="",
        help="upload the index already on disk without rebuilding it",
    )
    parser.add_argument(
        "--token-env",
        default="FUNCTIONS_SYNC_TOKEN",
        help="environment variable holding the push token (default: FUNCTIONS_SYNC_TOKEN)",
    )


def _sync_command(args: argparse.Namespace, app_url: str, flag: str) -> str:
    """A `sync` line that carries the --token-env and --out the operator gave.

    The --out path is made absolute. The line is meant to be pasted later,
    often from another directory after reading logs, and a relative path
    would then name a different file or none.
    """
    import shlex

    parts = ["akamai-cloud-docs-mcp", "sync", flag, app_url, "--token-env", args.token_env]
    if args.out:
        parts += ["--out", str(Path(args.out).resolve())]
    return " ".join(shlex.quote(part) for part in parts)


def resume_command(args: argparse.Namespace, app_url: str) -> str:
    """The exact command that pushes the index on disk without a rebuild."""
    return _sync_command(args, app_url, "--push-only")


def run(args: argparse.Namespace) -> int:
    """Execute a parsed `sync` invocation."""
    import os

    app_url = args.push_only or args.push
    try:
        if args.push_only:
            try:
                index = load_index(args.out)
            except (OSError, ValueError) as exc:
                # json.JSONDecodeError and UnicodeDecodeError are ValueErrors.
                # A half-written or hand-edited file deserves a message too.
                raise SyncError(f"cannot read the index to push: {exc}") from None
            # The app answers 503 to an index it cannot serve from its prebuilt
            # block. Say so here, before the upload, and name the rebuild.
            problem = prebuilt_problem(index)
            if problem:
                raise SyncError(
                    f"the index on disk cannot be pushed: {problem}. "
                    f"Run `{_sync_command(args, app_url, '--push')}` to rebuild it first."
                )
        else:
            index = build_index(quiet=args.quiet, include_api=not args.guides_only)
            target = write_index(index, args.out)
            if not args.quiet:
                _log(f"Wrote {target} ({target.stat().st_size / (1024 * 1024):.1f} MB)")
    except (SyncError, CatalogError, ApiCatalogError) as exc:
        _log(f"sync failed: {exc}")
        return 1

    if app_url:
        try:
            push_index(index, app_url, os.environ.get(args.token_env, ""), quiet=args.quiet)
        except SyncError as exc:
            # The index on disk is good. Say so, and give the command that
            # sends it again without another 45 second crawl.
            _log(f"sync failed: {exc}")
            _log(f"The index on disk is intact. To push it again: {resume_command(args, app_url)}")
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="akamai-cloud-docs-mcp sync", description="Build the documentation index."
    )
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
