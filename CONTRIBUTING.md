# Contributing

The clone, install, test, lint, and release-gate commands are under
[Development in the README](README.md#development). Run them from a checkout;
`uv sync --extra dev` installs from `uv.lock` into `.venv`.

Rules for a pull request:

- Test fixtures are invented content. No text from techdocs.akamai.com or the
  Linode API reference goes into `tests/`, and no built index is committed.
  `scripts/check_wheel.py` fails the build if either reaches an artifact.
- One entry in `CHANGELOG.md` per pull request, under the top version heading
  while it is marked Unreleased (today `## [0.2.0] - Unreleased`), in the
  Keep a Changelog headings (Added, Changed, Fixed, Removed).
- If you change ranking (`core/index.py`) or parsing (`core/sections.py`,
  `core/catalog.py`), run `scripts/rank_check.py` and `scripts/section_check.py`
  against a fresh `sync` and paste the before and after numbers in the pull
  request.
- Prose in code, docs and help text: plain words, short sentences.

Bug reports: use the issue template. It asks for your client, the transport,
`akamai-cloud-docs-mcp --version`, `akamai-cloud-docs-mcp status`, and the
failing query.
