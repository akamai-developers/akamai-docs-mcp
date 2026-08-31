# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

Highlights:

- Every payload changed shape. `search_docs` returns `{id, title, snippet}`;
  a section, a small guide, and an API card come back as markdown under one
  header line, and an API card comes back alone with nothing around it; a
  table of contents is `{id, title, preamble, sections}` with no `hint`,
  `kind`, or `url`.
- The Akamai Functions handler answers a real `initialize`, so an MCP client
  can connect to a hosted endpoint for the first time. With the prebuilt
  search index in the pushed file, a call takes 0.13 s instead of 0.97 s. The
  index schema is 2: run `sync` once and push once after upgrading.
- A hosted instance exists. `https://ea3aad34-8539-405f-be73-55201fdec736.fwf.app/mcp`
  is a public preview on Akamai Functions: no auth, no SLA, index refreshed
  weekly. `claude mcp add --transport http akamai-cloud-docs <that URL>` and
  there is nothing to install.
- A new install and operations surface: `status`, `GET /healthz`,
  `--allowed-host` and `--allowed-origin`, `--log-level`, and
  `sync --push-only` with retries and an exact resume command after a failed
  push.
- Three payload cuts (a text table of contents, 160-character snippets, and
  one more sentence in the search description) were measured with the eval
  harness and not shipped. Each saved prompt tokens on gpt-5-mini and cost the
  7B model at least one fact on twelve questions.

Every result shape changed, so a client that parsed `kind` or `score` out of a
search result, or `hint` out of a table of contents, needs an update. The index
schema is 2 and the text inside it changed, so run `akamai-cloud-docs-mcp sync`
after upgrading. stdio and HTTP still load a schema 1 index; a deployed
Functions app answers 503 until it gets a push from this version.

### Added

- A public preview instance on Akamai Functions:
  `https://ea3aad34-8539-405f-be73-55201fdec736.fwf.app`, MCP endpoint `/mcp`,
  status page `/`. Deployed 2026-08-28 in the `devrel-demos` account with the
  index pushed the same day. It serves `initialize`, `tools/list`,
  `search_docs` and `fetch_doc` over the 843 document index (394 guides and
  449 API operations), with guide text fetched live from techdocs on every
  `fetch_doc`. No auth, no SLA, limits subject to change.
  `.github/workflows/sync-push.yml` rebuilds and pushes its index every Monday
  at 06:00 UTC. `server.json` lists it under `remotes` as a `streamable-http`
  transport, and the README's Getting Started opens with it.
- Both tools advertise a title (`Search Akamai Cloud docs`, `Read an Akamai Cloud
  doc`) and annotations (`readOnlyHint`, `idempotentHint` true, `destructiveHint`,
  `openWorldHint` false). Claude Code defaults a tool with no annotations to
  destructive, so every call asked for confirmation. Registration order is fixed:
  `search_docs` first.
- Every tool parameter carries a description and the string parameters carry the
  `maxLength` from `config.py`. The `k` range (1 to 20, clamped) is stated in
  its description rather than as `minimum`/`maximum`, so an out-of-range value
  still clamps on every transport instead of failing on one.
- `core.tools.render_result(payload)`: one renderer for every transport. A
  section, a small guide, and an API card go out as markdown under one header
  line (`# {title} > {section_title} [{id} #{section_id}] {url}`); search
  results, tables of contents, and errors stay compact JSON. The `note` and a
  `Truncated at 40,000 characters` line follow the header when set. Measured on
  the cached index: the `aiven-manage-database` resize section 982 to 885
  bytes, API cards 685 to 408 bytes mean over 449 (441 when the index is past
  7 days and the staleness note is appended), and no `\n` escapes in any
  markdown result.
- A short top-level section now ships inside the table of contents. When a
  top-level section has no children and its body is under 300 bytes, its entry
  carries the body as `content` (raw markdown) instead of a `summary`. A model
  that read the outline no longer spends a second `fetch_doc` call on a section
  smaller than the framing around it. Over the 392 cached guides, 214 of 1,738
  top-level entries carry `content`; 10 of those are table-only sections that
  previously had no summary at all. The outline grows by 66 bytes per guide on
  average (313,348 to 339,055 bytes across all guides, 8.2%), which is the cost
  of the removed round trip. Children still carry `id` and `title` only. A
  body of blank lines and spaces does not count as content.
  `INLINE_CONTENT_BYTES` in `core/sections.py` holds the threshold.
- `fetch_doc` accepts the API reference URL its own cards emit
  (`https://techdocs.akamai.com/linode-api/reference/<operationId>`), with a
  trailing slash, a fragment, a query string, or different letter case. All 449
  cached operations resolve back from their own `url`. An unknown reference
  URL gets `No API operation matches that reference URL.` with `did_you_mean`
  instead of a complaint about the `/cloud-computing/docs/` path. Mixed-case
  guide slugs resolve. A guide URL with a `#fragment`, one trailing `/`, or
  both resolves to its slug in `normalize_guide_id`; techdocs serves all three
  forms and a model copies them out of prose. A query string, percent-encoding,
  and a double slash are still rejected.
- `catalog.resolve_template_variables`: known techdocs `<<NAME>>` variables that
  the raw markdown endpoint serves unexpanded are replaced in
  `clean_guide_markdown`. `<<AKAMAI CLOUD>>` becomes `Akamai Cloud`,
  `<<CJAR_DASH_LONG>>` becomes a comma. Unknown names, such as `<< EOF >>` in a
  shell heredoc, are left as written. Measured on a live rebuild of 394 guides:
  2 unresolved variables before, 0 after.
- `core.tools.staleness_line(service)` for operators: empty when fresh,
  otherwise one sentence with the age and the `sync` command, for stderr or a
  status page. `core.tools.is_ready()` and `load_error()`, so a transport can
  answer a tool call while a background index build holds the service lock.
- `sync` stores the search index it builds in the index file under a
  `prebuilt` key: the sorted term list, per-term document positions and
  weights as parallel integer lists, per-document lengths and normalized
  lengths, and the average length. `SearchIndex.prebuilt()` writes it and
  `SearchIndex.from_prebuilt(docs, block)` loads it without tokenizing a
  document. On the 843 document index the block is 674,572 bytes.
- `core.tools.prebuilt_problem(index)`: the one rule for whether an index can
  be served from its block. `DocsService` builds at load when it cannot, so an
  index written by an older `sync` keeps working locally.
- `build_server(log_level=...)`: passes the level to the SDK and sets the root
  logger explicitly; below debug the `mcp.server.streamable_http` logger is
  held at WARNING, which removes the `Terminating session: None` line printed
  on every stateless request. `http.build_app()` leaves the root logger alone
  when `log_level` is None; only `run()` defaults to info, so an external ASGI
  server that imports the app keeps its own logging level.
- Separate thread pools for the two tools (`FETCH_LIMITER` 40, `SEARCH_LIMITER`
  8). A burst of 45 slow live fetches no longer delays a search; a test fires
  45 concurrent `fetch_doc` calls and asserts a concurrent search finishes
  under one second.
- The HTTP server picks up a rebuilt index without a restart. `load_service`
  re-reads the index when the file's modification time changes; `set_service`
  turns the check off, so injected services never stat the filesystem.
- stdio: when the cached index is older than `STALE_AFTER_DAYS` and auto-sync
  is on, a daemon thread runs `sync` once at startup while the old index keeps
  serving. One stderr line when it starts, one if it fails. The server picks
  the new file up by mtime on the next tool call.
- `--allowed-host` and `--allowed-origin` on `--http`, both repeatable. Passing
  either turns the SDK's Host and Origin checks on for every bind address with
  that list, so a reverse proxy that forwards `Host: docs.example.com` is
  accepted and everything else answers 421 or 403. Before, the checks were on
  only for a loopback bind, with a loopback-only allowlist, so the documented
  proxy setup answered 421 to everything and `--host 0.0.0.0` accepted any Host
  and Origin without a word. The SDK matches `name:*` by prefix and a bare
  `name` only against a portless Host, so pass both forms when the proxy
  forwards a port. Loopback names stay on the list so a local smoke test keeps
  working. A loopback bind the SDK does not recognise (`127.0.0.2`,
  `::ffff:127.0.0.1`) gets the same checks as `127.0.0.1`, with the bound
  address itself in the allowlist so local clients still connect; before, such
  a bind ran with the checks off and printed no warning. A non-loopback
  `--host` with no `--allowed-host` prints one stderr line: validation is off,
  or, with `--allowed-origin` alone, on for loopback Hosts only. The line and
  the `--allowed-host` help text both name the fix.
