"""Search ranking, tokenization, and suggestions."""

from __future__ import annotations

import difflib
import json
import time

import pytest

from akamai_cloud_docs_mcp.core.index import SearchIndex, query_tokens, stem, tokenize


class TestTokenize:
    def test_splits_paths_and_hyphens(self):
        tokens = tokenize("POST /databases/postgresql/instances linode-cli")
        assert "database" in tokens
        assert "postgresql" in tokens
        assert "instance" in tokens
        assert "linode" in tokens
        assert "cli" in tokens

    def test_drops_stopwords(self):
        assert tokenize("how do I create a widget") == ["create", "widget"]

    def test_query_falls_back_when_only_stopwords(self):
        assert query_tokens("how do I") != []

    def test_stem_folds_plurals(self):
        assert stem("clusters") == "cluster"
        assert stem("databases") == "database"
        assert stem("policies") == "policy"
        assert stem("status") == "status"
        assert stem("access") == "access"


class TestSearch:
    def test_finds_the_obvious_document(self, sample_docs):
        results = SearchIndex(sample_docs).search("resize a widget", k=3)
        assert results[0]["id"] == "resize-a-widget"

    def test_plural_query_matches_singular_text(self, sample_docs):
        results = SearchIndex(sample_docs).search("sprockets", k=3)
        assert results[0]["id"] == "sprocket-basics"

    def test_result_shape(self, sample_docs):
        result = SearchIndex(sample_docs).search("sprocket", k=1)[0]
        assert set(result) == {"id", "kind", "title", "snippet", "score"}
        assert result["kind"] == "guide"
        assert isinstance(result["score"], float)

    def test_api_documents_are_tagged(self, sample_docs):
        results = SearchIndex(sample_docs).search("linode-cli widgets create", k=4)
        api = [r for r in results if r["kind"] == "api"]
        assert api and api[0]["id"] == "POST /widgets"

    def test_k_limits_results(self, sample_docs):
        assert len(SearchIndex(sample_docs).search("widget", k=2)) == 2

    def test_no_match_returns_empty_list(self, sample_docs):
        assert SearchIndex(sample_docs).search("kubernetes helm chart rollout", k=5) == []

    def test_empty_query_returns_empty_list(self, sample_docs):
        assert SearchIndex(sample_docs).search("", k=5) == []

    def test_empty_corpus(self):
        assert SearchIndex([]).search("anything", k=5) == []

    def test_kind_filter(self, sample_docs):
        results = SearchIndex(sample_docs).search("widget", k=5, kind="api")
        assert results and all(r["kind"] == "api" for r in results)

    def test_ranking_is_stable_across_builds(self, sample_docs):
        first = SearchIndex(sample_docs).search("widget", k=4)
        second = SearchIndex(list(sample_docs)).search("widget", k=4)
        assert [r["id"] for r in first] == [r["id"] for r in second]


class TestPrefixExpansion:
    """A rare term reaches its canonical longer spelling, common terms do not."""

    @pytest.fixture
    def corpus(self) -> list[dict]:
        docs = [
            {
                "id": "postgres-note",
                "kind": "guide",
                "title": "Postgres note",
                "text": "A short note that says postgres once.",
            },
            {
                "id": "postgresql-guide",
                "kind": "guide",
                "title": "PostgreSQL guide",
                "text": "PostgreSQL setup. PostgreSQL tuning. PostgreSQL backups.",
            },
        ]
        # Enough documents carrying "list" that it counts as common, each also
        # carrying a rare compound that expansion must not reach for.
        docs += [
            {
                "id": f"listing-{n}",
                "kind": "guide",
                "title": f"Listing {n}",
                "text": f"list the widgets listobjectsversion{n} here",
            }
            for n in range(12)
        ]
        return docs

    def test_rare_term_reaches_the_canonical_spelling(self, corpus):
        assert "postgresql" in SearchIndex(corpus).expand_prefix("postgre")

    def test_common_term_is_not_expanded(self, corpus):
        assert SearchIndex(corpus).expand_prefix("list") == []

    def test_short_term_is_not_expanded(self, corpus):
        assert SearchIndex(corpus).expand_prefix("pos") == []

    def test_expansion_finds_the_longer_document(self, corpus):
        results = SearchIndex(corpus).search("postgres setup", k=2)
        assert results[0]["id"] == "postgresql-guide"

    def test_expansion_does_not_drag_in_rare_compounds(self, corpus):
        # "list widgets" must rank a listing page, not be reordered by a rare
        # high-idf compound that merely starts with "list".
        results = SearchIndex(corpus).search("list widgets", k=3)
        assert all(result["id"].startswith("listing-") for result in results)

    def test_expansion_never_outscores_an_exact_match(self, corpus):
        results = SearchIndex(corpus).search("postgres", k=2)
        assert {result["id"] for result in results} == {"postgres-note", "postgresql-guide"}


