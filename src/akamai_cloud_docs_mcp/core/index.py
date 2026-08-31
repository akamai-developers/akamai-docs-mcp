"""Read-only search over a loaded index.

A hand-rolled inverted index with TF-IDF term weights and BM25 length
normalization. It is built once from the loaded documents and never mutated, so
there are no locks and no background threads. Roughly 850 documents index in
about a quarter of a second natively.

That quarter second is paid on every request on Akamai Functions, where nothing
survives between calls, so `sync` also stores the build's output in the index
(`prebuilt()`) and a reader takes `from_prebuilt()` instead: one `json.loads`
and no tokenizing. Both paths hold the same numbers, so they rank the same.
Changing anything the build computes means bumping `INDEX_SCHEMA` in config.py.
"""

from __future__ import annotations

import bisect
import difflib
import math
import re
from dataclasses import dataclass

from .sections import snippet

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HEADING_LINE_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+)$", re.MULTILINE)

# Small and deliberate. Anything longer starts eating real query terms such as
# "no", "all", or "one" that appear in parameter names.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can",
        "do", "does", "for", "from", "had", "has", "have", "how", "i", "if",
        "in", "into", "is", "it", "its", "of", "on", "or", "that", "the",
        "their", "then", "there", "these", "they", "this", "to", "was", "were",
        "what", "when", "where", "which", "who", "will", "with", "you", "your",
    }
)  # fmt: skip

# BM25 constants. b is below the usual 0.75 because long Akamai guides are
# genuinely more informative than short ones, so length is punished gently.
_K1 = 1.2
_B = 0.6

# Length alone cannot tell a comprehensive guide from a long reference table.
# `configuration-audit-log-events` is 237 table rows over a 310 word vocabulary,
# so it repeats "create", "delete", and every product name enough to win queries
# it has no answer for. Its type-token ratio is 0.075 against a guide median of
# 0.365, so scaling its length by how repetitive it is compared to its own
# catalog moves it off the top without touching normal prose.
#
# Compared per kind, not corpus-wide: the 449 API cards are generated and terse,
# and judging hand-written prose against them tilts every guide down for a
# reason that has nothing to do with repetition. Measured on the 20 queries in
# scripts/rank_check.py, alpha 0.5 to 0.9 all leave guide MRR at 0.900 while
# API MRR climbs, and guides regress at 1.0. 0.75 sits in the middle of that.
_REPETITION_ALPHA = 0.75

_TITLE_WEIGHT = 3
_HEADING_WEIGHT = 2
_BODY_WEIGHT = 1

# A rare query term is also looked up as a prefix, to reach the canonical
# spelling. "postgres" stems to "postgre", which only 3 documents carry, while 21
# carry "postgresql".
#
# The rules are deliberately tight. Expanding a common word such as "list" or
# "plan" pulls in rare compounds like "listobjectsv", and because rare terms
# carry high idf they then outrank the documents the user actually wanted.
# So: only long terms, only rare ones, and prefer the *most* common expansion,
# which is the canonical spelling rather than an unrelated compound.
_MIN_PREFIX_LEN = 5
_EXPAND_MAX_DF = 8
_MAX_PREFIX_MATCHES = 3
_PREFIX_PENALTY = 0.6

# A title that matches the query exactly is almost always the right answer.
_TITLE_EXACT_BOOST = 1.8
_TITLE_SUBSTRING_BOOST = 1.35

# A query that is exactly a document's id must return that document first. The
# tool description tells the model to pass ids around verbatim, so re-searching
# an id it already holds is common. Stronger than the title boost on purpose:
# at 1.8, `GET /linode/instances` still loses to `GET /linode/instances/{linodeId}`
# because "Get a Linode" carries "get" at title weight and "List Linodes" does
# not, and a repetitive reference page stays held down by its length penalty.
# Measured on the cached index: 1.8 leaves 2 of 841 ids off rank 1, 3 leaves 0.
_ID_EXACT_BOOST = 3