- `GET /healthz` on the HTTP transport, returning `name`, `version`,
  `documents`, `index_built_at`, `index_age_days` and `endpoint`. For probes
  and systemd; the Functions status page already had the equivalent.
- `--log-level {debug,info,warning,error}`. Defaults to `warning` over stdio
  and `info` with `--http`, where uvicorn's access lines are the point. It is
  accepted on either side of the `sync` subcommand and applied to the root
  logger for the sync run; `sync --log-level debug` used to exit 2.
- `status` subcommand, with `--json`. Prints the index path, size, build time,
  age in days, a STALE flag past 7 days, guide and API counts, and the API
  version. Exit 0 fresh, 1 stale, 2 missing or unreadable, so it can guard a
  systemd timer.
- `sync --push-only APP_URL` uploads the index already on disk without
  rebuilding it. A failed push now ends with the exact resume command, so a
  dropped connection on chunk 3 costs one retry instead of another 45 second
  crawl of the guide pages. `--push` and `--push-only` are mutually exclusive.
- `sync --push` retries a chunk or finish request up to 3 times, with a 1 s
  then 2 s pause, when the connection drops or the app answers 5xx. A 4xx
  stops the push on the first answer, since the app has looked at the body and
  sending it again will not change the verdict.
- The Akamai Functions handler answers a real `initialize`: it echoes a
  requested `protocolVersion` of 2024-11-05, 2025-03-26, 2025-06-18 or
  2025-11-25, answers 2025-11-25 to anything else, and returns
  `capabilities`, `serverInfo` and `instructions`. `ping` returns `{}`.
  Checked with the mcp 2.1.1 Python client against `spin up` in both `auto`
  and `legacy` modes: initialize, tools/list, search_docs, fetch_doc of an
  API card, a guide table of contents and one section all succeed. Before
  this, no MCP client could connect to the hosted endpoint.
- `tools/list` on Functions carries `title` and the read-only, idempotent
  `annotations`, built from the same literals in `core/tools.py` as the SDK
  server. A test asserts the two transports advertise identical name, title,
  description, input schema and annotations.
- On Functions, `GET` and `DELETE /mcp` answer 405 with `Allow: POST` and a
  one-line body. `OPTIONS /mcp` answers 204. Every `/mcp` response
  carries `access-control-allow-origin: *`, the allowed methods and headers,
  and exposes `mcp-protocol-version`, so a browser-based client can reach the
  hosted endpoint. `/admin/sync` sends no CORS headers, and `authorization`
  is not in the allowed list. The status page answers `GET` and `HEAD /`
  only, with no CORS headers.
- One JSON line per Functions tool call on stdout for `spin aka logs`:
  `{"tool", "ms", "ok"}`. `ms` covers the whole call, index load included.
  No query text and no ids are logged. The status page adds `index_age_days`,
  `stale`, `generation` and, once the index is past 7 days, the `staleness`
  line an operator should act on.
- The Functions guide fetch sets a 10 s bound on the connect, the first byte,
  and each wait for the next body chunk through wasi `RequestOptions`, below
  the 30 s handler cap. Verified by building once with every bound at 1 ns:
  the fetch failed at once and `fetch_doc` answered with the indexed copy.
- `examples/python/`: six client files, 39 to 62 lines each: a raw mcp 2.x
  client, Strands Agents, the OpenAI Agents SDK, the OpenAI Chat Completions
  API with the tool loop in plain `urllib` (the one that runs unchanged
  against vLLM through `OPENAI_BASE_URL`), LangChain through
  langchain-mcp-adapters, and the Claude API MCP connector. The README there
  explains the two-venv setup: Strands 1.54 and langchain-mcp-adapters 0.3 pin
  `mcp<2`, the server needs `mcp>=2`, and the wire protocol is the same. It
  makes `akamai-cloud-docs-mcp sync` an explicit step and describes the first
  run as it is: the server answers at once, the first tool call says "still
  building", and the build thread dies with the example; `mcp_client.py` waits
  for that build instead of exiting. The four stdio examples pass
  `AKAMAI_DOCS_MCP_CACHE_DIR` and `XDG_CACHE_HOME` through to the server,
  since MCP stdio clients start it with a minimal environment and a value
  exported in the shell never reached it. Client timeouts in the examples do
  not claim to cover the index build; the remaining read timeouts bound a live
  `fetch_doc`. `claude_api_connector.py` exits with a one-line message when
  `ANTHROPIC_API_KEY` is unset instead of a traceback.
  `examples/clients/codex.toml` and `examples/clients/gemini-settings.json`
  are ready-to-copy client files. Both start the server with
  `uvx --from git+<repo> akamai-cloud-docs-mcp`, so neither needs a path
  edited. `codex.toml` keeps the default `startup_timeout_sec`: the server
  answers `initialize` in about a second, and the index build never blocks
  it.
- `examples/deploy/`: systemd units (`akamai-cloud-docs-mcp.service`,
  `akamai-docs-sync.service`, `akamai-docs-sync.timer`) that keep the index in
  `/var/lib/akamai-cloud-docs-mcp` and restart the server after each weekly
  rebuild with `systemctl --no-block try-restart`; the blocking form deadlocked
  the oneshot in its start-post step and left the server stopped after every
  timer run. The server unit does not pull the sync unit in with `Wants=`,
  which re-ran the 45 second build on every restart and crash recovery. The
  unit header lists the commands: build the index once with
  `systemctl start akamai-docs-sync.service` before the first server start.
  A two-stage `Dockerfile` that runs the release gate in its builder stage,
  installs the runtime dependencies pinned and hashed from `uv.lock`
  (`uv export --frozen`, then `pip install --require-hashes`) and the wheel
  on its own with `--no-deps`, so the image carries the dependency tree CI
  tests, as a non-root user, plus `.dockerignore`. The HTTP run example
  publishes the port on the host's loopback (`-p 127.0.0.1:8000:8000`) and
  says to front it with a proxy and `--allowed-host` for anything wider. The
  stdio `docker run` example, in the Dockerfile and in the README, passes
  `--no-healthcheck`, as the file already said to. The README's systemd steps
  start from the clone into `/opt/akamai-cloud-docs-mcp`, `uv sync` there,
  copying the units, and `systemctl daemon-reload`.
- `server.json` for the MCP Registry: `io.github.labeveryday/akamai-cloud-docs-mcp`,
  one PyPI package, stdio, `AKAMAI_DOCS_MCP_CACHE_DIR` as an optional variable.
  Validates against the 2025-12-11 schema with zero errors. README.md carries
  the `mcp-name` marker the registry looks for in the PyPI description.
- `.github/workflows/release.yml`: on a `v*` tag, checks the tag against
  `__version__`, builds, gates both artifacts, publishes to PyPI with trusted
  publishing, lists the release in the MCP Registry with `mcp-publisher`
  1.8.1, and pushes `ghcr.io/labeveryday/akamai-cloud-docs-mcp`. The gate
  step runs `scripts/check_wheel.py` under `uv run --no-sync` so it uses the
  interpreter setup-uv installed. The `:latest` tag on `ghcr.io` moves only
  for a plain `x.y.z` tag; a pre-release tag such as `v0.3.0rc1` still gets
  its own version tag. The PyPI project and its trusted publisher do not exist
  yet; the workflow header lists the one-time setup. The registry job
  downloads `mcp-publisher` by version and checks its sha256 against the
  value recorded in the workflow before running it, since that job holds an
  OIDC token and a release asset can be replaced without its tag moving.
- `.github/workflows/sync-push.yml`: `sync --push` to the Functions app from a
  GitHub runner every Monday at 06:00 UTC, or on demand. Needs the `APP_URL`
  and `FUNCTIONS_SYNC_TOKEN` repository secrets. Until both exist, the first
  step prints a notice and the later steps are skipped, so the job ends green
  with nothing pushed instead of failing every Monday in a fork or a fresh
  repository.
- `.github/dependabot.yml` (uv at `/`, pip at `/functions`, GitHub Actions,
  weekly), `.github/CODEOWNERS`, a bug report template that asks for the
  client, transport, `--version`, `status` output and the failing call,
  `CONTRIBUTING.md`, and `SECURITY.md`.
