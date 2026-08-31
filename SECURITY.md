# Security policy

## Supported versions

The latest 0.x release. Older releases get no fixes; upgrade to the current one.

## Reporting a vulnerability

Use GitHub private vulnerability reporting:
https://github.com/labeveryday/akamai-cloud-docs-mcp/security/advisories/new

Do not open a public issue for a security problem. You will get an
acknowledgement within 3 business days and a fix, or a plan with a date,
within 14 days of the report.

## Scope

- The Python package: the stdio and HTTP transports, `sync`, and everything
  under `src/akamai_cloud_docs_mcp/`.
- The Akamai Functions handler in `functions/`, including the `/admin/sync`
  route and the bearer token `sync --push` sends to it.

Out of scope: techdocs.akamai.com and the Linode API themselves. Report those
to Akamai.

## What is enforced

**The server never fetches a URL a model composed freely.** A tool argument
names a document, never a destination. Slugs are resolved server-side into a
URL the server builds itself. A full URL is checked against a scheme, host,
and path allowlist before any request, which rejects a wrong host, plain http,
a credential-embedded host, a path outside the docs tree, path traversal, and
`file://`. A query string or percent-encoding in a guide URL is refused too.

**Redirects are re-checked.** A redirect target is attacker-controlled as far
as this process is concerned, so it has to clear the same bar. The allowed
hosts travel with each request rather than living on the shared opener, so the
docs rules apply to docs fetches and the wider GitHub rules apply only to the
spec download.

**Outbound requests** are https only, time out after 30 seconds, stop at 2 MB
per page, and send `akamai-cloud-docs-mcp/<version>` as the User-Agent. An
HTTP error is reported as a status code; the error body is never included in a
result.

**Input bounds.** `k` is clamped to 1-20, `id` and `section` to 256
characters, `query` to 1000.

**No tracebacks reach a tool result.** Every failure path returns a sentence
and, where it helps, the ids or sections the caller should try instead. A
missing index is reported as "No documentation index. Run
`akamai-cloud-docs-mcp sync`." and nothing else; no filesystem path reaches a
result.

**HTTP mode binds 127.0.0.1.** Put a reverse proxy in front to expose it, and
terminate TLS there. The SDK checks the Host and Origin headers only for a
loopback bind unless you pass `--allowed-host` or `--allowed-origin`; either
flag turns the checks on for every bind address with that list, and a
non-loopback `--host` without them prints a warning and accepts every Host. A
bare name matches a Host with no port and `name:*` matches any port, so give
both when the proxy forwards a port.

**`sync --push` refuses every redirect** and never resends the bearer token to
a different URL. The Functions `/admin/sync` route compares the token in
constant time, validates every field of every chunk, and answers 401, 400, or
413 without touching the store.

**Supply chain.** The whole dependency tree is locked in `uv.lock`
(`mcp>=2.0,<3`), CI installs from the lock, the Dockerfile installs from a
hashed export of the lock under `pip install --require-hashes`, and the
Functions build pins `spin-sdk` and `componentize-py` in
`functions/requirements.txt`. Dependabot opens a weekly pull request when
either drifts. GitHub Actions are pinned by commit SHA, since a tag can be
moved and a SHA cannot, and the release workflow checks the sha256 of the
`mcp-publisher` binary it downloads.

**Nothing but code is published.** `scripts/check_wheel.py` builds the wheel
and the sdist and fails if either contains an index, a fixture, a test path,
or any non-source file. It runs in CI on every push to `main` and every pull
request.

## What the tests already cover

`tests/test_security.py` pins the outbound rules (HTTPS only, one allowed host,
a 2 MB response cap, no response body in error text), the redirect allowlist,
model-supplied ids that never reach the network, input length caps, no
tracebacks in tool results, and the HTTP transport's loopback default and Host
and Origin validation. A fix for a report should add a case there.