# The prefix `build_search_text` puts on every API card. Search already shows
# the id and title as their own fields, so the snippet drops them.
_API_TEXT_PREFIX = "{id} {title} "


# A suggestion below this similarity is noise. The two cheap upper bounds in
# `_similarity` use the same cutoff, so nothing that could pass it is skipped.
_SUGGEST_MIN_RATIO = 0.3


def _similarity(needle: str, hay: str) -> float:
    """`SequenceMatcher.ratio()` with its two cheap upper bounds checked first.

    The full ratio costs about a quarter of a millisecond per pair, and the
    fetch tool runs it against every document for an id it does not know. A
    256-character id that nobody typed costs half a second of CPU that way,
    on an endpoint with no auth. `real_quick_ratio` bounds by length alone and
    `quick_ratio` by character multiset, so a long or unrelated needle is
    rejected without the alignment. Measured: 400 to 480 ms falls to under
    50 ms for the worst 256-character inputs, and short typos return the same
    suggestions as before. The needle is not truncated, because truncation
    changes which suggestions come back and it is the length bound that makes
    long needles cheap.
    """
    matcher = difflib.SequenceMatcher(None, needle, hay)
    if matcher.real_quick_ratio() <= _SUGGEST_MIN_RATIO:
        return 0.0
    if matcher.quick_ratio() <= _SUGGEST_MIN_RATIO:
        return 0.0
    return matcher.ratio()