- `scripts/rank_check.py`: 20 queries with their expected document ids, scored
  as MRR and top-1, split into guide-shaped and API-shaped. The BM25 constants
  are tuned against this and nothing else. Run it before and after any ranking
  change.
- `scripts/section_check.py`: walks every section of every guide and every API
  entry in the cached index and checks that a section id resolves to content,
  a table of contents entry points at a real section, and a flattened title
  never invents a word that is not in its own heading. Run it after touching
  `core/sections.py`.
- Tests. `tests/test_functions_app.py` covers `functions/app.py`, which had
  none; it installs a stand-in Spin runtime built to the spin-sdk 3.4.1
  signatures and loads the app by path. It grew from 28 to 131 tests over this
  release: the full handshake shape and version echo, every malformed-body
  case, the SDK client end to end over an in-process ASGI shim in both `auto`
  and `legacy` modes, parity of `tools/list` with the SDK server,
  generation-prefixed storage, a partial push serving the old index, a spliced
  payload and a bad meta answering 503 with one store read, 29 malformed admin
  inputs each answering 400 with the app still serving, 413 on an oversized
  chunk, 401 on a non-ASCII bearer, the 405 and CORS routes, the per-call log
  line, the fallback on a send failure, and the outbound timeout options.
  `tests/test_http.py` drives `build_app` through Starlette's test client: the
  legacy initialize, tools/list without a handshake, a notification (202), a
  batch (400), a 2026-07-28 tools/call with the `_meta` envelope, an
  unsupported version (400, -32022), a foreign Origin (403), a proxy Host with
  and without the flag (200 and 421), `/healthz`, and 405 on GET and DELETE.
  `tests/test_main.py` covers the parser, `status`, stdio startup, and launches
  the real entry point as a subprocess driven by the SDK's stdio client. Sync
  tests cover the operator command end to end: the token is read from the
  variable named by `--token-env` and never printed, a missing token is exit 1
  with "no sync token" on stderr, `--guides-only` skips the API catalog, a
  build failure is exit 1 with "sync failed:", `_post_json` returns
  `(status, body)` on an HTTP error and raises "could not reach" on a
  connection failure. `sync.py` coverage went from 69% to 94%.

### Changed

- `search_docs` results are `{id, title, snippet}`. `kind` restated the id
  shape (every API id starts with an HTTP verb and a path) and `score` restated
  the order. Mean result list at k=5 over five rank-check queries: 1,764 to
  1,613 bytes. `SearchIndex.search` still returns both fields for the rank
  checks.
- API search snippets no longer start with the operation's own id and title.
  `build_search_text` puts `METHOD /path Summary ` at the front of every API
  card's ranking text, and `SearchIndex.search` cut its snippet from that
  text, so every API result repeated two fields it already carried. All 449
  cached operations match that exact prefix; it is removed, and any card
  built another way is used as is. Ranking is untouched because the index is
  built from the full text. Measured on the 20 `rank_check.py` queries at
  k=5: 2,014 of 6,350 API snippet bytes gone, about 61 bytes per API result.
- A table of contents is `{id, title, preamble, sections}`. The `hint` repeated
  the tool descriptions, `kind` is implied by the id, and the `url` comes with
  the section. `aiven-manage-database`: 2,607 to 2,423 bytes.
- A bad-section error lists top-level sections only and says "Pick a section
  id from valid_sections. Omit section to see subsections too." On the
  40-section Lish guide the error is 6 entries and 433 bytes instead of the
  whole outline.
- The staleness note is gone from guide results, whose text is fetched live.
  An API card past 7 days carries `From an index built N days ago.`; the
  advice to run `sync` goes to operators through `staleness_line`.
- Server instructions moved to `core.tools.INSTRUCTIONS` so every transport
  serves the same text, and now say when to search, when to read, to read one
  section at a time, and to pass ids verbatim.
- Tool descriptions: the search one names the new result shape, the fetch one
  names the rendered forms and says a table of contents entry carries either a
  summary or, for a short section, its whole text, so a model does not fetch a
  section whose entry already has content. The list of accepted id forms moved
  into the `id` parameter description. The advertised `tools/list` is 2,196
  bytes on the wire (compact JSON of the `tools` array from a `tools/list`
  call over HTTP), up from 1,649 measured the same way against a 0.1.0
  server: about 280 of that is the two titles and annotation objects, the
  rest is parameter descriptions and bounds, less the pydantic titles that
  were removed.
- Search scales a document's BM25 length normalization by how repetitive it is
  compared to other documents of its kind. Reference pages built from hundreds
  of table rows over a few hundred words were matching queries on term overlap
  alone. `configuration-audit-log-events` is 237 rows over a 310 word
  vocabulary, and it was the top hit for "create postgres cluster" and the
  second hit for "resize database cluster", neither of which it answers.
  - Measured on the 20 queries in `scripts/rank_check.py`: API mean reciprocal
    rank rose from 0.703 to 0.758 and top-1 from 5/10 to 6/10, with guide
    results unchanged at 0.900 and 9/10. No query ranked worse.
  - Comparison is within a kind. The 449 API cards are generated and terse, so
    measuring hand-written prose against them tilts every guide down for a
    reason that has nothing to do with repetition. Using one corpus-wide median
    scored better on this query set, by 0.05 API MRR, but that gain came from
    the tilt and would move as the ratio of guides to operations changes.
  - An index built by an earlier version still loads over stdio and HTTP. The
    penalty is computed when the search index is built, about 10ms over 841
    documents, and a schema 2 index carries the result in its prebuilt block.
- The `Sub pages` navigation list at the end of a guide is stripped along with
  `Sibling pages`. It was the last heading in 60 of 392 indexed guides, every
  line under it a link to a page that is already indexed, and each one was a
  spurious "Sub pages" entry in the table of contents. The cut lands at the
  first navigation heading that has nothing but link lines and further
  headings over link lines after it, which also removes the `What's next`
  list techdocs places after `Sibling pages` on `cli-1`. A navigation heading
  with prose directly under it is content and stays whole. When no navigation
  heading has an all-navigation tail, each link-only `Sub pages` or
  `Sibling pages` block is removed on its own and the rest of the page stays,
  so a prose section that follows a navigation heading is never deleted. No
  live guide is affected by that case.
- readme.io block JSON is converted to markdown when a guide is cleaned. 47
  guides carried 91 blocks: 37 `[block:parameters]` tables as JSON cell maps
  keyed `h-0` and `0-2`, 48 `[block:image]` figures as URL, alignment and
  sizing, and 6 `[block:html]` snippets as an escaped string. All of it was
  indexed and served raw, and 9 top-level TOC summaries read
  `[block:parameters]`.
  - A parameters block becomes a pipe table. `<br>` runs and newlines inside
    a cell become ` / `, literal pipes are escaped, and the cell keys decide
    the table's shape, not the `cols` and `rows` fields.
  - An image block becomes its caption, or its alt text, on one line. A
    screenshot with neither renders as nothing.
  - An html block becomes the html it wrapped, unescaped.
  - Both block layouts techdocs emits are handled: pretty-printed across lines
    and squeezed onto one line. A block that does not parse, or a kind this
    does not know, stays exactly as it was.
  - Re-cleaning the stored texts of the 2026-08-13 index removes 2.8% of
    guide characters across both changes (2,703,663 to 2,627,752). A fresh
    live build has 0 `[block:` hits, 0 guides ending in `Sub pages`, and
    1,681 top-level TOC entries against 1,738 (on 394 guides, two more than
    the old index). `scripts/section_check.py` is clean. `scripts/rank_check.py`
    guide MRR is unchanged at 0.900 (top-1 9/10); API MRR moved from 0.758 to
    0.767 with "create object storage bucket" going from rank 4 to 3, and
    top-3 from 9/10 to 10/10.
  - The stored index must be rebuilt with `akamai-cloud-docs-mcp sync` for
    any of this to reach a caller.
- stdio builds a missing index on a background thread from the moment the
  process starts, with progress on stderr, instead of inside the first tool
  call. The first call used to block 41 to 45 seconds, most of the 60 second
  budget of Claude Desktop and Cursor and far past the 5 second default of the
  OpenAI Agents SDK. The thread retries after a failure, waiting 30 s and then 60 s
  before the second and third attempts. Each failure is one stderr line; the
  last one names `akamai-cloud-docs-mcp sync`. Before, one failed attempt left
  the process without an index until restart.
- `--http --auto-sync` with no index prints "No index found. Building it now,
  about 45 seconds." and then the sync progress on stderr. Before, the log
  stayed blank for the whole build.
