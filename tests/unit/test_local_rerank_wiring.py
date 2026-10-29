from __future__ import annotations

from typing import cast

import httpx
import pytest

from omlxc.api import create_app
from omlxc.daemon import build_production_daemon
from omlxc.daemon.local_reranker import LocalReranker
from omlxc.dataplane import ExecutionError, ExecutionErrorCode, RerankExecution
from omlxc.dataplane.models import RerankRequest
from omlxc.dataplane.reranker import RerankEngine
from omlxc.domain import RouteProfile


class StubReranker:
    def __init__(self, result: RerankExecution | None = None) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def rerank(self, request_id: str, query: str, documents: tuple[str, ...]) -> RerankExecution:
        self.calls.append((query, documents))
        if self.result is not None:
            return self.result
        return RerankExecution(
            request_id,
            ((1, 0.9), (0, 0.1)),
            placement_id="local-rerank",
            backend_id="mlx-local",
            profile=RouteProfile.QUALITY,
        )


def test_rerank_engine_exposes_protocol_shape() -> None:
    engine = RerankEngine(model_path="/missing/model", allow_download=False)

    assert engine.backend == "uninitialized"
    assert engine.fallback_reason == "model unavailable"


@pytest.mark.asyncio
async def test_unwired_reranker_is_unsupported(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(inference=cast(object, _Inference(ExecutionErrorCode.UNSUPPORTED, "reranker_unset")))
    with caplog.at_level("WARNING"):
        response = await _post_rerank(app)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "unsupported_feature"
    assert "reranker_unset" in caplog.text


@pytest.mark.asyncio
async def test_failed_reranker_is_backend_failure(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(inference=cast(object, _Inference(ExecutionErrorCode.BACKEND_FAILURE, "reranker_call_failed")))
    with caplog.at_level("WARNING"):
        response = await _post_rerank(app)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "E300"
    assert "reranker_call_failed" in caplog.text


def test_production_daemon_builds_local_reranker(tmp_path) -> None:
    config = _config(tmp_path)
    composition = build_production_daemon(config)

    assert composition.inference._reranker is not None


@pytest.mark.asyncio
async def test_local_reranker_preserves_document_order_scores() -> None:
    class StubEngine:
        backend = "mlx-local"
        fallback_reason = None

        def rerank(self, query: str, docs: list[str], top_k: int = 50) -> dict[str, object]:
            del query, top_k
            ranking = list(reversed(range(len(docs))))
            scores = [float(index) / 10 for index in range(len(docs))]
            return {
                "ranking": ranking,
                "scores": [scores[index] for index in ranking],
            }

    documents = tuple(f"document-{index}" for index in range(7))
    result = await LocalReranker(StubEngine()).rerank(RerankRequest("rerank-1", "query", documents))

    assert result.scores == tuple(float(index) / 10 for index in range(7))


def test_explicit_reranker_does_not_initialize_default_engine(tmp_path, monkeypatch) -> None:
    def fail_init(self):
        raise AssertionError("default rerank engine should not be built")

    monkeypatch.setattr(RerankEngine, "__init__", fail_init)
    composition = build_production_daemon(_config(tmp_path), reranker=StubReranker())

    assert isinstance(composition.inference._reranker, StubReranker)


async def _post_rerank(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://omlxc") as client:
        return await client.post(
            "/api/v1/rerank",
            json={"model": "rerank", "query": "q", "documents": ["a", "b"]},
        )


class _Inference:
    def __init__(self, code: ExecutionErrorCode, reason: str) -> None:
        self.code = code
        self.reason = reason

    async def rerank(self, *, request_id: str, query: str, documents: tuple[str, ...]) -> RerankExecution:
        del query, documents
        return RerankExecution(
            request_id,
            (),
            ExecutionError(self.code, False, reason=self.reason),
        )


def _config(tmp_path):
    from omlxc.config import AppConfig, DaemonConfig, StorageConfig

    return AppConfig(
        daemon=DaemonConfig(socket_path=tmp_path / "daemon.sock", probe_interval_seconds=60.0),
        storage=StorageConfig(database_path=tmp_path / "state.db"),
    )