class TestRepetitionPenalty:
    """A page that repeats a small vocabulary is treated as the verbose one.

    The real corpus has reference pages that are hundreds of table rows over a
    few hundred words. They match many queries on term overlap alone, so length
    normalization scales with how repetitive a page is for its kind.
    """

    @pytest.fixture
    def corpus(self) -> list[dict]:
        # 200 rows over a 4 word vocabulary. Says "resize widget" constantly and
        # explains nothing. Its title does not match the query, the way the real
        # guide for resizing a database cluster is titled "Create and manage
        # database clusters", so no title boost decides this for us.
        table = "\n".join(f"| resize widget event {n % 4} |" for n in range(200))
        # A page that genuinely covers resizing. It repeats the query terms about
        # as often as the table does, so term frequency alone cannot separate
        # them. The vocabulary around those terms is what differs.
        lines = [
            "Resizing a widget moves it to a different plan and hardware.",
            "Open the dashboard and select the widget you intend to resize.",
            "Pick a larger widget plan from the list of available sizes.",
            "Confirm the reboot, because a resize restarts the widget.",
            "The resize migration copies the widget disk to new hardware.",
            "Downtime during a widget resize depends on the disk size.",
            "Verify the widget reports the new plan once the resize finishes.",
            "Billing for the resized widget changes on the next cycle.",
            "You cannot resize a widget downward without shrinking its disk.",
            "Contact support when a widget resize stalls beyond an hour.",
            "A cold resize powers the widget off before moving it.",
            "A warm resize keeps the widget running during the transfer.",
            "Check the widget event log to watch the resize progress.",
            "Snapshots taken before the resize stay attached to the widget.",
            "Network settings survive a widget resize without changes.",
        ]
        answer = "# Changing a plan\n\n" + "\n\n".join(lines) + "\n"
        docs = [
            {"id": "event-table", "kind": "guide", "title": "Event table", "text": table},
            {"id": "resize-guide", "kind": "guide", "title": "Change a widget plan", "text": answer},
        ]
        # Ordinary prose, so the corpus median sits where real guides sit rather
        # than at 1.0. A corpus of one-line fillers would make every real page
        # look repetitive by comparison.
        docs += [
            {
                "id": f"other-{n}",
                "kind": "guide",
                "title": f"Other {n}",
                "text": (
                    f"# Sprocket note {n}\n\n"
                    f"A sprocket turns inside its housing. This note covers sprocket {n} "
                    "and its maintenance schedule. Inspect the sprocket for wear, then "
                    "replace the sprocket when the teeth are worn. Routine sprocket "
                    "maintenance keeps the housing quiet.\n"
                ),
            }
            for n in range(30)
        ]
        return docs

    def test_repetitive_page_scores_far_below_the_real_answer(self, corpus):
        """The table is not merely beaten, it is out of contention.

        Without the penalty the two score within a few percent of each other,
        which is how a reference table ends up second on queries it cannot
        answer. Asserting the size of the gap, not the order, is what makes this
        test fail if the penalty is removed.
        """
        results = SearchIndex(corpus).search("resize a widget", k=2)
        scores = {result["id"]: result["score"] for result in results}
        # Measured 0.45 with the penalty, 0.94 without it.
        assert scores["event-table"] < 0.6 * scores["resize-guide"]

    def test_repetitive_page_is_normalized_as_longer(self, corpus):
        index = SearchIndex(corpus)
        position = next(i for i, d in enumerate(index.docs) if d["id"] == "event-table")
        entry = index._entries[position]
        assert entry.norm_length > entry.length

    def test_ordinary_prose_is_barely_touched(self, corpus):
        index = SearchIndex(corpus)
        position = next(i for i, d in enumerate(index.docs) if d["id"] == "resize-guide")
        entry = index._entries[position]
        assert entry.norm_length == pytest.approx(entry.length, rel=0.05)

    def test_kinds_are_compared_separately(self):
        """An api card is measured against other api cards, not against prose."""
        docs = [
            {"id": f"guide-{n}", "kind": "guide", "title": f"G{n}", "text": "some prose " * 30}
            for n in range(5)
        ]
        docs += [
            {"id": f"POST /r{n}", "kind": "api", "title": f"R{n}", "text": f"post r{n} label region"}
            for n in range(5)
        ]
        index = SearchIndex(docs)
        # Every document sits at its own kind's median, so nothing is rescaled.
        for entry in index._entries:
            assert entry.norm_length == pytest.approx(entry.length, rel=0.01)