- stdio and HTTP print one stderr line at startup when the index is older than
  7 days, naming the sync command. stdio reads `built_at` from the first 4 KB
  of the file rather than loading 3.7 MB of JSON to find out.
- `sync --help` describes what sync fetches and writes. HTTP-only flags sit
  under their own "HTTP mode (with --http)" heading in `--help`.
- Index schema is 2. The index file grew from 3,216,836 to 3,891,400 bytes
  and pushes as 5 chunks instead of 4. The shared cache and every deployed
  Functions app need one `akamai-cloud-docs-mcp sync` and one
  `sync --push <app-url>` from this version; until that push the app answers
  503 on `tools/call` and `GET /`.
- Akamai Functions loads only the prebuilt block. Measured under `spin up`,
  wall time per call: `search_docs` 0.969 s to 0.130 s, `fetch_doc` of an API
  card 0.955 s to 0.108 s, `GET /` 0.947 s to 0.109 s; `tools/list` stays at
  3 ms. Natively the same load path went from 283 ms to 41 ms, and peak traced
  memory from 21.8 MiB to 28.5 MiB, inside the 128 MiB cap. Search results are
  unchanged: 876 queries return identical lists from both paths, and
  `scripts/rank_check.py` still reads guide MRR 0.900, API MRR 0.767.
- An index without the block, or under another schema number, is refused by
  the Functions app with a 503 that names the sync to run, and by
  `sync --push-only` before any upload, with the `sync --push` command that
  rebuilds it.
- Postings inside `SearchIndex` are two parallel lists with whole number
  weights instead of a list of `(position, float)` tuples. Scores are
  unchanged; this is what lets the JSON block be used as it comes.
- Push wire protocol is version 2. Every chunk body carries a random 32 hex
  character `generation` alongside `chunk` and `data`, and the finish body
  carries the same `generation` plus a `sha256` of the whole payload next to
  `chunks` and `bytes`. The app stores chunks under the generation and writes
  its meta key last, so a partial or interrupted push cannot mix into the
  index a reader sees, and two pushes at once cannot interleave.
- The Functions key value store layout is generation-prefixed. Chunks live at
  `index:<generation>:chunk:N` and `index:meta` names the generation, the
  chunk count, the byte count and a sha256 of the whole payload. `finish`
  verifies every chunk is present, the lengths add up, and the sha256 matches
  before it writes meta, then deletes the chunks of the generation the
  previous meta named, plus any pre-generation `index:chunk:N` key, using
  `get_keys()`. A partial push leaves the previous index serving; verified
  against `spin up` by sending one chunk of a new generation and reading the
  old generation back from `GET /`. The loader reads only the published
  generation, checks length and sha256, and answers 503 on any mismatch
  instead of serving a spliced index.
- The Functions `handle_rpc` refuses malformed input in order: bytes that are
  not JSON, an integer too long to parse, or nesting too deep are 400 with
  `-32700`; a batch or any body that is not one object is 400 with `-32600`; a
  notification is 202 with no body; `params` or `arguments` that are not
  objects are `-32602` with the request id echoed. `k: Infinity` and
  `k: 1e400` clamp. None of these are a 500 any more.
- `/admin/sync` validates everything and never answers 500 for a client
  mistake, since the sync client retries a 5xx three times: a body that is not
  an object, a chunk number that is not an integer in 0..63 or is a bool, a
  generation that is not 32 hex characters, base64 that fails strict
  decoding, or a finish with the wrong field types are 400; a decoded chunk
  over 1,000,000 bytes is 413; a missing, wrong, or non-ASCII bearer token is
  401. The status route and the admin route now sit behind the same guard as
  `/mcp`: a surprise is a 500 naming the exception class, never a trap.
- The Functions `call_tool` renders results with `core_tools.render_result`,
  so a section, a small guide and an API card go out as markdown under one
  header line on Functions exactly as they do over stdio and HTTP. A failure
  inside the Spin `http.send` call becomes a `FetchError` naming only the
  wasi error code the runtime returned, never a host or address, and
  `fetch_doc` falls back to the indexed copy with a note instead of answering
  500.
- The Functions `PROTOCOL_VERSION` is 2025-11-25. The `mcp-protocol-version`
  header and the status page follow it. The handler docstring and
  `functions/README.md` say which era this transport speaks; the stdio and
  HTTP transports also speak 2026-07-28. `functions/spin.toml` application
  version is 0.2.0.
- `functions/README.md`: the app URL is `https://<uuid>.fwf.app`, an id
  assigned at deploy time that persists across updates; the chunk size is
  explained by the 1 MB key value store value cap; the token is generated
  with `read -rs` so it never lands in shell history; the deploy runbook,
  the resume path after a failed push, the route table, the two 7-day usage
  commands, the bundle size (about 38 MiB of 50 MiB), and the measured
  per-call latency under `spin up` (search_docs median 0.130 s, API card
  fetch_doc median 0.108 s, tools/list 3 ms, from 0.969 s and 0.955 s before
  the prebuilt block) are written down. The file is ordered runbook first
  (prerequisites, build, run locally, deploy, push, operate, teardown with
  `spin aka app delete`) and internals after, and every `sync` command says
  it runs from the repository root in the project venv, since
  `functions/.venv` is the toolchain venv and has no `akamai-cloud-docs-mcp`
  command. The README's Functions block runs `spin` in a subshell for the
  same reason.
- The version lives only in `src/akamai_cloud_docs_mcp/__init__.py`;
  `pyproject.toml` reads it through hatch (`dynamic = ["version"]`).
  `__version__` is 0.2.0. CI checks that `functions/spin.toml` and
  `server.json` say the same.
- CI installs from `uv.lock` on every job (`uv sync --locked`), runs ruff and
  pytest through `uv run`, and compiles `examples/python/*.py`. `mcp` is
  `>=2.0,<3` in `pyproject.toml`; the lock pins the tree.
- `scripts/check_wheel.py` gates the sdist as well as the wheel, with the same
  forbidden names, path fragments and size caps. The sdist holds `src/`,
  README, CHANGELOG, LICENSE and the project metadata, about 97 KB against
  the 200 KB cap (the wheel is about 74 KB; both measured 2026-08-28).
- `.gitignore` adds `.coverage` and `examples/**/.venv/`.
- The eval harness reads its tool schemas from the `core.tools` literals
  instead of a hand-written copy, and `run_tool` returns
  `core_tools.render_result(payload)`, the same text every transport sends,
  instead of a JSON dump of the payload. It runs six conditions instead of
  three: `toc-text`, `snippet-160` and `medium` are applied inside the
  harness (a text renderer for tables of contents, a rebinding of
  `core.index.snippet` to a 160 character limit, one sentence appended to
  `SEARCH_DESCRIPTION`), so each variant was measured without changing
  anything in `src/`. `raw_results.jsonl` records every `fetch_doc` call's
  `id`, `section` and resolved id, plus per-question counts of section
  fetches and tables of contents fetched, with the bytes of each table of
  contents in both the JSON and the text form. `RESULTS.md` gains a "Section
  fetches" column and one decision paragraph per variant, generated from the
  run. `--toc-bytes` prints the JSON against text size of every indexed guide
  that takes the table-of-contents path, without a model. `--raw PATH` sets
  where the raw records go, so two providers can run as two processes and be
  concatenated.
- `eval/RESULTS.md` and `eval/raw_results.jsonl` were re-run on 2026-08-28
  against the wire text and a rebuilt index (840 documents). The 2026-08-13
  numbers were measured against JSON payloads and are no longer comparable.
  `--from-raw` reproduces the file byte for byte. Under `tight`, gpt-5-mini
  scored 100% facts, 92% doc hit and 7,232 prompt tokens per question; the 7B
  model 88% facts, 58% doc hit and 3,256 prompt tokens. The three variants
  were measured and none ships:
  - `toc-text` cuts a table of contents by 25% (1,519 to 1,143 bytes over
    the 25 fetched; 1,465 to 1,124 over the 103 TOC-path guides in the
    index). gpt-5-mini held at 100% guide facts with 828 fewer prompt tokens;
    the 7B model lost one guide fact (90% to 80%) on a question where it
    fetched the wrong document under both conditions.
  - `snippet-160` raised the 7B model's doc hit from 58% to 75% and dropped
    its facts from 88% to 80%, two facts, both on questions where it picked a
    different document. gpt-5-mini held at 100% with 610 fewer prompt tokens.
    Inconclusive on twelve questions.
  - `medium` had nothing left to fix: the two questions it targeted already
    pass under `tight` in this run, and it cost the 7B model one fact. On
    gpt-5-mini it cut prompt tokens 30% (7,232 to 5,027) at 100% facts and
    100% doc hit, which is worth a follow-up with more questions. It would
    also push the two descriptions to 1,246 characters, past the 1,200 cap
    in `tests/test_server.py`.
