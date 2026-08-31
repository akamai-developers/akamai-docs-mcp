"""Stateless streamable HTTP transport, for self-hosting.

Stateless means no initialize handshake and no session header. Every request
carries everything it needs, so instances are interchangeable and a restart
costs a client nothing.

Binds 127.0.0.1 by default. Put a reverse proxy in front to expose it, and pass
the Host the proxy forwards as --allowed-host so the SDK's Host and Origin
checks stay on.

Routes:

    POST   /mcp       the MCP endpoint
    *      /mcp       405 with `Allow: POST` for every other method; this
                      server never opens a server-to-client stream and has
                      no session to end
    GET    /healthz   server name, version, document count, index age,
                      endpoint path
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from collections.abc import Sequence

import anyio
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import get_route_path

from . import __version__
from .config import index_path
from .core import tools as core_tools
from .server import SERVER_NAME, build_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"
DEFAULT_LOG_LEVEL = "info"

#: Bind addresses for which the SDK turns its own Host and Origin checks on.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: The SDK's own loopback allowlists. Kept when an operator adds hosts, so a
#: smoke test on the box itself keeps working. A DNS rebinding page cannot
#: make a browser send a loopback Host, so this widens nothing.
LOOPBACK_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
LOOPBACK_ALLOWED_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")

HEALTH_PATH = "/healthz"

#: Method-guard body. One line, so a probe that logs bodies stays readable.
_METHOD_NOT_ALLOWED_BODY = b"This MCP endpoint is stateless. Send POST.\n"


def _bind_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a bind of `host` lands on, or None for a name.

    `ipaddress` reads the dotted quad and nothing shorter. The C library,
    and so uvicorn, also binds the short and padded spellings: 127.1,
    127.000.000.001, 0x7f000001. `inet_aton` expands those the same way, so
    they classify by where they land. It also stops at a space and ignores
    what follows, so a host with one in it is refused first; nothing binds
    such a string anyway.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if not host or any(char.isspace() for char in host):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def is_loopback(host: str) -> bool:
    """True for a bind address that only the local machine can reach."""
    if host in LOOPBACK_HOSTS:
        return True
    address = _bind_address(host)
    return address is not None and address.is_loopback


def normalize_path(path: str) -> str:
    """The MCP route as the SDK and the method guard both see it.

    Starlette refuses a route with no leading slash, and a trailing slash
    would make the SDK answer POST on the unslashed path with a redirect
    while the guard treats both spellings as one.
    """
    if not path.startswith("/"):
        raise ValueError(f"the MCP path must start with a slash, got {path!r}")
    return path.rstrip("/") or "/"


def _bind_allowlists(host: str) -> tuple[list[str], list[str]]:
    """Host and Origin patterns for a loopback bind the SDK does not name.

    The SDK's lists cover 127.0.0.1, localhost and ::1. A client on a bind
    such as 127.0.0.2 sends `Host: 127.0.0.2:PORT`, which matches none of
    them, so the bound address goes in with the port wildcard, spelled the
    way the operator typed it: that spelling is what a client copies into
    its URL, and 127.1 is not the string 127.0.0.1.
    """
    if host in LOOPBACK_HOSTS or not is_loopback(host):
        return [], []
    bind = f"[{host}]" if ":" in host else host
    return [f"{bind}:*"], [f"http://{bind}:*"]


def transport_security(
    allowed_hosts: Sequence[str], allowed_origins: Sequence[str], *, host: str = DEFAULT_HOST
) -> TransportSecuritySettings | None:
    """Settings for the SDK's Host and Origin checks, or None to leave them off.

    The SDK turns the checks on by itself only for a bind of 127.0.0.1,
    localhost or ::1. Every loopback bind gets explicit settings here, so
    127.0.0.2, 127.1 or 0:0:0:0:0:0:0:1 is checked the same way and the
    bound address itself stays reachable. Any explicit list turns the checks on for
    every bind address, so a reverse proxy that forwards
    `Host: docs.example.com` is accepted and everything else is refused.
    None only for a non-loopback bind with no lists; build_app warns about
    that case.
    """
    if not allowed_hosts and not allowed_origins and not is_loopback(host):
        return None
    bind_hosts, bind_origins = _bind_allowlists(host)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*LOOPBACK_ALLOWED_HOSTS, *bind_hosts, *allowed_hosts],
        allowed_origins=[*LOOPBACK_ALLOWED_ORIGINS, *bind_origins, *allowed_origins],
    )


def _validation_warning(
    host: str, allowed_hosts: Sequence[str], allowed_origins: Sequence[str]
) -> str:
    """The startup line about Host validation on a non-loopback bind, or ""."""
    if is_loopback(host) or allowed_hosts:
        return ""
    if not allowed_origins:
        return (
            f"Host and Origin validation is off because --host {host} is not loopback. "
            "Pass --allowed-host with the name the proxy forwards to turn it on."
        )
    # An origin list alone turns the checks on, with only the loopback names
    # in the Host allowlist, so the proxy's own Host would be refused.
    return (
        "Host validation is on with loopback names only because --allowed-origin was "
        "given without --allowed-host, so a proxy forwarding any other Host gets 421. "
        "Pass --allowed-host with the name the proxy forwards."
    )


class MethodGuard:
    """ASGI wrapper answering every method but POST on the MCP path with 405.

    The SDK opens a server-to-client event stream on GET that never carries
    anything in stateless mode, so a probe or a browser tab holds a connection
    forever. A custom route cannot intercept this because the SDK registers its
    own route first, so the answer has to come before the app is reached. The
    other methods are answered here too, so every refusal carries the same
    `Allow: POST` instead of the SDK's `GET, POST, DELETE`.
    """

    def __init__(self, app, path: str):
        self.app = app
        self.path = path.rstrip("/") or "/"

    async def __call__(self, scope, receive, send):
        # get_route_path strips an ASGI root_path the way Starlette's router
        # does, so the guard and the SDK route agree under a proxy prefix.
        if (
            scope["type"] == "http"
            and scope["method"] != "POST"
            and (get_route_path(scope).rstrip("/") or "/") == self.path
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [
                        (b"allow", b"POST"),
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(_METHOD_NOT_ALLOWED_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _METHOD_NOT_ALLOWED_BODY})
            return
        await self.app(scope, receive, send)


def build_app(
    *,
    auto_sync: bool = False,
    host: str = DEFAULT_HOST,
    path: str = DEFAULT_PATH,
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
    log_level: str | None = None,
):
    """Build the ASGI app. Import target for an external ASGI server.

    The index is loaded here rather than on first request, so a missing index is
    a startup failure the operator sees immediately. `log_level` None leaves
    the host process's logging alone; only `run` picks a default.
    """
    path = normalize_path(path)
    if auto_sync and not index_path().exists():
        # Progress goes to stderr from here on; without this line the log
        # would stay blank for the whole build.
        print("No index found. Building it now, about 45 seconds.", file=sys.stderr)
    try:
        service = core_tools.load_service(auto_sync=auto_sync, quiet=not auto_sync)
    except (FileNotFoundError, NotADirectoryError):
        raise SystemExit(
            "No index found. Run `akamai-cloud-docs-mcp sync` before starting the "
            "HTTP server, or pass --auto-sync to build one now."
        ) from None
    print(
        f"Loaded {len(service.search_index)} documents built at {service.built_at or 'unknown'}",
        file=sys.stderr,
    )
    stale = core_tools.staleness_line(service)
    if stale:
        print(stale, file=sys.stderr)
    warning = _validation_warning(host, allowed_hosts, allowed_origins)
    if warning:
        print(warning, file=sys.stderr)

    server = build_server(auto_sync=auto_sync, log_level=log_level and log_level.lower())

    @server.custom_route(HEALTH_PATH, methods=["GET"])
    async def healthz(request: Request) -> Response:
        # Ask for the service on every probe rather than closing over the one
        # loaded at startup: load_service swaps in a rebuilt index by mtime,
        # and a monitor should see the new build time. It stats and may read
        # the file, so it runs in a worker thread like the tool bodies. A
        # reload that fails (a half-written file) keeps the last good service,
        # which is also what tools/call keeps serving.
        def current() -> core_tools.DocsService:
            try:
                return core_tools.load_service(auto_sync=False, quiet=True)
            except Exception:
                return core_tools.current_service() or service

        live = await anyio.to_thread.run_sync(current)
        age = live.index_age_days()
        return JSONResponse(
            {
                "name": SERVER_NAME,
                "version": __version__,
                "documents": len(live.search_index),
                "index_built_at": live.built_at,
                "index_age_days": None if age is None else round(age, 1),
                "endpoint": path,
            }
        )

    app = server.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
        host=host,
        transport_security=transport_security(allowed_hosts, allowed_origins, host=host),
    )
    return MethodGuard(app, path)


def run(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    auto_sync: bool = False,
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
    log_level: str | None = None,
) -> None:
    """Serve the stateless streamable HTTP app with uvicorn."""
    level = (log_level or DEFAULT_LOG_LEVEL).lower()
    app = build_app(
        auto_sync=auto_sync,
        host=host,
        path=path,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        log_level=level,
    )
    uvicorn.run(app, host=host, port=port, log_level=level)


if __name__ == "__main__":
    run()
