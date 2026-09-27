"""CatalogProbe 的 backend 级探测预算(probe timeout)此前被硬 clamp 到 5.0s
(min(max(interval, 0.1), 5.0)), 而 LM Studio 后端的一次完整探测在结构上
就超过这个数: SSH 控制通道往返 (discover 与 list_models 各调一次
_list_models_with_control, 实测 ~0.5s/次) + 就绪探测 chat 的独立超时
_PROBE_CHAT_TIMEOUT=5.0s。数学上 6s+ 永远撞破 5s 总限 → backend 级
asyncio.timeout 先于 probe chat 自己的超时掐死整个探测 → _fail_stale →
backend 全部 placement fresh=False + available=False。

实际症状 (2026-08-24): qwythos-9b 处于 loaded 态时, mbp 本机 LM Studio
backend 的 placement 全部持续 stale, full-status 报"全灭"误报, 路由也
永远不敢选 LM Studio 兜底 —— 即使模型实际可用且 SSH 控制通道 0.5s 内
就能返回完整状态。

修复: 探测预算取 max(5.0, probe_interval_seconds) —— 生产 interval=10s
下预算 10s, 覆盖 SSH×2 + probe chat 5s 后仍有余量; interval 极小时保底
5s, 不回退到比单次 probe chat 更紧的值。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from omlxc.config import (
    AppConfig,
    BackendConfig,
    DaemonConfig,
    ModelConfig,
    NodeConfig,
    PlacementConfig,
    StorageConfig,
)
from omlxc.daemon.composition import CatalogProbe, SnapshotCatalog, _configured_snapshots
from omlxc.domain import BackendKind
from omlxc.domain.protocols import (
    AdapterCapability,
    CapabilitySnapshot,
    ModelRuntime,
    ModelRuntimeState,
)


def _config(root: Path, interval: float) -> AppConfig:
    return AppConfig(
        daemon=DaemonConfig(socket_path=root / "omlxcd.sock", probe_interval_seconds=interval),
        storage=StorageConfig(database_path=root / "state.db"),
        nodes=(NodeConfig(id="local-node", display_name="Local", platform="macos", memory_gb=16),),
        backends=(
            BackendConfig(
                id="lm-backend",
                node_id="local-node",
                kind=BackendKind.LM_STUDIO,
                base_url="http://127.0.0.1:1234",
            ),
        ),
        models=(ModelConfig(id="m1", category="llm", role="chat", engine="omlx"),),
        placements=(
            PlacementConfig(
                id="p1",
                model_id="m1",
                backend_id="lm-backend",
                backend_model_id="lm-model",
                context_limit=8192,
            ),
        ),
    )


class SlowAdapter:
    """模拟 LM Studio 后端: discover 耗时超过旧 clamp 预算但仍在 fresh
    观测窗口内 —— interval=0.5 时旧预算=0.5s、fresh 窗口=max(1.0, 0.5*2)=1.0s,
    delay=0.6s 恰好落在"旧预算掐死 / 新预算(保底5s)放行"的分界带上。
    注意 fresh 窗口按 observed_at(discover 打点)到 _apply 的 age 判定,
    list_models 不再额外 sleep, 避免污染 age。"""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def discover(self) -> CapabilitySnapshot:
        await asyncio.sleep(self._delay)
        return CapabilitySnapshot(
            backend_id="lm-backend",
            reachable=True,
            compatible=True,
            model_available=True,
            generation_ready=True,
            observed_at=datetime.now(UTC),
            protocol_version="openai-v1",
            capabilities=frozenset({AdapterCapability.CHAT}),
            errors=(),
        )

    async def list_models(self) -> tuple[ModelRuntime, ...]:
        await asyncio.sleep(self._delay)
        return (ModelRuntime(id="lm-model", state=ModelRuntimeState.AVAILABLE, loaded=False),)


def test_probe_survives_discovery_slower_than_old_clamp(tmp_path: Path) -> None:
    """interval=0.5 时旧预算=0.5s: 1s 的探测会被整体掐死误判 stale;
    新预算保底 5s, 同样的探测必须完整落地为 fresh+available。"""
    config = _config(tmp_path, interval=0.5)
    catalog = SnapshotCatalog(_configured_snapshots(config), now=lambda: datetime.now(UTC))
    probe = CatalogProbe(
        config=config,
        adapters={"lm-backend": SlowAdapter(delay=0.6)},
        catalog=catalog,
        tailscale=None,
        now=lambda: datetime.now(UTC),
    )

    asyncio.run(probe.refresh_backend("lm-backend"))

    snapshot = catalog.get_one("p1")
    assert snapshot.fresh is True, f"探测 1s 未超新预算, 不应 stale: {snapshot}"
    assert snapshot.available is True


def test_probe_budget_tracks_production_interval(tmp_path: Path) -> None:
    """生产 interval=10s: 预算必须 >= interval, 覆盖 SSH×2 + probe chat 5s
    的结构性开销 (2*0.5 + 5.0 = 6s < 10s)。"""
    config = _config(tmp_path, interval=10.0)
    catalog = SnapshotCatalog(_configured_snapshots(config), now=lambda: datetime.now(UTC))
    probe = CatalogProbe(
        config=config,
        adapters={},
        catalog=catalog,
        tailscale=None,
        now=lambda: datetime.now(UTC),
    )

    assert probe._timeout >= 10.0


class FlakyAdapter(SlowAdapter):
    """前 hangs 次 discover 卡死(模拟 omlxcd 事件循环被饿导致的超时), 之后正常。"""

    def __init__(self, hangs: int) -> None:
        super().__init__(delay=0.0)
        self.hangs = hangs
        self.calls = 0

    async def discover(self) -> CapabilitySnapshot:
        self.calls += 1
        if self.calls <= self.hangs:
            await asyncio.sleep(3600)
        return await super().discover()


def _probe(tmp_path: Path, adapter: SlowAdapter) -> tuple[CatalogProbe, SnapshotCatalog]:
    config = _config(tmp_path, interval=0.5)
    catalog = SnapshotCatalog(_configured_snapshots(config), now=lambda: datetime.now(UTC))
    probe = CatalogProbe(
        config=config,
        adapters={"lm-backend": adapter},
        catalog=catalog,
        tailscale=None,
        now=lambda: datetime.now(UTC),
    )
    probe._timeout = 0.2
    return probe, catalog


def test_single_probe_timeout_is_retried_not_marked_stale(tmp_path: Path) -> None:
    """一次超时(探测方自身被饿)不应把 placement 打成 stale: 立即重试一次。"""
    adapter = FlakyAdapter(hangs=1)
    probe, catalog = _probe(tmp_path, adapter)

    asyncio.run(probe.refresh_backend("lm-backend"))

    assert adapter.calls == 2
    snapshot = catalog.get_one("p1")
    assert snapshot.fresh is True and snapshot.available is True


def test_persistent_probe_timeout_still_marks_stale(tmp_path: Path) -> None:
    """后端真挂(连续超时): 重试后仍判 stale, 不掩盖故障。"""
    adapter = FlakyAdapter(hangs=99)
    probe, catalog = _probe(tmp_path, adapter)

    asyncio.run(probe.refresh_backend("lm-backend"))

    assert adapter.calls == 2
    snapshot = catalog.get_one("p1")
    assert snapshot.fresh is False and snapshot.available is False