- README.md: the developer path comes first (`uv tool install` from git, then
  `sync`, then `claude mcp add`), with a check step (`claude mcp list`, then
  one question that runs both tools) and a teardown block; the client blocks
  name the key each client reads; every tool example was regenerated from the
  running server and the byte table was re-measured on 2026-08-28; the
  architecture diagram and the index internals moved under Development; the
  security section is five rules plus the public-instance paragraph, and
  `SECURITY.md` lists everything the tests enforce under "What is enforced".
  One duration and one size everywhere: about 45 seconds for `sync`, about
  3.7 MB (3,891,400 bytes on 2026-08-28) for the index, which is what
  `status` prints. `CONTRIBUTING.md` points at the README for the commands
  and names the `## [0.2.0] - Unreleased` heading for new entries.

### Fixed

- Text hidden inside an HTML comment is no longer indexed or served. One
  guide, `protect-data-with-object-lock`, opened a `<!--` at 90% of the page
  and never closed it, so two whole sections that are invisible on the
  rendered page came back from `fetch_doc` and ranked the page first for
  "audit logging configuration object lock". Another, `local-disk-encryption`,
  carried a closed comment that contradicted the visible sentence next to it.
  - `clean_guide_markdown` now drops closed `<!-- ... -->` spans and
    everything after a `<!--` that never closes. A comment inside a fenced
    code block is left alone, so a sample that shows one survives. A comment
    that opens outside a fence runs to the next `-->` wherever it is, which is
    how a markdown renderer reads it too.
  - The object lock page went from 8,108 to 6,615 characters, and its table of
    contents lost the two phantom sections.
- The guide cleaner handles CRLF input. `strip_sibling_pages` matched on `\n`
  only, so a page served with Windows line endings kept its navigation tail
  and its frontmatter date. Line endings are normalized once, first, and every
  pattern after that assumes `\n`.
- The live-fetch fallback now fires for every read error, not only
  `CatalogError`. `fetch_url` caught HTTPError, URLError and TimeoutError
  only, so a connection reset or a short read during `response.read()`, or a
  `Content-Type` charset the codec registry does not know, escaped as a raw
  exception and the "indexed copy is better than no answer" fallback never
  ran. `OSError`, `http.client.HTTPException` and `LookupError` now map to
  "<url> read failed: ...", the decode sits inside the try, and `fetch_doc`
  returns the indexed copy with the note "Live fetch failed (...); this is the
  indexed copy." The quoted reason is the first line of the message, capped at
  120 characters. No message carries a response body.
- A tool call before the index exists no longer blocks for the build or leaks
  the cache path. With auto-sync on and no index file the call returns "The
  documentation index is still building (about 45 seconds on first run).
  Retry in 30 seconds." A failed background build is reported, not retried
  inside the call; the "Building the index failed" result names the recovery
  step: run `akamai-cloud-docs-mcp sync` in another shell, or restart the
  server, then retry. A missing, truncated, or wrong-shape index file maps to
  one sentence each with no filesystem path in it.
- `load_service` records a failure inside `DocsService(...)` in
  `load_error()`, so the stdio transport reports a damaged index file instead
  of answering "still building" until the client gives up.
- `search_docs(k=Infinity)` clamps to the default instead of raising
  `OverflowError`.
- `staleness_note` on API cards no longer claims the text was fetched live.
- Searching a document's own id now returns that document first for all 841
  cached documents. It used to for 610, and 9 ids were absent from the top
  20. `GET /linode/instances` lost to `GET /linode/instances/{linodeId}`
  because "Get a Linode" carries "get" at title weight and "List Linodes"
  does not, and slugs such as `iam-aclp` and `cli-1` were not indexed at
  all, since only the title, headings and text were.
  - Id tokens are now indexed at title weight, so a slug word is searchable.
  - A query equal to a document's id multiplies its score by
    `_ID_EXACT_BOOST = 3`, applied after and independently of the title
    boost. 1.8 (the title boost) leaves 2 ids off rank 1; 3 leaves none.
  - `scripts/rank_check.py` is unchanged before and after (guide MRR 0.900,
    API MRR 0.758) and title-as-query rank 1 stays at 824/841.
- `suggest()` no longer spends half a second of CPU on an unknown
  256-character id. `fetch_doc` calls it for every id it does not know, on
  an endpoint with no auth, and it ran the full `SequenceMatcher.ratio()`
  twice for each of 841 documents. The two cheap upper bounds,
  `real_quick_ratio()` and `quick_ratio()`, are checked first at the same
  0.3 cutoff, so a pair that cannot pass the cutoff skips the alignment.
  Suggestions are identical to before on 25 probes, including short typos.
  Measured on the cached index: `'a-'*128` 403 ms to 15 ms, a repeated
  alphabet 446 ms to 43 ms, `'linode instance '` repeated 480 ms to 23 ms,
  `'x'*256` 82 ms to 15 ms. The needle is not truncated: that changes the
  results and raises the gated worst case.
- Section titles, summaries, snippets, and API card descriptions keep the
  underscores in an identifier. `flatten_markdown` unwrapped inline code first
  and then ran one emphasis pass that treated `_` as a delimiter anywhere, so a
  heading written `` ## `thread_pool_size` `` came back as `threadpoolsize`.
  A caller reading a table of contents got a config key that does not exist.
  - Code spans are now set aside while the emphasis and HTML passes run, then
    restored. A code span is literal Markdown, so nothing after it may rewrite
    its text.
  - Underscore emphasis has to match CommonMark and cannot be intraword.
    `foo_bar_baz` stays whole, `_em_` still flattens to `em`, and `_foo_bar_`
    still flattens to `foo_bar`. Asterisks keep their old greedy behavior.
  - Found by walking all 2,709 sections of the 392 live guides and comparing
    each parsed title against its own heading. Three live section titles were
    wrong: `chunk_store_config`, `entity_ids and entity_regions`, and
    `thread_pool_size`, all in the Grafana Loki and OpenTelemetry collector
    pages, which are exactly the pages where a key is copied by hand.
  - One API card gained back a value it had been dropping. The `k8s_version`
    field of `POST /lke/clusters` documents its format as `` `<major>.<minor>` ``,
    and the HTML tag pass had been deleting both placeholders, leaving "in the
    format of .". Card sizes are unchanged at 181 to 1,077 bytes.
  - Ranking is untouched. Guide text is scored raw, and the API search text was
    byte-identical for all 449 operations, so `scripts/rank_check.py` still
    reports 0.900 guide and 0.758 API mean reciprocal rank.
- Numbered step headings keep their number. `flatten_markdown` is now an
  inline pass (`flatten_inline`: images, links, autolinks, code spans,
  emphasis, html) followed by a line-prefix pass (quote and list markers), and
  heading titles get only the inline pass. "1. Create and share the Shared IP
  address" was reaching callers as "Create and share the Shared IP address".
  11 live section titles across three guides change.
- A `#` inside a heading title survives. The closing-sequence rule now matches
  CommonMark: only `#` characters after a space at the end of the line are
  dropped, so "Using C#" stays "Using C#" and "Title ##" still becomes "Title".
- `<placeholder>` values and `a < b ... c > d` spans survive flattening. The
  html tag strip is an allowlist of the tag names the docs use (br, td, tr, b,
  strong, img, div, span, table, sup, AkamaiTabs and the rest) plus html
  comments, instead of any `<...>` span. `<https://...>` and `<user@host>`
  autolinks flatten to their address. Over the corpus, 4 summaries and 1
  snippet change; two of them exposed the unresolved techdocs variables that
  `resolve_template_variables` now handles.
- A code fence opened at 3 spaces and closed at 4, which is valid inside a list
  item, no longer hides every later heading. Fences match at any indentation
  and pair by marker character. The section splitter and the content iterator
  now share one fence tracker (`_iter_prose_lines`) so they cannot disagree.
  No live guide changed section count (2,709 before and after); 238 fences in
  the corpus sit at 4 or more spaces and are now recognized as code for
  summaries and snippets. After all the parser changes, `scripts/section_check.py`
  exits 0 and `scripts/rank_check.py` is unchanged at guide MRR 0.900, API
  MRR 0.758.
