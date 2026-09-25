"""Multi-candidate probe_model_id: readiness must not depend on one fixed
model happening to be the one currently loaded on a single-slot backend.

See omlx_app.py's OmlxAppAdapter docstring on _probe_model_ids for the bug
this guards against: a backend that holds only one model at a time rotates
which model is loaded, so a single fixed probe id was loaded only by
coincidence and the readiness probe silently skipped (never ran, reported
not-ready) for every other model almost all the time.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omlxc.adapters.omlx_app import OmlxAppAdapter


def _status_and_models(loaded_id: str) -> dict[str, httpx.Response]:
    return {
        "/api/status": httpx.Response(200, json={"status": "ok", "version": "0.5.7"}),
        "/v1/models": httpx.Response(200, json={"data": [{"id": loaded_id, "loaded": True}]}),
        "/v1/models/status": httpx.Response(404),
    }


@pytest.mark.asyncio
async def test_probes_whichever_candidate_is_currently_loaded() -> None:
    requests: list[httpx.Request] = []
    routes = _status_and_models("coding-next")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "O"}}]})
        return routes[request.url.path]

    adapter = OmlxAppAdapter(
        backend_id="mbp-omlx",
        base_url="http://omlx.invalid",
        probe_model_id=frozenset({"coding", "coding-next", "reasoning"}),
        transport=httpx.MockTransport(handler),
    )

    snapshot = await adapter.discover()  # type: ignore[attr-defined]

    assert snapshot.generation_ready is True
    probe_requests = [r for r in requests if r.url.path == "/v1/chat/completions"]
    assert len(probe_requests) == 1


@pytest.mark.asyncio
async def test_does_not_probe_a_loaded_model_outside_the_candidate_set() -> None:
    """The model actually loaded (e.g. a mistagged reranker) isn't a chat
    candidate, so the probe must be skipped rather than sent to it -- sending
    it would itself fail (or worse, hang) against a non-chat model."""
    requests: list[httpx.Request] = []
    routes = _status_and_models("baai-bge-reranker-v2-m3-mlx-fp16")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/chat/completions":
            raise AssertionError("must not probe a model outside the candidate set")
        return routes[request.url.path]

    adapter = OmlxAppAdapter(
        backend_id="mbp-omlx",
        base_url="http://omlx.invalid",
        probe_model_id=frozenset({"coding", "coding-next", "reasoning"}),
        transport=httpx.MockTransport(handler),
    )

    snapshot = await adapter.discover()  # type: ignore[attr-defined]

    assert snapshot.generation_ready is False
    assert snapshot.model_available is True


@pytest.mark.asyncio
async def test_single_string_probe_model_id_still_supported() -> None:
    """Backward compatibility: a bare string keeps behaving as one candidate."""
    routes = _status_and_models("model-a")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "O"}}]})
        return routes[request.url.path]

    adapter = OmlxAppAdapter(
        backend_id="mbp-omlx",
        base_url="http://omlx.invalid",
        probe_model_id="model-a",
        transport=httpx.MockTransport(handler),
    )

    snapshot = await adapter.discover()  # type: ignore[attr-defined]

    assert snapshot.generation_ready is True


@pytest.mark.asyncio
async def test_retries_with_reasoning_budget_after_empty_minimal_probe() -> None:
    routes = _status_and_models("reasoning")
    probe_tokens: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            payload = json.loads(request.content)
            probe_tokens.append(payload["max_tokens"])
            content = "O" if payload["max_tokens"] == 100 else ""
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return routes[request.url.path]

    adapter = OmlxAppAdapter(
        backend_id="mbp-omlx",
        base_url="http://omlx.invalid",
        probe_model_id="reasoning",
        transport=httpx.MockTransport(handler),
    )

    snapshot = await adapter.discover()  # type: ignore[attr-defined]

    assert snapshot.generation_ready is True
    assert probe_tokens == [1, 100]
