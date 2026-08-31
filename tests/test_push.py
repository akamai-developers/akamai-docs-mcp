"""The chunked upload client for Akamai Functions. No network."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json

import pytest

from akamai_cloud_docs_mcp.sync import (
    PUSH_ATTEMPTS,
    SyncError,
    TransientError,
    _admin_url,
    push_index,
)


@pytest.fixture
def index() -> dict:
    # Deterministic filler, sized so the fixture spans several chunks.
    import hashlib

    def filler(seed: int) -> str:
        out = []
        for step in range(12):
            out.append(hashlib.sha256(f"{seed}-{step}".encode()).hexdigest())
        return " ".join(out)

    return {
        "schema": 1,
        "built_at": "2026-08-13T00:00:00Z",
        "sources": {},
        "docs": [
            {"id": f"doc-{n}", "kind": "guide", "title": f"Doc {n}", "text": filler(n)}
            for n in range(40)
        ],
    }


@pytest.fixture
def recorder():
    """A stand-in for the admin endpoint that records what it was sent."""
    calls: list[dict] = []

    def poster(url: str, body: bytes, headers: dict) -> tuple[int, str]:
        calls.append({"url": url, "body": json.loads(body), "headers": headers})
        return 200, '{"ok":true}'

    poster.calls = calls
    return poster


def flaky(answers: list):
    """A poster that returns, or raises, each queued answer in turn, then 200."""
    calls: list[dict] = []

    def poster(url: str, body: bytes, headers: dict) -> tuple[int, str]:
        calls.append(json.loads(body))
        if answers:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return 200, '{"ok":true}'

    poster.calls = calls
    return poster


def no_sleep(seconds: float) -> None:
    no_sleep.delays.append(seconds)


no_sleep.delays = []


class TestAdminUrl:
    def test_appends_the_route(self):
        assert _admin_url("https://app.example.com") == "https://app.example.com/admin/sync"

    def test_strips_a_trailing_slash(self):
        assert _admin_url("https://app.example.com/") == "https://app.example.com/admin/sync"

    def test_allows_http_only_for_localhost(self):
        assert _admin_url("http://127.0.0.1:3000").startswith("http://127.0.0.1:3000")
        assert _admin_url("http://localhost:3000").startswith("http://localhost:3000")

    @pytest.mark.parametrize(
        "url", ["http://example.com", "ftp://example.com", "example.com", "", "https://"]
    )
    def test_rejects(self, url):
        with pytest.raises(SyncError):
            _admin_url(url)


class TestPushIndex:
    def test_uploads_chunks_then_publishes(self, index, recorder):
        meta = push_index(index, "https://app.example.com", "t0ken", quiet=True, poster=recorder)
        bodies = [call["body"] for call in recorder.calls]
        assert all("chunk" in body for body in bodies[:-1])
        assert "finish" in bodies[-1]
        assert meta["chunks"] == len(bodies) - 1

    def test_meta_is_written_last(self, index, recorder):
        push_index(index, "https://app.example.com", "t0ken", quiet=True, poster=recorder)
        # A reader must see either the old index or the new one, never a mix.
        assert "finish" not in recorder.calls[0]["body"]
        assert "finish" in recorder.calls[-1]["body"]

    def test_chunks_reassemble_into_the_original_index(self, index, recorder):
        push_index(
            index, "https://app.example.com", "t0ken", chunk_bytes=4096, quiet=True, poster=recorder
        )
        chunks = [
            base64.b64decode(call["body"]["data"])
            for call in recorder.calls
            if "chunk" in call["body"]
        ]
        assert len(chunks) > 1, "the fixture should span several chunks"
        assert json.loads(b"".join(chunks)) == index

    def test_meta_records_the_byte_count(self, index, recorder):
        meta = push_index(
            index, "https://app.example.com", "t0ken", chunk_bytes=4096, quiet=True, poster=recorder
        )
        sent = sum(
            len(base64.b64decode(call["body"]["data"]))
            for call in recorder.calls
            if "chunk" in call["body"]
        )
        assert meta["bytes"] == sent
        assert meta["documents"] == len(index["docs"])
        assert meta["built_at"] == index["built_at"]

    def test_bearer_token_is_sent(self, index, recorder):
        push_index(index, "https://app.example.com", "t0ken", quiet=True, poster=recorder)
        assert recorder.calls[0]["headers"]["Authorization"] == "Bearer t0ken"

    def test_missing_token_is_refused_before_any_request(self, index, recorder):
        with pytest.raises(SyncError, match="no sync token"):
            push_index(index, "https://app.example.com", "", quiet=True, poster=recorder)
        assert recorder.calls == []

    def test_a_rejected_publish_is_reported(self, index):
        poster = flaky([(200, "ok"), (400, '{"error":"bytes do not add up"}')])
        with pytest.raises(SyncError, match="publish rejected with HTTP 400"):
            push_index(index, "https://app.example.com", "t0ken", quiet=True, poster=poster)

    def test_a_rejected_chunk_stops_the_push(self, index):
        calls: list[dict] = []

        def poster(url: str, body: bytes, headers: dict) -> tuple[int, str]:
            calls.append(json.loads(body))
            return 401, '{"error":"unauthorized"}'

        with pytest.raises(SyncError, match="HTTP 401"):
            push_index(index, "https://app.example.com", "bad", quiet=True, poster=poster)
        assert len(calls) == 1, "it should not keep uploading after a rejection"


class TestWireProtocol:
    """The body shapes functions/app.py accepts. Both sides code to this."""

    def test_every_chunk_body_has_the_v2_fields(self, index, recorder):
        push_index(
            index, "https://app.example.com", "t0ken", chunk_bytes=4096, quiet=True, poster=recorder
        )
        for call in recorder.calls[:-1]:
            assert set(call["body"]) == {"generation", "chunk", "data"}
            assert isinstance(call["body"]["chunk"], int)
            # base64 of raw JSON bytes, strict alphabet, so the app can decode
            # with validate=True.
            base64.b64decode(call["body"]["data"], validate=True)

    def test_generation_is_32_hex_and_the_same_across_one_push(self, index, recorder):
        push_index(
            index, "https://app.example.com", "t0ken", chunk_bytes=4096, quiet=True, poster=recorder
        )
        generations = {
            call["body"].get("generation") or call["body"]["finish"]["generation"]
            for call in recorder.calls
        }
        assert len(generations) == 1
        generation = generations.pop()
        assert len(generation) == 32
        assert generation == generation.lower()
        int(generation, 16)

    def test_two_pushes_get_different_generations(self, index, recorder):
        first = push_index(index, "https://app.example.com", "t0ken", quiet=True, poster=recorder)
        second = push_index(index, "https://app.example.com", "t0ken", quiet=True, poster=recorder)
        assert first["generation"] != second["generation"]

    def test_finish_body_matches_the_contract(self, index, recorder):
        meta = push_index(
            index, "https://app.example.com", "t0ken", chunk_bytes=4096, quiet=True, poster=recorder
        )
        finish = recorder.calls[-1]["body"]
        assert set(finish) == {"finish"}
        assert finish["finish"] == meta
        assert set(meta) == {
            "generation",
            "chunks",
            "bytes",
            "sha256",
            "built_at",
            "documents",
            "schema",
            "encoding",
        }
        assert meta["chunks"] == len(recorder.calls) - 1
        assert meta["encoding"] == "json"
        assert meta["schema"] == index["schema"]

    def test_sha256_covers_the_reassembled_payload(self, index, recorder):
        meta = push_index(
            index, "https://app.example.com", "t0ken", chunk_bytes=4096, quiet=True, poster=recorder
        )
        payload = b"".join(
            base64.b64decode(call["body"]["data"])
            for call in recorder.calls
            if "chunk" in call["body"]
        )
        assert meta["sha256"] == hashlib.sha256(payload).hexdigest()
        assert meta["bytes"] == len(payload)

    def test_chunk_numbers_are_dense_from_zero(self, index, recorder):
        push_index(
            index, "https://app.example.com", "t0ken", chunk_bytes=4096, quiet=True, poster=recorder
        )
        numbers = [call["body"]["chunk"] for call in recorder.calls if "chunk" in call["body"]]
        assert numbers == list(range(len(numbers)))


class TestRetry:
    def setup_method(self):
        no_sleep.delays.clear()

    def test_a_503_is_retried_once_and_the_push_succeeds(self, index):
        poster = flaky([(503, "busy")])
        meta = push_index(
            index, "https://app.example.com", "t0ken", quiet=True, poster=poster, sleep=no_sleep
        )
        assert meta["chunks"] == 1
        # Two calls for chunk 0, one for the finish.
        assert len(poster.calls) == 3
        assert poster.calls[0] == poster.calls[1]
        assert "finish" in poster.calls[2]
        assert no_sleep.delays == [1.0]

    def test_a_dropped_connection_is_retried(self, index):
        poster = flaky([TransientError("could not reach")])
        push_index(
            index, "https://app.example.com", "t0ken", quiet=True, poster=poster, sleep=no_sleep
        )
        assert len(poster.calls) == 3

    def test_gives_up_after_the_last_attempt(self, index):
        poster = flaky([(502, "bad gateway")] * PUSH_ATTEMPTS)
        with pytest.raises(SyncError, match="HTTP 502"):
            push_index(
                index, "https://app.example.com", "t0ken", quiet=True, poster=poster, sleep=no_sleep
            )
        assert len(poster.calls) == PUSH_ATTEMPTS
        assert no_sleep.delays == [1.0, 2.0]

    def test_a_connection_that_never_answers_is_the_final_error(self, index):
        poster = flaky([TransientError("could not reach x")] * PUSH_ATTEMPTS)
        with pytest.raises(TransientError, match="could not reach"):
            push_index(
                index, "https://app.example.com", "t0ken", quiet=True, poster=poster, sleep=no_sleep
            )
        assert len(poster.calls) == PUSH_ATTEMPTS

    @pytest.mark.parametrize(
        "error",
        [TimeoutError("timed out"), http.client.RemoteDisconnected("closed")],
        ids=["hang-after-request", "closed-after-request"],
    )
    def test_a_hang_after_the_request_is_retried_through_the_real_client(
        self, index, monkeypatch, capsys, error
    ):
        """The default poster, with the opener replaced by the bare exception
        urllib raises when the server accepts the request and never answers.

        No poster stand-in here: the point is that _post_json itself turns
        this into a TransientError so the retry loop sees it.
        """
        import urllib.request

        attempts: list = []

        def never_answers(self, request, timeout=None):
            attempts.append(timeout)
            raise error

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", never_answers)
        with pytest.raises(TransientError, match="could not reach") as exc:
            push_index(index, "https://app.example.com", "t0ken", sleep=no_sleep)
        assert str(error) in str(exc.value)
        assert len(attempts) == PUSH_ATTEMPTS
        assert no_sleep.delays == [1.0, 2.0]
        err = capsys.readouterr().err
        assert "chunk 0: could not reach https://app.example.com/admin/sync" in err
        assert "retrying in 1s (1/3)" in err
        assert "t0ken" not in err

    def test_a_4xx_is_never_retried(self, index):
        poster = flaky([(413, "too large")])
        with pytest.raises(SyncError, match="HTTP 413"):
            push_index(
                index, "https://app.example.com", "t0ken", quiet=True, poster=poster, sleep=no_sleep
            )
        assert len(poster.calls) == 1
        assert no_sleep.delays == []

    def test_a_redirect_error_is_never_retried(self, index):
        # A plain SyncError from the poster is a decision, not a transient
        # failure: the redirect refusal must not resend the token.
        poster = flaky([SyncError("app URL redirected")])
        with pytest.raises(SyncError, match="redirected"):
            push_index(
                index, "https://app.example.com", "t0ken", quiet=True, poster=poster, sleep=no_sleep
            )
        assert len(poster.calls) == 1

    def test_the_finish_request_is_retried_too(self, index):
        poster = flaky([(200, "ok"), (500, "oops")])
        push_index(
            index, "https://app.example.com", "t0ken", quiet=True, poster=poster, sleep=no_sleep
        )
        finishes = [call for call in poster.calls if "finish" in call]
        assert len(finishes) == 2
        assert finishes[0] == finishes[1]

    def test_retry_is_logged_when_not_quiet(self, index, capsys):
        poster = flaky([(503, "busy")])
        push_index(index, "https://app.example.com", "t0ken", poster=poster, sleep=no_sleep)
        err = capsys.readouterr().err
        assert "chunk 0: HTTP 503; retrying in 1s (1/3)" in err
        assert "t0ken" not in err