- A fenced code block that opens on the same line as its list marker
  (```` - ```python ````) is a fence in both the section parser and the HTML
  comment stripper. Before, its closer opened a phantom fence and every later
  heading on the page vanished. No live guide has this shape today.
- A line that starts with an inline triple-backtick code span
  (```` ```foo``` is a tag ````) is no longer read as a fence opener. Tilde
  fences keep the plain rule.
- Backslash escapes are removed from heading titles and flattened prose outside
  code spans. The section title `Autoconfigure mod\_status popup` on
  `capture-apache-metrics-with-linode-longview` now reads
  `Autoconfigure mod_status popup` in the table of contents. This is the only
  live title that changes.
- Docstrings and comments in `core/sections.py` and `core/catalog.py` match
  the code: `_iter_content_lines` passes table rows through, `fetch_url` also
  serves sync for llms.txt and the OpenAPI spec, only guide pages and the
  guides llms.txt carry the index boilerplate line, and the template-variable
  comment no longer quotes a documentation sentence.
- A guide whose page title is a heading at the same level as its sections no
  longer produces an empty section 1. Many techdocs pages use `#` for both the
  title and every section, so depth alone could not tell them apart, and the
  title became a section with no body. A model that followed the table of
  contents and asked for section 1 spent a tool call to receive a heading and
  nothing else.
  - A first heading is now read as the title when nothing at all sits between it
    and the next heading of its own level. A heading with prose under it, or one
    whose content lives in a subsection, is still a section.
  - 63 of 392 live guides carried such a section. 11 of them are large enough to
    return a table of contents, so the empty entry was visible to a caller,
    including `getting-started-with-the-linode-cli`, `aiven-postgresql`, and
    `aiven-mysql`. Section 1 of the CLI guide went from a bare heading to the
    5,701 byte "Install the CLI" section.
  - Checked across all 392 guides: no empty section remains, no guide loses
    content, and every promoted title still matches the curated llms.txt title.
    Search ranking is untouched, since the index scores raw markdown and never
    calls the section parser.
- `is_valid_slug` rejects a slug with a trailing newline. The pattern ended in
  `$`, which also matches just before a final newline, so `"widget\n"` passed.
  It ends in `\Z` now. Not reachable from a tool call, because `normalize_guide_id`
  strips its input first, but this is the function every other path trusts.
- Every method other than POST on the MCP path answers 405 with `Allow: POST`
  before the SDK route sees it. `GET /mcp` used to open a server-to-client
  event stream that never carries anything in stateless mode, so a probe or a
  browser tab held a connection forever, and HEAD and the other non-GET,
  non-DELETE methods fell through to the SDK, whose answer advertised GET. The
  guard matches the MCP path under an ASGI `root_path`, so a GET through a
  proxy prefix returns 405 instead of holding the SDK's stream open.
- `--http sync` is rejected with exit 2. It used to run `sync` and never start
  a server.
- `--path` without a leading slash is a usage error instead of a Starlette
  traceback. A trailing slash is dropped, so POST on the unslashed path no
  longer redirects and `/healthz` reports the path clients use.
- `--auto-sync` and `--no-auto-sync` help text mentions the stale-index rebuild
  stdio does, the `/healthz` route description in `http.py` names the fields
  it returns, and the index build time is quoted as 45 seconds everywhere in
  the transport files.
- `sync --push` refuses every redirect. urllib's default opener follows a
  301, 302 or 303 by turning the POST into a GET and forwards the
  Authorization header to whatever host the redirect names. The client now
  installs a redirect handler that raises on every 3xx and reports "app URL
  redirected, refusing to resend the token". A redirect is not retried.
- The comment on `CHUNK_BYTES` names the real limit. Chunks are 900 KiB
  because each one is stored as a single key value store value and the value
  cap is 1 MB. The 10 MiB request limit it used to cite is not the binding
  constraint.
- Publishing an index to Akamai Functions clears every chunk the previous
  index left behind. `finish_push` scanned a fixed 32 key window and stopped at
  the first missing key, so an index that shrank by more than 32 chunks
  stranded the rest in the key value store. With the generation-prefixed
  layout above, finish lists the keys and deletes the chunks of the
  generation the previous meta named, plus the pre-generation keys. The meta
  key is written before anything is deleted, so the new index goes live
  complete and a concurrent reader on another instance never loads a meta
  whose chunks are gone.
- `/healthz` reports the index the server is serving now, not the one loaded at startup. The probe asks `load_service` on every request, which swaps in a rebuilt index by mtime, so after a timer-driven `sync` the document count and `index_built_at` change without a restart. A reload that fails on a half-written file keeps the last good index, which is also what `tools/call` keeps serving. `core.tools.current_service()` is the lock-free accessor that makes this possible.
- `SearchIndex.from_prebuilt` checks the values inside a prebuilt block, not
  only the list lengths. A string or null where a number belongs, a position
  outside the document list, a float or negative position, postings of
  unequal length, and terms that are not sorted unique strings are refused at
  load with the `ValueError` the transports map to "not readable; run sync".
  Before, such a block loaded and then raised `TypeError` or `IndexError`
  from inside every tool call, and a negative position ranked the wrong
  document without a word. The checks are whole-list operations in C and
  cost 7 ms on the 93,900 postings of the real index.
- `load_service` clears the recorded load error at the start of each attempt.
  A tool call during the stdio builder's retry now answers "still building"
  instead of reporting the failure the retry is fixing and telling the user
  to run `sync` in another shell against it.
- A reload that fails, on an index file damaged after a working one was
  loaded, keeps serving the previous service, records the error, prints one
  stderr line naming the build it still serves, and does not parse the bad
  file again until its modification time changes. Before, every tool call
  re-read the file and answered "not readable" while `current_service()`
  still held a working index. A first load still raises.
- `fetch_doc` on a guide with no headings and more than 40,000 characters of
  text sets `truncated` and prints the "Truncated at 40,000 characters" line,
  the same as a long section does. Before, the text was cut with no sign of
  it.
- `scripts/rank_check.py` measures the index a server would load: the
  prebuilt block when the file carries one for this schema, a fresh build
  otherwise, and its first line says which. With a block, it also ranks every
  query on a fresh build and exits 1 naming the queries where the two
  disagree, which is the sign that ranking changed without an `INDEX_SCHEMA`
  bump. Before, it always built from `docs`, so a wrong or stale block, the
  thing Functions serves on every request, printed the same table as a good
  one. A block that will not load is reported with the sync command and exit
  1. The index file is read as UTF-8, the way `sync` writes it.
- `--http --host 127.1` or `--host 127.000.000.001`, which uvicorn binds to
  127.0.0.1, is classified as loopback: the Host and Origin checks are on,
  the spelling as typed is in the allowlist so a local client that copies it
  into a URL still connects, and the startup line that called the address
  not loopback is gone. `is_loopback` expands the short and padded IPv4
  spellings with `socket.inet_aton` after `ipaddress` refuses them; a string
  with whitespace in it stays unclassified. The `transport_security`
  docstring names `0:0:0:0:0:0:0:1` in place of `::ffff:127.0.0.1`, which
  uvicorn cannot bind.
- A push whose request reached the app but got no usable answer is retried
  and, when it keeps failing, reported with the resume command. `_post_json`
  mapped only `URLError` to `TransientError`, and urllib wraps only the send
  in `URLError`. A socket timeout while waiting for the status line came out
  as a bare `TimeoutError`, a server that closed after reading the body as
  `http.client.RemoteDisconnected`, a short body as `IncompleteRead`. Each
  escaped `sync` as a traceback after one attempt with no
  "The index on disk is intact" line, which is the load balancer reset the
  retry was written for. Measured against stub servers on 127.0.0.1 with a
  2 s timeout: a hang before headers and a close after the body are now
  three attempts, then `sync failed: could not reach ...` plus the resume
  command. `OSError` and `http.client.HTTPException` now map to
  `TransientError`, after the `HTTPError` branch, which stays first because
  `HTTPError` is an `OSError` too. The worst case for one request is 93 s,
  three 30 s timeouts plus 3 s of backoff, and the comment on
  `PUSH_ATTEMPTS` says so.
- `sync --push-only` with a corrupt or half-written `index.json` prints
  `sync failed: cannot read the index to push: Expecting value: line 1
  column 24 (char 23)` and exits 1. It printed a `JSONDecodeError` traceback.
  `run()` caught `OSError` only; `json.JSONDecodeError` and
  `UnicodeDecodeError` are `ValueError`s and are caught now. The `status`
  subcommand already handled the same file.
- The `MIN_OPERATIONS` floor, and any other `ApiCatalogError`, ends `sync`
  with `sync failed: only 1 operations parsed, expected at least 400. ...`
  and exit 1. `run()` caught `SyncError` and `CatalogError`;
  `ApiCatalogError` inherits from neither, so the floor that exists to
  refuse a thin index reported it as a traceback. `sync.py` imports
  `ApiCatalogError` and catches it next to the other two.
- The rebuild hint printed when `--push-only` refuses an index the app cannot
  serve carries the `--token-env` and `--out` the operator gave. It was a
  hand-written `sync --push URL`, so following it rebuilt into the default
  cache directory and read `FUNCTIONS_SYNC_TOKEN` instead of the named
  variable. Both operator commands now come from one builder.
- The resume command names `--out` as an absolute path. It echoed the path as
  typed, so `--out index.json` produced a line that only worked from the
  directory the failed sync ran in.
- Five parser patterns were quadratic on one crafted line. A guide carrying
  such a line hung every `search_docs` hit and every `fetch_doc` for that
  page past the 30 s Functions cap, and one of them hung `sync`. All five are
  linear now. Timings are the same inputs before and after, on this box:
  - The emphasis patterns in `core/sections.py` scanned to the end of the
    line from every `*` or `_` that never closed. `flatten_inline(" *a" *
    20000)` (60 KB) took 11.3 s and 120 KB ran past 15 s; both take 0.02 s.
    The span between two markers now stops at the next marker and may only
    cross a lone one (`5 * 3`) or an intraword underscore (`foo_bar`). The
    pass runs innermost-first, so `**Hosting the domain *example.com*.**`
    flattens to `Hosting the domain example.com.` where it used to keep the
    inner stars. Over the 394 cached guides, section ids, titles and snippets
    are identical; one summary (`a-and-aaaa-records`) lost its stray stars,
    and 11 guides lost stray markers in their flattened text.
  - `_HEADING_RE` re-scanned a whitespace run inside a heading from every
    position: a heading with 40,000 spaces took 6.0 s to match, now under
    1 ms. The closing `#` sequence is dropped with `str.rstrip` instead of a
    regex that took 9.5 s on a title of 20,000 spaces and 20,000 hashes.
  - `_NAV_LINK_RE` in `core/catalog.py` retried the URL from every `](` on a
    navigation line: `- [` + `](a` * 40000 took 3.3 s to match, and
    `clean_guide_markdown` on a page carrying it 6.6 s; both under 5 ms. That
    call runs inside `sync` for every guide and inside every live
    `fetch_guide`. A title may still carry `]`, as in `[Widgets [beta]](url)`.
  - The `<!--.*?-->` branch of `_HTML_TAG_RE` scanned to the end from every
    `<!--` that never closed: `flatten_inline("<!--" * 20000)` took 6.9 s,
    now 0.01 s. `flatten_inline` drops comments with `str.find`; the first
    `-->` after an opener closes it, and an opener that never closes stays
    as text.
- A `[block:html]` whose JSON wrote `<` as `<` carried its comments past
  `strip_html_comments`, because blocks were converted after comments were
  stripped. After `clean_guide_markdown` a crafted page held 100,000 `<!--`
  openers (400 KB), and `snippet` and `build_toc` on it ran past 30 s. Blocks
  convert first now, so a comment inside an html block goes the same way as
  one in the page: the same page comes out with 0 openers and 23 bytes. A
  literal `<!--` inside a block's JSON used to cut the JSON at the opener and
  leave the block unrendered; it now renders and the comment goes.
- `tests/test_security.py` gained `TestParserTimeBounds`: seven shapes of
  about 100 KB (star and underscore emphasis, a heading with a space run, a
  heading with a closing hash run, a navigation link line, comment openers,
  and the escaped html block) go through `clean_guide_markdown`,
  `parse_document` with `build_toc`, `snippet` and `flatten_inline`, each
  under a 1 s budget. An alarm cuts a hang short, so a regression fails in a
  second instead of stalling the suite.
- Functions: a live guide fetch that fails inside the Spin runtime is reported
  by its wasi error code (`ErrorCode_ConnectionReadTimeout`,
  `ErrorCode_HttpRequestDenied`, `ErrorCode_ConnectionRefused`), not as
  `Err`. The SDK raises `spin_sdk.wit.types.Err` around the code, and the
  fallback note and the log named the wrapper, so a timeout, a refused
  connection, and a denied host all read the same. Checked with a build whose
  bounds are 1 ns: the note reads
  `read failed: ErrorCode_ConnectionReadTimeout`.
- Functions: a finish sweeps only the generation the previous meta named,
  plus the `index:chunk:N` keys from before generations. It deleted every
  other generation, so when two pushes overlapped one finish could delete the
  other's chunks between its verification reads and its meta write: both
  clients saw 200 and every reader got 503 until the next push. The chunks of
  a push that died before its finish now stay in the store, because the app
  cannot tell them from a push in flight.
- Functions: a `tools/call` that finds no usable index logs
  `{"tool": ..., "ms": ..., "ok": false, "error": "index"}` before answering
  503. It logged nothing, so `spin aka logs` could not show an outage. A call
  that crashes logs the exception class name in the same field.
- Functions: the 10 s outbound bound covers each wait for the next body chunk
  (`set_between_bytes_timeout`) as well as the connect and the first byte,
  and each setter is tried on its own, so a runtime that refuses one bound
  keeps the others. A refused second setter used to discard the first.
- Functions: `HEAD /` answers with the status and headers of `GET /` and no
  body, so an uptime probe sees the page as up or down; it was a 405.
  `OPTIONS /` is a 405 with `Allow: GET, HEAD`; it promised CORS headers that
  `GET /` never sent. CORS stays on `/mcp` only.
- Functions: a store error in the sweep after the meta write answers 200 with
  a `warning` field and logs `{"event": "sweep", "ok": false, "error":
  "<class>"}`. It answered 500, which the sync client retried three times and
  then reported as a rejected push although the index was published.
- Functions: `app.py` no longer states the chunk count of the index in a
  comment; `sync --push` prints it.

### Removed

- `kind` and `score` from `search_docs` results. See the search result entry
  under Changed.
- `kind`, `url` and `hint` from a table of contents. The section result still
  carries the url.
- The staleness note from guide results. Guide text is fetched live.
- `tests/` from the sdist. The wheel and the sdist now pass the same gate;
  anyone who wants the tests has the repository.
- The previous Functions key value store layout (`index:chunk:N` with no
  generation). The first push to an upgraded app deletes those keys. Until
  that push, the old meta makes `GET /` and `tools/call` answer 503, so push
  right after deploying.

## [0.1.0] - 2026-08-13

First release. An MCP server for Akamai Cloud documentation that runs over
stdio, stateless streamable HTTP, and Akamai Functions.

- 392 guide pages and 449 Linode API operations, 409 of them with a linode-cli
  command, in one index built from live sources.
- Two tools. `search_docs` ranks across both catalogs; `fetch_doc` returns a
  table of contents, then one section. Reading a section of a 27KB guide costs
  3,581 bytes across both calls.
- Errors teach: an unknown id returns the closest real ids, a bad section
  returns the section list.
- No documentation text ships in the wheel. A CI gate enforces it.

### Added

- Phase 6, 2026-08-13. Akamai Functions entry point.
  - `functions/app.py`: a stateless JSON-RPC handler on Spin, reusing `core/`
    unchanged. `tools/list`, `tools/call`, a status page, and a token-guarded
    `/admin/sync`. The index is read from the key value store in chunks.
  - `functions/spin.toml`: http trigger, `key_value_stores = ["default"]`,
    `allowed_outbound_hosts` listing only `https://techdocs.akamai.com`, and a
    required `sync_token` application variable.
  - `sync --push <app-url> --token-env <VAR>` builds the index and uploads it in
    900KB chunks. The meta key is written last, so a reader sees either the old
    index or the new one, never a mixture.
  - `functions/README.md`: build, run locally, deploy, and push.
- Phase 5, 2026-08-13. Eval harness and results.
  - `eval/questions.json`: 12 questions, 5 guide how-tos, 4 API, 3 linode-cli.
    Gold facts were read out of the live documentation on 2026-08-13.
  - `eval/harness.py`: an agentic tool loop capped at 6 tool calls, talking to
    `core.tools` in this process. Two providers, three conditions. Token counts
    come from the providers' usage fields. A provider whose environment
    variables are missing is skipped with a note, and nothing from the
    environment is printed or written out.
  - `eval/fat_descriptions.py`: the same two tools described at four times the
    length, for the description-length comparison.
  - `eval/RESULTS.md` and `eval/raw_results.jsonl`, both committed. The prose
    summary is generated from the run, so it cannot drift from its own tables.
  - `--from-raw` rebuilds the report from the recorded records without calling a
    model. A partial run writes to a separate file so a smoke test cannot
    overwrite a full run's committed results.
- Phase 4, 2026-08-13. Security hardening and the packaging gate.
  - `tests/test_security.py`, 52 tests covering the outbound fetch rules, the
    redirect allowlist, hostile ids, input bounds, and the promise that no
    traceback reaches a tool result.
  - `scripts/check_wheel.py`: builds the wheel and fails if it holds an index, a
    fixture, a test path, or any non-source file. Verified against a wheel with
    a documentation file injected into it.
  - `.github/workflows/ci.yml`: lint and tests on Python 3.11, 3.12, and 3.13,
    plus the wheel gate, on every push to `main` and every pull request.
    Actions are pinned by commit SHA, because a tag can be moved and a SHA
    cannot. All three interpreters were run locally first.
- Phase 3, 2026-08-13. API and CLI catalog.
  - `core/api_catalog.py`: the Linode OpenAPI spec parsed into 449 operation
    entries with rendered cards. `x-linode-cli-command` lives on the path item
    and `x-linode-cli-action` on the operation, so the two are joined to build
    the command. An action can be a list, which becomes `cli_aliases`. The 39
    operations flagged `x-linode-cli-skip` get no command, because the CLI does
    not expose them. 409 of 449 end up with a command.
  - Cards carry method, path, summary, a one-line description, parameters,
    required body fields nested two deep, the CLI command, OAuth scopes, and the
    reference URL. Response schemas are left out. Cards average 408 bytes and
    the largest is 1,077.
  - `/{apiVersion}` is stripped from paths and its parameter dropped, so ids read
    as `POST /databases/postgresql/instances`.
  - `sync.py` fetches the spec and enforces a 400 operation floor.
  - `fetch_doc` resolves an API id from `METHOD /path` in any case, an
    operationId, or a `linode-cli` command string including its aliases.
- Phase 2, 2026-08-13. MCP entry points.
  - `core/tools.py`: `search_docs` and `fetch_doc` over plain dicts, with id
    resolution for slugs, techdocs URLs, `METHOD /path`, operationIds, and
    `linode-cli` command strings. Auto-sync on first use, a 7 day staleness
    note, and teaching errors carrying `did_you_mean` or `valid_sections`.
    A failed live fetch falls back to the indexed copy and says so.
  - `server.py`: `MCPServer` construction and tool registration. Tool bodies run
    blocking work in a worker thread so a live page fetch never stalls the event
    loop. Results are serialized as one compact JSON block, which keeps a search
    result list in a single block and drops the default indentation.
  - `stdio.py` and `http.py`: the two local transports. HTTP is stateless, binds
    127.0.0.1, and loads the index at startup so a missing index is a boot
    failure rather than a first-request surprise.
  - `__main__.py`: bare command serves stdio, `--http --host --port --path`
    serves HTTP, `sync` builds the index.
  - 50 more tests, including the full tool contract and an end to end pass
    through the SDK's in-process client.
- Phase 1, 2026-08-13. Foundation and the guide catalog.
  - `config.py`: source URLs, request caps, input bounds, cache directory
    resolution with an `AKAMAI_DOCS_MCP_CACHE_DIR` override.
  - `core/catalog.py`: llms.txt parsing, slug and URL normalization, an https
    plus host plus path allowlist applied to every request and every redirect,
    guide markdown fetching with a 2MB cap. Strips frontmatter, the index
    boilerplate line, and the trailing `Sibling pages` navigation list.
  - `core/sections.py`: heading tree parsing with dotted section ids, code-fence
    aware scanning, section extraction, table of contents with summaries,
    query-centered snippets.
  - `core/index.py`: hand-rolled inverted index. TF-IDF term weights with BM25
    length normalization, a title and heading boost, plural folding, query
    coverage weighting, and close-match suggestions for unknown ids.
  - `sync.py`: index builder writing one `index.json`, 8 concurrent fetches, a
    300 page floor, atomic writes.
  - `__main__.py`: the `akamai-cloud-docs-mcp sync` command.
  - 103 tests over invented fixtures. The suite blocks sockets, so it runs
    offline and cannot depend on a live site.

### Changed

- The tool descriptions moved from `server.py` into `core/tools.py`. Every
  transport needs them, and `core/` has no third-party imports. `server.py`
  re-exports them, so existing imports still work.
- `DocsService.index_age_days` uses `datetime.fromisoformat` rather than
  `strptime`. `strptime` lazily imports `_strptime`, which is not bundled in the
  WebAssembly build, so it failed there and nowhere else.
- `sync --push` uploads plain JSON rather than gzip. The interpreter on Akamai
  Functions has no `zlib`, so the component could not decompress it.
- An HTTP error from a source now reports only its status code. The response
  body never reaches an exception message.
- Search expands a rare query term to longer indexed terms that start with
  it, so "postgres" reaches "postgresql". Expansion is limited to terms of five
  characters or more appearing in eight documents or fewer, and prefers the most
  common expansion. A wider rule made results worse: expanding a common word
  like "list" pulls in rare compounds whose high idf then outranks the right
  answer.
- A title matching the query exactly scores above one that merely contains it,
  so "create a firewall" ranks `POST /networking/firewalls` over
  `POST /networking/firewalls/{firewallId}/devices`.
- On a 14 query check against both catalogs, these two ranking changes moved
  top-1 from 1 to 9 and mean reciprocal rank from 0.30 to 0.68.

### Fixed

- The redirect allowlist was global to the shared opener, so it applied the
  techdocs rules to the GitHub spec download. Allowed redirect hosts now travel
  with each request, defaulting to the host of the URL being fetched.
- The test network block now patches `socket.getaddrinfo` and
  `socket.create_connection` rather than `socket.socket`, which the event loop
  needs for its local self-pipe.
- A rejected URL returns the reason it was refused instead of fuzzy title
  matches, which were unrelated to the input.

### Verified

- `spin build` succeeds, and a local `spin up` served the whole flow: push the
  841 document index into the key value store, `tools/list`, `search_docs`,
  `fetch_doc` for an API card, and `fetch_doc` for a guide section fetched live
  from techdocs through the Spin HTTP client.
- `/admin/sync` answers 401 with no token and with a wrong token, 405 to GET,
  and unknown routes 404.
- Eval results: gpt-5-mini went from 68% fact accuracy with no tools to 100%
  with them. Qwen2.5-7B-Instruct went from 36% to 72%. The three linode-cli
  questions went from 0% to 100% on Qwen2.5-7B-Instruct; those answers are
  exact command strings. Quadrupling the tool descriptions cost gpt-5-mini
  3,456 more prompt tokens per question and moved accuracy not at all. On
  Qwen2.5-7B-Instruct the same text raised accuracy from 72% to 88%.
- `tools/list` and `tools/call` answer raw stateless JSON-RPC over curl with
  `MCP-Protocol-Version: 2026-07-28`. The 2026-07-28 wire format requires a
  `params._meta` envelope carrying `io.modelcontextprotocol/protocolVersion` and
  `io.modelcontextprotocol/clientCapabilities`.
- stdio answers `tools/list` and both tools when launched as a subprocess.
- Reading one section of a 27,014 byte guide costs 3,581 bytes across both
  calls, an 87% reduction.
- A live sync builds 392 guides into a 2.7MB index in 41 seconds.
- `search("resize database cluster")` ranks `aiven-manage-database` first.
- A 27KB guide parses into a 12-section table of contents, and the resize
  section extracts to 708 characters.

[0.2.0]: https://github.com/labeveryday/akamai-cloud-docs-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/labeveryday/akamai-cloud-docs-mcp/releases/tag/v0.1.0
