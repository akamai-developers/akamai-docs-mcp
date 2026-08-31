"""Source URLs, limits, and cache location.

Every tunable the rest of the package needs lives here. Nothing in this module
imports from the `mcp` package, so `core/` stays transport-agnostic.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__

# --- Sources -----------------------------------------------------------------

MASTER_LLMS_URL = "https://techdocs.akamai.com/cloud-computing/llms.txt"
GUIDES_LLMS_URL = "https://techdocs.akamai.com/cloud-computing/docs/llms.txt"
OPENAPI_URL = (
    "https://raw.githubusercontent.com/linode/linode-api-docs/"
    "refs/heads/development/openapi.json"
)

# Guide pages are the only URLs the server ever fetches at runtime.
DOCS_HOST = "techdocs.akamai.com"
DOCS_PATH_PREFIX = "/cloud-computing/docs/"
DOCS_BASE = f"https://{DOCS_HOST}{DOCS_PATH_PREFIX}"

# API reference pages are linked, never fetched (they do not serve markdown).
API_REFERENCE_BASE = "https://techdocs.akamai.com/linode-api/reference/"

# --- Outbound HTTP -----------------------------------------------------------

USER_AGENT = f"akamai-cloud-docs-mcp/{__version__}"
REQUEST_TIMEOUT = 30.0
#: Cap for a single documentation page.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: Cap for the OpenAPI spec, which is ~8MB and fetched only during sync.
MAX_SPEC_BYTES = 32 * 1024 * 1024
SYNC_CONCURRENCY = 8

# --- Sync guards -------------------------------------------------------------
# A source format change should fail the build loudly, not produce an empty index.

MIN_GUIDES = 300
MIN_OPERATIONS = 400

# --- Input bounds ------------------------------------------------------------

MAX_ID_LEN = 256
MAX_SECTION_LEN = 256
MAX_QUERY_LEN = 1000
MIN_K = 1
MAX_K = 20
DEFAULT_K = 5

# --- Behavior ----------------------------------------------------------------

#: Guides smaller than this are returned whole instead of as a table of contents.
SMALL_DOC_BYTES = 8 * 1024
#: fetch_doc adds a note to its result when the index is older than this.
STALE_AFTER_DAYS = 7

#: Bump this whenever `sync` writes something a reader of the previous number
#: would misread. Schema 2 added the `prebuilt` block: the search index's
#: postings and lengths, computed once at sync time so a reader does not
#: tokenize 843 documents on every load. Anything that changes what
#: `core/index.py` computes at build (term weights, stopwords, the stemmer, the
#: repetition constant) has to bump this too, or an index on disk would keep
#: serving the old ranking.
INDEX_SCHEMA = 2
INDEX_FILENAME = "index.json"

# --- Cache -------------------------------------------------------------------

CACHE_DIR_ENV = "AKAMAI_DOCS_MCP_CACHE_DIR"


def cache_dir() -> Path:
    """Directory holding the built index.

    Order: `AKAMAI_DOCS_MCP_CACHE_DIR`, then `$XDG_CACHE_HOME`, then `~/.cache`.
    """
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "akamai-cloud-docs-mcp"


def index_path() -> Path:
    """Full path to the built index."""
    return cache_dir() / INDEX_FILENAME
