"""Reconciliation of configured placements against a backend's real inventory.

A placement whose backend_model_id no longer exists on the backend stays visible
in config and in the routing catalog, so it keeps getting selected and every
operation on it fails with a generic error. The daemon surfaced this only as an
inventory count delta, which does not name the affected placements.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from omlxc.diagnostics import _placement_inventory_check


@dataclass
class _Backend:
    id: str


@dataclass
class _Placement:
    id: str
    backend_id: str
    backend_model_id: str


@dataclass
class _Model:
    id: str


class _Config:
    def __init__(self, placements: list[_Placement]) -> None:
        self.placements = placements


class _Adapter:
    def __init__(self, served: list[str] | None = None, *, fail: bool = False) -> None:
        self._served = served or []
        self._fail = fail

    async def list_models(self) -> tuple[_Model, ...]:
        if self._fail:
            raise RuntimeError("backend unreachable")
        return tuple(_Model(id=item) for item in self._served)


BACKEND = _Backend(id="mbp-omlx-app")


def _placement(placement_id: str, backend_model_id: str) -> _Placement:
    return _Placement(id=placement_id, backend_id=BACKEND.id, backend_model_id=backend_model_id)


@pytest.mark.asyncio
async def test_drifted_placements_are_named_not_just_counted():
    config = _Config(
        [
            _placement("mythos-local", "mythos"),
            _placement("mythos-fast-local", "mythos-fast"),
            _placement("coding-fast-local", "coding-fast"),
        ]
    )
    adapter = _Adapter(["mythos", "coding"])

    result = await _placement_inventory_check(config, BACKEND, adapter)

    assert result["ok"] is False
    assert result["placements"] == ["coding-fast-local", "mythos-fast-local"]
    assert "2/3" in result["detail"]


@pytest.mark.asyncio
async def test_fully_reconciled_backend_passes():
    config = _Config([_placement("mythos-local", "mythos"), _placement("coding-local", "coding")])
    adapter = _Adapter(["mythos", "coding", "vision"])

    result = await _placement_inventory_check(config, BACKEND, adapter)

    assert result["ok"] is True
    assert "2 placements reconciled" in result["detail"]


@pytest.mark.asyncio
async def test_backend_with_no_placements_is_not_a_failure():
    result = await _placement_inventory_check(_Config([]), BACKEND, _Adapter(["mythos"]))

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_unreadable_inventory_is_reported_rather_than_read_as_drift():
    config = _Config([_placement("mythos-local", "mythos")])

    result = await _placement_inventory_check(config, BACKEND, _Adapter(fail=True))

    assert result["ok"] is False
    assert result["detail"] == "inventory unavailable"
    assert "placements" not in result
