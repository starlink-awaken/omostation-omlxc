"""Placement selection for load/unload dispatch.

A model with placements on several backends used to resolve to whichever one came
first in catalog order, so an unload could be dispatched to a backend that did not
hold the model: the job failed and the memory stayed allocated on the real one.
"""

from __future__ import annotations

from omlxc.daemon.composition import ProductionControlService
from omlxc.scheduler import PlacementSnapshot


def _snapshot(
    placement_id: str,
    *,
    backend_id: str,
    loaded: bool,
    available: bool = True,
) -> PlacementSnapshot:
    return PlacementSnapshot(
        placement_id=placement_id,
        model_id="mythos-fast",
        backend_id=backend_id,
        backend_model_id="physical/model",
        node_id="mbp-m5-max-128g",
        fresh=True,
        available=available,
        authorized=True,
        capabilities=frozenset({"chat"}),
        context_limit=8192,
        memory_admitted=True,
        loaded=loaded,
        ttft_ms=10,
        throughput_tps=10,
        queue_depth=0,
        error_rate=0,
        network_cost_ms=0,
        affinity=1,
        available_concurrency=1,
        local=True,
        security_allowed=True,
    )


class _Catalog:
    def __init__(self, snapshots: tuple[PlacementSnapshot, ...]) -> None:
        self._snapshots = snapshots

    def get(self) -> tuple[PlacementSnapshot, ...]:
        return self._snapshots


def _service(*snapshots: PlacementSnapshot) -> ProductionControlService:
    """Build only the two attributes placement selection reads."""
    service = object.__new__(ProductionControlService)
    service._catalog = _Catalog(snapshots)  # type: ignore[assignment]
    service._model_aliases = {}
    return service


def test_unload_targets_the_backend_that_holds_the_model() -> None:
    idle = _snapshot("mythos-fast--lm_studio", backend_id="lm_studio", loaded=False)
    resident = _snapshot("mythos-fast-local", backend_id="omlx_app", loaded=True)

    chosen = _service(idle, resident)._placement_for_model("mythos-fast", prefer_loaded=True)

    assert chosen.placement_id == "mythos-fast-local"


def test_load_prefers_a_backend_that_does_not_already_hold_the_model() -> None:
    resident = _snapshot("mythos-fast-local", backend_id="omlx_app", loaded=True)
    idle = _snapshot("mythos-fast--lm_studio", backend_id="lm_studio", loaded=False)

    chosen = _service(resident, idle)._placement_for_model("mythos-fast", prefer_loaded=False)

    assert chosen.placement_id == "mythos-fast--lm_studio"


def test_selection_keeps_catalog_order_when_no_placement_matches_the_preference() -> None:
    first = _snapshot("first", backend_id="lm_studio", loaded=False)
    second = _snapshot("second", backend_id="ollama", loaded=False)

    chosen = _service(first, second)._placement_for_model("mythos-fast", prefer_loaded=True)

    assert chosen.placement_id == "first"


def test_eligible_placements_win_over_ineligible_ones() -> None:
    unavailable = _snapshot("unavailable", backend_id="lm_studio", loaded=False, available=False)
    healthy = _snapshot("healthy", backend_id="omlx_app", loaded=False)

    chosen = _service(unavailable, healthy)._placement_for_model("mythos-fast")

    assert chosen.placement_id == "healthy"


def test_model_without_any_placement_is_rejected() -> None:
    import pytest

    with pytest.raises(KeyError):
        _service()._placement_for_model("mythos-fast")