class TestTitleBoost:
    def test_exact_title_wins_over_a_longer_title(self):
        docs = [
            {
                "id": "firewall-device",
                "kind": "api",
                "title": "Create a firewall device",
                "text": "POST /networking/firewalls/{id}/devices create a firewall device",
            },
            {
                "id": "firewall",
                "kind": "api",
                "title": "Create a firewall",
                "text": "POST /networking/firewalls create a firewall",
            },
        ]
        assert SearchIndex(docs).search("create a firewall", k=2)[0]["id"] == "firewall"


class TestIdAsQuery:
    """Searching a document's own id returns that document first.

    The tool description tells the model to pass ids around verbatim, so an id
    it already holds is a common query. On the live index 231 of 841 ids used
    to lose to a neighbour, most often a longer path that shared every token
    plus a title word, and slug-only ids such as `iam-aclp` were not indexed
    at all.
    """

    @pytest.fixture
    def corpus(self) -> list[dict]:
        # The trap from the live index: the list operation shares every token
        # with the single-item operation, whose title also carries "get".
        docs = [
            {
                "id": "GET /widgets",
                "kind": "api",
                "title": "List widgets",
                "text": "GET /widgets List widgets Returns a paginated list of widgets. page",
            },
            {
                "id": "GET /widgets/{widgetId}",
                "kind": "api",
                "title": "Get a widget",
                "text": "GET /widgets/{widgetId} Get a widget Get a single widget by id. widgetId",
            },
            {
                "id": "DELETE /widgets/{widgetId}",
                "kind": "api",
                "title": "Delete a widget",
                "text": "DELETE /widgets/{widgetId} Delete a widget Deletes a widget. widgetId",
            },
            # A slug whose words appear nowhere in its title or text.
            {
                "id": "wcp-acl",
                "kind": "guide",
                "title": "Manage access to a workspace",
                "text": "# Manage access to a workspace\n\nGrant a user access to a workspace.\n",
            },
            {
                "id": "workspace-access-overview",
                "kind": "guide",
                "title": "Workspace access",
                "text": "# Workspace access\n\nWho can see a workspace and why.\n",
            },
        ]
        # A repetitive reference page whose slug shares tokens with a guide.
        table = "\n".join(f"| workspace access event {n % 3} |" for n in range(200))
        docs.append(
            {"id": "workspace-access-events", "kind": "guide", "title": "Events", "text": table}
        )
        return docs

    def test_every_id_ranks_first(self, corpus):
        index = SearchIndex(corpus)
        for doc in corpus:
            results = index.search(doc["id"], k=3)
            assert results and results[0]["id"] == doc["id"], doc["id"]

    def test_every_sample_id_ranks_first(self, sample_docs):
        index = SearchIndex(sample_docs)
        for doc in sample_docs:
            assert index.search(doc["id"], k=3)[0]["id"] == doc["id"]

    def test_slug_only_token_finds_the_guide(self, corpus):
        results = SearchIndex(corpus).search("wcp", k=2)
        assert results and results[0]["id"] == "wcp-acl"

    def test_id_boost_is_independent_of_the_title_boost(self):
        # A document whose id and title are the same string gets both boosts.
        # The other document only gets the title boost, so the gap is the id
        # boost alone.
        docs = [
            {"id": "gadget", "kind": "guide", "title": "gadget", "text": "A gadget."},
            {"id": "gadget-two", "kind": "guide", "title": "gadget", "text": "A gadget."},
        ]
        results = SearchIndex(docs).search("gadget", k=2)
        assert results[0]["id"] == "gadget"
        assert results[0]["score"] > 2 * results[1]["score"]