def stem(token: str) -> str:
    """Crude plural folding so `clusters` matches `cluster`."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("sses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Lowercase alphanumeric runs, plural-folded, stopwords dropped.

    Splitting on non-alphanumerics means `/databases/postgresql/instances` and
    `linode-cli databases postgresql-create` tokenize the way a reader expects.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    if keep_stopwords:
        return [stem(token) for token in tokens]
    return [stem(token) for token in tokens if token not in _STOPWORDS]


def query_tokens(query: str) -> list[str]:
    """Tokens for a user query, falling back to stopwords-included when empty."""
    tokens = tokenize(query)
    if tokens:
        return tokens
    return tokenize(query, keep_stopwords=True)


def _ids(docs: list[dict]) -> dict[str, dict]:
    """Documents by id. The first of two documents with one id wins."""
    by_id: dict[str, dict] = {}
    for doc in docs:
        doc_id = doc.get("id", "")
        if doc_id and doc_id not in by_id:
            by_id[doc_id] = doc
    return by_id


def _snippet_source(doc: dict) -> str:
    """The text a search snippet is cut from.

    API cards start with their own id and title, which the result already
    carries as separate fields. Only that exact prefix is removed; a card built
    some other way is used as is.
    """
    text = doc.get("text", "")
    if doc.get("kind") == "api":
        prefix = _API_TEXT_PREFIX.format(id=doc.get("id", ""), title=doc.get("title", ""))
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


@dataclass
class _Doc:
    ref: dict
    #: Weighted token count. Whole, because every weight is a whole number.
    length: int
    #: `length` scaled by the repetition penalty. Only the BM25 norm uses it.
    norm_length: float = 0.0


#: A term's postings: the positions of the documents carrying it, in document
#: order, and the weight of the term in each. Two parallel lists rather than a
#: list of pairs so the block `json.loads` gives back is used as it is, with no
#: pass over the 94,000 entries to turn pairs into tuples.
_Postings = tuple[list[int], list[int]]

#: The fields `prebuilt()` writes and `from_prebuilt()` needs.
_PREBUILT_FIELDS = ("terms", "positions", "weights", "lengths", "norm_lengths", "avg_length")


def _check_prebuilt(prebuilt: dict, doc_count: int) -> None:
    """Refuse a block `search` could not use, with a ValueError every time.

    Length checks alone let a block with the right shape and the wrong values
    inside load fine: a string where a number belongs, a position past the
    last document. Those fail later, with a TypeError or an IndexError from
    inside the tool body, where no transport maps them to "run sync". Every
    check here is a whole-list operation in C, under 10 ms for the 94,000
    postings of the real index against a quarter second to build, and a
    TypeError from any of them becomes the ValueError the caller promises.
    """
    try:
        if any(key not in prebuilt for key in _PREBUILT_FIELDS):
            raise ValueError("the prebuilt search index is missing a field")
        terms, positions, weights = prebuilt["terms"], prebuilt["positions"], prebuilt["weights"]
        lengths, norm_lengths = prebuilt["lengths"], prebuilt["norm_lengths"]
        avg_length = prebuilt["avg_length"]
        if not (len(terms) == len(positions) == len(weights)):
            raise ValueError("the prebuilt search index has mismatched term lists")
        if not (len(lengths) == len(norm_lengths) == doc_count):
            raise ValueError("the prebuilt search index does not match the documents")
        # A bool passes isinstance(int); sync never writes one.
        if isinstance(avg_length, bool) or not isinstance(avg_length, (int, float)):
            raise TypeError("avg_length is not a number")
        # sum() refuses a string or a null among the numbers.
        sum(lengths)
        sum(norm_lengths)
        sum(map(sum, weights))
        # Positions index the document list, so they must be whole numbers in
        # range. A float among them makes the sum a float. A negative one
        # would count from the end and rank the wrong document without a
        # word.
        if not isinstance(sum(map(sum, positions)), int):
            raise TypeError("positions are not whole numbers")
        occupied = [posting for posting in positions if posting]
        if occupied and (max(map(max, occupied)) >= doc_count or min(map(min, occupied)) < 0):
            raise ValueError("the prebuilt search index points outside the documents")
        pairs = zip(positions, weights, strict=True)
        if any(len(found) != len(weighed) for found, weighed in pairs):
            raise ValueError("the prebuilt search index has mismatched postings")
        # expand_prefix bisects the terms, so they must be sorted strings with
        # no repeats.
        if not all(isinstance(term, str) for term in terms) or terms != sorted(set(terms)):
            raise ValueError("the prebuilt search index terms are not sorted")
    except TypeError as exc:
        raise ValueError("the prebuilt search index holds a value of the wrong type") from exc


class SearchIndex:
    """An immutable inverted index over guide and API documents."""

    def __init__(self, docs: list[dict]):
        self.docs: list[dict] = docs
        self.by_id: dict[str, dict] = _ids(docs)
        self._entries: list[_Doc] = []
        self._postings: dict[str, _Postings] = {}
        self._avg_length = 0.0
        self._terms: list[str] = []
        self._build()

    # -- build ----------------------------------------------------------------

    def _build(self) -> None:
        total_length = 0
        ratios: list[float] = []
        for position, doc in enumerate(self.docs):
            doc_id = doc.get("id", "")
            counts: dict[str, int] = {}
            for token in tokenize(doc.get("title", "")):
                counts[token] = counts.get(token, 0) + _TITLE_WEIGHT
            # Guide slugs such as `iam-aclp` carry words the title and text may
            # not, so the id is indexed as if it were a second title.
            for token in tokenize(doc_id):
                counts[token] = counts.get(token, 0) + _TITLE_WEIGHT
            text = doc.get("text", "")
            for heading in _HEADING_LINE_RE.findall(text):
                for token in tokenize(heading):
                    counts[token] = counts.get(token, 0) + _HEADING_WEIGHT - _BODY_WEIGHT
            body = tokenize(text)
            for token in body:
                counts[token] = counts.get(token, 0) + _BODY_WEIGHT

            length = sum(counts.values())
            total_length += length
            ratios.append((len(set(body)) / len(body)) if body else 1.0)
            self._entries.append(_Doc(ref=doc, length=length, norm_length=length))
            for token, weight in counts.items():
                positions, weights = self._postings.setdefault(token, ([], []))
                positions.append(position)
                weights.append(weight)

        self._avg_length = (total_length / len(self._entries)) if self._entries else 0.0
        self._apply_repetition_penalty(ratios)
        self._terms = sorted(self._postings)

    # -- prebuilt -------------------------------------------------------------

    def prebuilt(self) -> dict:
        """Everything `_build` computed, as plain JSON types.

        `sync` stores this in the index under `prebuilt`. Weights and lengths
        are whole numbers, and the floats (`norm_lengths`, `avg_length`) go
        through `json.dumps` and back unchanged, so an index loaded from this
        block scores every query the same as one built from the documents.
        """
        return {
            "terms": self._terms,
            "positions": [self._postings[term][0] for term in self._terms],
            "weights": [self._postings[term][1] for term in self._terms],
            "lengths": [entry.length for entry in self._entries],
            "norm_lengths": [entry.norm_length for entry in self._entries],
            "avg_length": self._avg_length,
        }

    @classmethod
    def from_prebuilt(cls, docs: list[dict], prebuilt: dict) -> SearchIndex:
        """An index over `docs` from the block `prebuilt()` wrote for them.

        Nothing is tokenized. The lists come straight out of `json.loads` and
        are kept as they are, so the cost is a dict over the terms and one
        `_Doc` per document. A block that does not fit `docs`, lacks a field,
        or holds a value `search` could not use is a `ValueError`: the
        transports map that to "the index file is not readable; run sync",
        and the Functions handler to a 503.
        """
        if not isinstance(prebuilt, dict):
            raise ValueError("the prebuilt search index is missing a field")
        _check_prebuilt(prebuilt, len(docs))
        terms, positions, weights = prebuilt["terms"], prebuilt["positions"], prebuilt["weights"]
        lengths, norm_lengths = prebuilt["lengths"], prebuilt["norm_lengths"]

        index = cls.__new__(cls)
        index.docs = docs
        index.by_id = _ids(docs)
        index._entries = [
            _Doc(ref=doc, length=length, norm_length=norm_length)
            for doc, length, norm_length in zip(docs, lengths, norm_lengths, strict=True)
        ]
        index._postings = dict(zip(terms, zip(positions, weights, strict=True), strict=True))
        index._avg_length = prebuilt["avg_length"]
        index._terms = terms
        return index

    def _apply_repetition_penalty(self, ratios: list[float]) -> None:
        """Scale each document's norm length by how repetitive it is for its kind.

        Comparison is within a kind, so a guide is measured against other guides.
        See `_REPETITION_ALPHA` for why.
        """
        medians: dict[str, float] = {}
        for kind in {entry.ref.get("kind", "guide") for entry in self._entries}:
            values = sorted(
                ratio
                for ratio, entry in zip(ratios, self._entries, strict=True)
                if entry.ref.get("kind", "guide") == kind
            )
            medians[kind] = values[len(values) // 2] if values else 1.0
        for ratio, entry in zip(ratios, self._entries, strict=True):
            if ratio <= 0:
                continue
            median = medians[entry.ref.get("kind", "guide")]
            entry.norm_length = entry.length * (median / ratio) ** _REPETITION_ALPHA

    # -- query ----------------------------------------------------------------

    def expand_prefix(self, term: str) -> list[str]:
        """Longer indexed terms starting with `term`, most common first.

        Returns nothing for a short or already-common term. See the constants
        above for why the rules are this tight.
        """
        if len(term) < _MIN_PREFIX_LEN:
            return []
        if self._document_frequency(term) > _EXPAND_MAX_DF:
            return []
        start = bisect.bisect_left(self._terms, term)
        matches: list[str] = []
        for candidate in self._terms[start:]:
            if not candidate.startswith(term):
                break
            if candidate != term:
                matches.append(candidate)
            if len(matches) > 50:
                break
        matches.sort(key=lambda candidate: (-self._document_frequency(candidate), len(candidate)))
        return matches[:_MAX_PREFIX_MATCHES]

    def _document_frequency(self, term: str) -> int:
        """How many documents carry `term`. Zero for one that is not indexed."""
        postings = self._postings.get(term)
        return len(postings[0]) if postings else 0

    def search(self, query: str, k: int = 5, *, kind: str = "") -> list[dict]:
        """Rank documents for `query`. Returns `{id, kind, title, snippet, score}`."""
        terms = query_tokens(query)
        if not terms or not self._entries:
            return []

        total_docs = len(self._entries)
        scores: dict[int, float] = {}
        matched_terms: dict[int, set[str]] = {}
        # Kept separate from matched_terms: snippets highlight the term actually
        # present in the text, coverage counts the query term the user typed.
        snippet_terms: dict[int, set[str]] = {}
        unique_terms = set(terms)

        for term in unique_terms:
            lookups = [(term, 1.0)] if self._postings.get(term) else []
            lookups += [
                (match, _PREFIX_PENALTY) for match in self.expand_prefix(term) if match != term
            ]
            for lookup, weight in lookups:
                postings = self._postings.get(lookup)
                if not postings:
                    continue
                positions, frequencies = postings
                count = len(positions)
                idf = math.log(1.0 + (total_docs - count + 0.5) / (count + 0.5))
                for position, frequency in zip(positions, frequencies, strict=True):
                    length = self._entries[position].norm_length
                    norm = 1.0 - _B + _B * (length / self._avg_length if self._avg_length else 1.0)
                    contribution = idf * (frequency * (_K1 + 1.0)) / (frequency + _K1 * norm)
                    scores[position] = scores.get(position, 0.0) + contribution * weight
                    # Credited to the query term, not the expansion, so query
                    # coverage below counts it once however many terms matched.
                    matched_terms.setdefault(position, set()).add(term)
                    snippet_terms.setdefault(position, set()).add(lookup)

        if not scores:
            return []

        # Reward documents that cover more of the query. Without this, one very
        # rare term drags in documents that ignore the rest of the question.
        for position, matched in matched_terms.items():
            coverage = len(matched) / len(unique_terms)
            scores[position] *= 0.5 + 0.5 * coverage

        lowered_query = query.strip().lower()
        if lowered_query:
            for position in scores:
                title = self.docs[position].get("title", "").lower().strip()
                if title == lowered_query:
                    scores[position] *= _TITLE_EXACT_BOOST
                elif lowered_query in title:
                    scores[position] *= _TITLE_SUBSTRING_BOOST
                # Not an elif: a document whose title and id both match keeps
                # both boosts.
                if self.docs[position].get("id", "").lower() == lowered_query:
                    scores[position] *= _ID_EXACT_BOOST

        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.docs[item[0]]["id"]))
        results: list[dict] = []
        for position, score in ranked:
            doc = self.docs[position]
            if kind and doc.get("kind") != kind:
                continue
            results.append(
                {
                    "id": doc["id"],
                    "kind": doc.get("kind", "guide"),
                    "title": doc.get("title", ""),
                    "snippet": snippet(_snippet_source(doc), sorted(snippet_terms[position])),
                    "score": round(score, 4),
                }
            )
            if len(results) >= k:
                break
        return results

    def get(self, doc_id: str) -> dict | None:
        """Fetch an indexed document by exact id."""
        return self.by_id.get(doc_id)

    def suggest(self, raw_id: str, n: int = 3) -> list[dict]:
        """Closest documents to an unrecognized id, for teaching errors."""
        needle = (raw_id or "").strip().lower()
        if not needle:
            return []
        scored: list[tuple[float, dict]] = []
        for doc in self.docs:
            doc_id = doc.get("id", "").lower()
            title = doc.get("title", "").lower()
            ratio = max(_similarity(needle, doc_id), _similarity(needle, title))
            if needle in doc_id or needle in title:
                ratio = max(ratio, 0.9)
            scored.append((ratio, doc))
        scored.sort(key=lambda item: (-item[0], item[1].get("id", "")))
        return [
            {"id": doc["id"], "title": doc.get("title", "")}
            for ratio, doc in scored[:n]
            if ratio > _SUGGEST_MIN_RATIO
        ]

    def __len__(self) -> int:
        return len(self._entries)
