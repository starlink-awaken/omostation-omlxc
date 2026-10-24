from __future__ import annotations

from dataclasses import dataclass

from omlxc.dataplane.models import RerankRequest, RerankResult
from omlxc.dataplane.reranker import RerankEngine
from omlxc.domain import RouteProfile


@dataclass(slots=True)
class LocalReranker:
    """Adapt the process-local MLX engine to the data-plane protocol."""

    engine: RerankEngine
    placement_id: str = "local-rerank"
    backend_id: str = "mlx-local"

    async def rerank(self, request: RerankRequest) -> RerankResult:
        result = self.engine.rerank(request.query, list(request.documents), top_k=len(request.documents))
        ranking = result["ranking"]
        scores_by_index = dict(zip(ranking, result["scores"], strict=True))
        scores = tuple(scores_by_index[index] for index in range(len(request.documents)))
        return RerankResult(
            scores=scores,
            placement_id=self.placement_id,
            backend_id=self.backend_id,
            profile=RouteProfile.QUALITY,
        )