class TestSnippet:
    def test_api_snippet_drops_its_own_id_and_title(self):
        doc = {
            "id": "POST /gadgets",
            "kind": "api",
            "title": "Create a gadget",
            "text": "POST /gadgets Create a gadget Creates a gadget in a region. label region",
        }
        result = SearchIndex([doc]).search("create gadget", k=1)[0]
        assert result["snippet"].startswith("Creates a gadget")
        assert "POST /gadgets" not in result["snippet"]

    def test_api_text_without_the_prefix_is_used_as_is(self):
        doc = {
            "id": "POST /gadgets",
            "kind": "api",
            "title": "Create a gadget",
            "text": "Creates a gadget in a region. label region",
        }
        result = SearchIndex([doc]).search("create gadget", k=1)[0]
        assert result["snippet"].startswith("Creates a gadget")

    def test_guide_snippet_keeps_its_opening(self, sample_docs):
        result = SearchIndex(sample_docs).search("sprocket basics", k=1)[0]
        assert result["id"] == "sprocket-basics"
        # The heading is dropped, the body is kept whole.
        assert result["snippet"].startswith("A sprocket turns inside a widget")


class TestLookupAndSuggest:
    def test_get_by_id(self, sample_docs):
        assert SearchIndex(sample_docs).get("resize-a-widget")["title"] == "Resize a widget"

    def test_get_unknown(self, sample_docs):
        assert SearchIndex(sample_docs).get("nope") is None

    def test_suggest_close_slug(self, sample_docs):
        suggestions = SearchIndex(sample_docs).suggest("resize-widget")
        assert suggestions[0]["id"] == "resize-a-widget"
        assert set(suggestions[0]) == {"id", "title"}

    def test_suggest_returns_at_most_three(self, sample_docs):
        assert len(SearchIndex(sample_docs).suggest("widget")) <= 3

    def test_suggest_on_empty_input(self, sample_docs):
        assert SearchIndex(sample_docs).suggest("") == []

    def test_suggest_is_case_insensitive(self, sample_docs):
        assert SearchIndex(sample_docs).suggest("RESIZE-A-WIDGET")[0]["id"] == "resize-a-widget"


class TestSuggestCost:
    """An unknown 256-character id must not cost half a second of CPU.

    `fetch_doc` runs `suggest()` for every id it does not know, on an endpoint
    with no auth. The two cheap `SequenceMatcher` bounds reject most pairs
    before the full alignment, and they are upper bounds, so the suggestions
    are the same as the ungated computation.
    """

    @pytest.fixture
    def corpus(self) -> list[dict]:
        docs = [
            {"id": f"guide-{n}-{word}", "kind": "guide", "title": f"{word.title()} guide {n}"}
            for n in range(120)
            for word in ("widget", "sprocket", "gadget")
        ]
        docs += [
            {
                "id": f"GET /{word}s/{{{word}Id}}/things{n}",
                "kind": "api",
                "title": f"List {word} things",
            }
            for n in range(100)
            for word in ("widget", "sprocket", "gadget")
        ]
        return docs

    @staticmethod
    def ungated(docs: list[dict], needle: str, n: int = 3) -> list[str]:
        needle = needle.lower()
        scored = []
        for doc in docs:
            doc_id, title = doc["id"].lower(), doc["title"].lower()
            ratio = max(
                difflib.SequenceMatcher(None, needle, doc_id).ratio(),
                difflib.SequenceMatcher(None, needle, title).ratio(),
            )
            if needle in doc_id or needle in title:
                ratio = max(ratio, 0.9)
            scored.append((ratio, doc["id"]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [doc_id for ratio, doc_id in scored[:n] if ratio > 0.3]

    @pytest.mark.parametrize(
        "needle",
        [
            "a-" * 128,
            ("abcdefghijklmnopqrstuvwxyz" * 10)[:256],
            ("widget thing " * 20)[:256],
            "x" * 256,
            "widget-guide-7",
            "get /widgets/{widgetid}/things1",
            "sprokcet",
        ],
    )
    def test_gate_returns_the_ungated_suggestions(self, corpus, needle):
        index = SearchIndex(corpus)
        assert [s["id"] for s in index.suggest(needle)] == self.ungated(corpus, needle)

    def test_long_unknown_id_is_cheap(self, corpus):
        index = SearchIndex(corpus)
        index.suggest("a-" * 128)  # warm up
        for needle in ("a-" * 128, ("widget thing " * 20)[:256], "x" * 256):
            started = time.perf_counter()
            index.suggest(needle)
            # The ungated version took about 0.4 s on the live index. A loose
            # bound so a slow CI box does not flake.
            assert time.perf_counter() - started < 0.1, needle


class TestPrebuilt:
    """`from_prebuilt` answers exactly like the index its block was written from.

    `sync` stores `prebuilt()` in the index and the Functions handler loads
    nothing else, so the two paths must agree to the last digit after a trip
    through JSON. The queries cover every shape the ranking special-cases:
    plain words, a plural, a prefix expansion, an exact title, an exact id,
    stopwords only, and a kind filter.
    """

    QUERIES = [
        "resize a widget",
        "sprockets",
        "postgres setup",
        "linode-cli widgets create",
        "create a widget",
        "POST /widgets",
        "event-table",
        "how do I",
        "kubernetes helm chart rollout",
        "",
    ]

    @pytest.fixture
    def corpus(self, sample_docs) -> list[dict]:
        """The sample documents plus the shapes that make the numbers non-trivial.

        A repetitive table gets a norm length that is not its length, the two
        postgres pages trigger a prefix expansion, and a duplicated id checks
        that `by_id` keeps the first document both ways.
        """
        table = "\n".join(f"| resize widget event {n % 4} |" for n in range(200))
        return [
            *sample_docs,
            {"id": "event-table", "kind": "guide", "title": "Event table", "text": table},
            {
                "id": "postgresql-guide",
                "kind": "guide",
                "title": "PostgreSQL guide",
                "text": "PostgreSQL setup. PostgreSQL tuning. PostgreSQL backups.",
            },
            {
                "id": "postgres-note",
                "kind": "guide",
                "title": "Postgres note",
                "text": "A short note that says postgres once.",
            },
            {**sample_docs[0], "title": "A later duplicate"},
        ]

    @staticmethod
    def both(docs: list[dict]) -> tuple[SearchIndex, SearchIndex]:
        built = SearchIndex(docs)
        block = json.loads(json.dumps(built.prebuilt(), separators=(",", ":")))
        return built, SearchIndex.from_prebuilt(docs, block)

    @pytest.mark.parametrize("name", ["sample_docs", "corpus"])
    def test_search_results_are_identical(self, request, name):
        docs = request.getfixturevalue(name)
        built, loaded = self.both(docs)
        for query in self.QUERIES + [doc["id"] for doc in docs]:
            assert loaded.search(query, k=10) == built.search(query, k=10), query
        assert loaded.search("widget", k=5, kind="api") == built.search("widget", k=5, kind="api")

    def test_suggestions_and_expansions_are_identical(self, corpus):
        built, loaded = self.both(corpus)
        for needle in ("resize-widget", "RESIZE-A-WIDGET", "widget", "a-" * 128, ""):
            assert loaded.suggest(needle) == built.suggest(needle), needle
        for term in ("postgre", "sprock", "widge", "post", "list"):
            assert loaded.expand_prefix(term) == built.expand_prefix(term), term

    def test_the_numbers_behind_the_scores_are_identical(self, corpus):
        built, loaded = self.both(corpus)
        assert loaded._avg_length == built._avg_length
        assert [e.length for e in loaded._entries] == [e.length for e in built._entries]
        assert [e.norm_length for e in loaded._entries] == [e.norm_length for e in built._entries]
        assert loaded._terms == built._terms
        assert loaded._postings == built._postings
        assert loaded.by_id == built.by_id
        assert loaded.get("create-a-widget")["title"] == "Create a widget"

    def test_the_block_is_plain_json_with_whole_numbers(self, corpus):
        block = SearchIndex(corpus).prebuilt()
        assert set(block) == {"terms", "positions", "weights", "lengths", "norm_lengths", "avg_length"}
        assert block["terms"] == sorted(block["terms"])
        assert len(block["terms"]) == len(block["positions"]) == len(block["weights"])
        assert all(isinstance(w, int) for weights in block["weights"] for w in weights)
        assert all(isinstance(length, int) for length in block["lengths"])
        assert len(block["lengths"]) == len(block["norm_lengths"]) == len(corpus)
        assert isinstance(block["avg_length"], float)
        # The repetitive table is the one document whose norm length moved.
        position = next(i for i, doc in enumerate(corpus) if doc["id"] == "event-table")
        assert block["norm_lengths"][position] > block["lengths"][position]

    def test_loading_builds_nothing(self, sample_docs, monkeypatch):
        block = SearchIndex(sample_docs).prebuilt()

        def never(self):
            raise AssertionError("from_prebuilt tokenized the documents")

        monkeypatch.setattr(SearchIndex, "_build", never)
        loaded = SearchIndex.from_prebuilt(sample_docs, block)
        assert loaded.search("resize a widget", k=1)[0]["id"] == "resize-a-widget"

    def test_empty_corpus_round_trips(self):
        built, loaded = self.both([])
        assert len(loaded) == 0
        assert loaded.search("anything", k=5) == built.search("anything", k=5) == []

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda block: block.pop("weights"), id="missing field"),
            pytest.param(lambda block: block["positions"].pop(), id="short positions"),
            pytest.param(lambda block: block["lengths"].pop(), id="short lengths"),
            pytest.param(lambda block: block.update(norm_lengths=[]), id="empty norm_lengths"),
            # The shape fits and a value inside does not. Each of these loaded
            # before, then raised TypeError or IndexError from inside `search`
            # on every call; the negative position ranked the wrong document.
            pytest.param(lambda block: block.update(avg_length="12.5"), id="avg_length str"),
            pytest.param(lambda block: block.update(avg_length=None), id="avg_length null"),
            pytest.param(
                lambda block: block.update(lengths=[None] * len(block["lengths"])),
                id="lengths null",
            ),
            pytest.param(
                lambda block: block.update(norm_lengths=["1.0"] * len(block["norm_lengths"])),
                id="norm_lengths str",
            ),
            pytest.param(
                lambda block: block.update(weights=[["1"] * len(w) for w in block["weights"]]),
                id="weights str",
            ),
            pytest.param(
                lambda block: block.update(positions=[[999] * len(p) for p in block["positions"]]),
                id="position past the last document",
            ),
            pytest.param(
                lambda block: block.update(positions=[[-1] * len(p) for p in block["positions"]]),
                id="negative position",
            ),
            pytest.param(
                lambda block: block.update(positions=[[0.0] * len(p) for p in block["positions"]]),
                id="float position",
            ),
            pytest.param(
                lambda block: block.update(positions=[["0"] * len(p) for p in block["positions"]]),
                id="position str",
            ),
            pytest.param(lambda block: block["positions"][0].pop(), id="posting lists differ"),
            pytest.param(
                lambda block: block.update(terms=list(reversed(block["terms"]))),
                id="terms unsorted",
            ),
            pytest.param(
                lambda block: block.update(terms=[block["terms"][0]] * len(block["terms"])),
                id="terms repeated",
            ),
            pytest.param(
                lambda block: block.update(terms=list(range(len(block["terms"])))),
                id="terms not strings",
            ),
        ],
    )
    def test_a_block_that_does_not_fit_is_a_value_error(self, sample_docs, damage):
        """A ValueError, because that is what the transports map to "run sync"."""
        block = SearchIndex(sample_docs).prebuilt()
        damage(block)
        with pytest.raises(ValueError):
            SearchIndex.from_prebuilt(sample_docs, block)

    def test_a_checked_block_still_loads_and_serves(self, corpus):
        """The checks read every list once and change nothing."""
        block = json.loads(json.dumps(SearchIndex(corpus).prebuilt()))
        loaded = SearchIndex.from_prebuilt(corpus, block)
        assert loaded._terms is block["terms"]
        assert loaded.search("resize a widget", k=1)[0]["id"] == "resize-a-widget"

    def test_a_block_that_is_not_an_object_is_a_value_error(self, sample_docs):
        with pytest.raises(ValueError):
            SearchIndex.from_prebuilt(sample_docs, ["nope"])

    def test_a_block_for_other_documents_is_refused(self, sample_docs):
        block = SearchIndex(sample_docs).prebuilt()
        with pytest.raises(ValueError):
            SearchIndex.from_prebuilt(sample_docs[:2], block)
