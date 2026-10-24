"""加载侧的上下文：**永远**带 ``-c``，而且是**逐模型**算出来的（§5.70）。

⚠⚠⚠ 为什么有这份文件（§5.70 实测，measured_from 2026-10-24 08:12 · MBP）：

1. **机制本身是有效的**：声明了 ``context_limit`` 的落点，加载时真的把
   ``-c`` 传给了 ``lms load``（实测 ``lms ps`` 显示 ``contextLength: 32768``）。
2. **但几乎没人声明**：33 条落点里只有 8 条声明过（全在 oMLX App 后端），
   **21 条 LM Studio / Ollama 落点一条都没声明**。
3. **两侧算的是两个不同的数**：

   - 路由侧（catalog）：``model.context_limit`` 或 ``placement.context_limit``
     或 **32768 硬编码兜底**；
   - 加载侧：``min(该后端各落点的声明, default=None)``
     ⇒ **没人声明就不带 ``-c``** ⇒ LM Studio 回落到全局
     ``defaultContextLength``（实测 **262144**）。

   ⇒ **承诺 32768、实到 262144** ⇒ 28GB+ 权重按最大上下文分配，
   直接打穿统一内存（2026-08-22 多次实测）。

所以契约是三条：

- **永远带 ``-c``**（fail-closed），不给 ``None``；
- **逐模型**算：同后端上小声明的模型不再把大模型一起拖死；
- **声明只能收紧**，物理上限是硬顶。

⚠ 判据一律落在**副作用**上（真正跑出来的 argv）——
   私有属性可以改，argv 改不了（§5.68 的教训：判据必须落在副作用上）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omlxc.adapters.lmstudio import (
    LmsLoadOptions,
    LmsPlatform,
    LmStudioAdapter,
    ProcessOutput,
)
from omlxc.config import (
    AppConfig,
    BackendConfig,
    DaemonConfig,
    ModelConfig,
    NodeConfig,
    PlacementConfig,
    PoliciesConfig,
    StorageConfig,
)
from omlxc.daemon.composition import build_configured_adapters
from omlxc.domain import BackendKind
from omlxc.domain.context import DEFAULT_CONTEXT_LIMIT, effective_context_limit


def _config(*context_limits: int | None) -> AppConfig:
    """一条 LM Studio 后端 + N 条落点，声明值按 ``context_limits`` 逐条给。"""
    return AppConfig(
        daemon=DaemonConfig(socket_path=Path("/tmp/omlxcd.sock")),
        storage=StorageConfig(database_path=Path("/tmp/state.db")),
        nodes=(NodeConfig(id="node", display_name="Node", platform="macos", memory_gb=16),),
        models=tuple(
            ModelConfig(id=f"m-{index}", category="llm", role="chat", engine="omlx")
            for index in range(len(context_limits))
        ),
        backends=(
            BackendConfig(
                id="lms",
                node_id="node",
                kind=BackendKind.LM_STUDIO,
                base_url="http://127.0.0.1:1234",
            ),
        ),
        placements=tuple(
            PlacementConfig(
                id=f"p-{index}",
                model_id=f"m-{index}",
                backend_id="lms",
                backend_model_id=f"bm-{index}",
                context_limit=limit,
                memory_gb=1,
            )
            for index, limit in enumerate(context_limits)
        ),
    )


def _controlled_adapter(
    *,
    http_models: list[str],
    native_models: list[dict[str, object]] | None,
    placement_context_limits: dict[str, int] | None = None,
    load_options: LmsLoadOptions | None = None,
) -> tuple[LmStudioAdapter, list[tuple[str, ...]]]:
    """构造一个带 SSH 控制通道的适配器，并把每次控制命令的 argv 记下来。"""
    calls: list[tuple[str, ...]] = []
    loaded: set[str] = set()

    async def runner(argv: tuple[str, ...], timeout: float) -> ProcessOutput:
        del timeout
        calls.append(argv)
        if "ps" in argv:  # lms ps --json
            rows = [
                {"modelKey": model_id, "identifier": model_id, "contextLength": 4096} for model_id in sorted(loaded)
            ]
            return ProcessOutput(returncode=0, stdout=json.dumps(rows), stderr="")
        if "load" in argv:  # lms load <key> -c <n> ...
            key = argv[argv.index("load") + 1]
            loaded.add(key)
            return ProcessOutput(returncode=0, stdout="loaded", stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":  # OpenAI 兼容面：只列已加载
            return httpx.Response(200, json={"data": [{"id": model_id} for model_id in http_models]})
        if request.url.path == "/api/v1/models":  # 原生面：全部已下载
            if native_models is None:
                raise AssertionError("unexpected request: /api/v1/models")
            return httpx.Response(200, json={"models": native_models})
        raise AssertionError(f"unexpected request: {request.url.path}")

    adapter = LmStudioAdapter(
        backend_id="lms",
        base_url="https://lmstudio.invalid",
        ssh_target="node.invalid",
        known_hosts_file=Path(__file__).resolve(),
        platform=LmsPlatform.MACOS,
        load_options=load_options,
        placement_context_limits=placement_context_limits,
        process_runner=runner,
        transport=httpx.MockTransport(handler),
    )
    return adapter, calls


def _context_argument(calls: list[tuple[str, ...]]) -> int | None:
    """从最后一次受控加载的 argv 里读出 ``-c`` 的值。"""
    loads = [argv for argv in calls if "load" in argv]
    assert loads, f"没有发生受控加载: {calls}"
    argv = loads[-1]
    assert "-c" in argv, f"受控加载必须带 -c: {argv}"
    return int(argv[argv.index("-c") + 1])


# --------------------------------------------------------------------------
# 纯函数：上下文的唯一真源
# --------------------------------------------------------------------------


def test_effective_context_limit_without_any_input_is_the_known_ceiling() -> None:
    assert effective_context_limit(None, None) == DEFAULT_CONTEXT_LIMIT


def test_effective_context_limit_uses_the_declared_value_when_it_is_tighter() -> None:
    assert effective_context_limit(8194, 262144) == 8194


def test_effective_context_limit_caps_the_declaration_at_the_physical_limit() -> None:
    """声明超过物理上限 ⇒ 按物理上限算（fail-closed）。"""
    assert effective_context_limit(262144, 4096) == 4096


def test_effective_context_limit_does_not_inflate_the_promise_from_the_native_value() -> None:
    """没声明时**不会**因为实物大就自动放大承诺 ⇒ 路由行为与今天一致。"""
    assert effective_context_limit(None, 262144) == DEFAULT_CONTEXT_LIMIT
    assert effective_context_limit(None, 4096) == 4096


# --------------------------------------------------------------------------
# 适配器：加载侧永远带 -c
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_without_any_declaration_still_passes_a_context() -> None:
    """回归锁：**没人声明时也不能不带 -c**。

    旧实现是 ``min(各落点声明, default=None)`` ⇒ 没有 ``-c``
    ⇒ LM Studio 按全局 ``defaultContextLength`` 加载（实测 262144）
    ⇒ 路由承诺 32768、实到 262144 ⇒ 内存打穿。
    """
    adapter, calls = _controlled_adapter(
        http_models=["model-a"],
        native_models=[{"key": "model-a", "max_context_length": 262144}],
    )
    result = await adapter.load_model("model-a")
    assert result.status.value == "succeeded", result.error
    assert _context_argument(calls) == DEFAULT_CONTEXT_LIMIT


@pytest.mark.asyncio
async def test_each_model_gets_its_own_declared_context() -> None:
    """逐模型算：同后端上小声明的模型**不再**把大模型一起拖死。

    旧实现取「该后端全部落点的最小值」⇒ 一个 8194 的声明会把同后端所有
    模型都压到 8194。
    """
    adapter, calls = _controlled_adapter(
        http_models=["model-a", "model-b"],
        native_models=[
            {"key": "model-a", "max_context_length": 262144},
            {"key": "model-b", "max_context_length": 262144},
        ],
        placement_context_limits={"model-a": 8194},
    )
    assert (await adapter.load_model("model-a")).status.value == "succeeded"
    assert _context_argument(calls) == 8194

    calls.clear()
    assert (await adapter.load_model("model-b")).status.value == "succeeded"
    assert _context_argument(calls) == DEFAULT_CONTEXT_LIMIT


@pytest.mark.asyncio
async def test_declared_context_is_capped_by_the_physical_limit() -> None:
    """声明 262144 但实物只支持 4096 ⇒ 按 4096 加载（fail-closed）。"""
    adapter, calls = _controlled_adapter(
        http_models=["model-a"],
        native_models=[{"key": "model-a", "max_context_length": 4096}],
        placement_context_limits={"model-a": 262144},
    )
    assert (await adapter.load_model("model-a")).status.value == "succeeded"
    assert _context_argument(calls) == 4096


@pytest.mark.asyncio
async def test_declared_context_survives_an_unreadable_native_surface() -> None:
    """原生面读不到时，**声明本身**必须仍然生效。"""
    adapter, calls = _controlled_adapter(
        http_models=["model-a"],
        native_models=None,
        placement_context_limits={"model-a": 16384},
    )
    assert (await adapter.load_model("model-a")).status.value == "succeeded"
    assert _context_argument(calls) == 16384


@pytest.mark.asyncio
async def test_native_surface_accepts_both_key_spellings() -> None:
    """两个面的键名不一样（§5.70 实测）：

    - REST 原生面 ``/api/v1/models`` ⇒ ``max_context_length``（snake_case）
    - CLI ``lms ls --json`` ⇒ ``maxContextLength``（camelCase）

    ⇒ 两种拼法都必须收 —— 读错键名会得到「全是 null」的假象。
    """
    adapter, calls = _controlled_adapter(
        http_models=["model-a", "model-b"],
        native_models=[
            {"key": "model-a", "max_context_length": 4096},
            {"key": "model-b", "maxContextLength": 8192},
        ],
    )
    assert (await adapter.load_model("model-a")).status.value == "succeeded"
    assert _context_argument(calls) == 4096

    calls.clear()
    assert (await adapter.load_model("model-b")).status.value == "succeeded"
    assert _context_argument(calls) == 8192


@pytest.mark.asyncio
async def test_load_options_context_length_still_caps_every_model() -> None:
    """直接构造适配器的调用方可以用 ``load_options.context_length`` 压住所有模型。"""
    adapter, calls = _controlled_adapter(
        http_models=["model-a"],
        native_models=[{"key": "model-a", "max_context_length": 262144}],
        placement_context_limits={"model-a": 131072},
        load_options=LmsLoadOptions(context_length=16384),
    )
    assert (await adapter.load_model("model-a")).status.value == "succeeded"
    assert _context_argument(calls) == 16384


# --------------------------------------------------------------------------
# composition：逐落点的声明要真的传到适配器
# --------------------------------------------------------------------------


def test_composition_hands_per_placement_limits_to_the_adapter() -> None:
    """声明必须按 ``backend_model_id`` 逐条传下去（键就是 ``lms load`` 的实参）。"""
    adapters = build_configured_adapters(_config(8194, None, 262144))
    adapter = adapters["lms"]
    limits = adapter._placement_context_limits  # type: ignore[attr-defined]
    assert limits == {"bm-0": 8194, "bm-2": 262144}


def test_composition_no_longer_injects_a_backend_wide_minimum() -> None:
    """⚠ 旧契约：``_load_options.context_length`` = 该后端全部落点的最小声明。

    现在**不再**有后端级最小值 —— 逐模型算，判定落在 argv 上。
    这条断言的作用是**盯住契约变更本身**，不是盯住实现细节。
    """
    adapters = build_configured_adapters(_config(32768, 16384))
    adapter = adapters["lms"]
    assert adapter._load_options.context_length is None  # type: ignore[attr-defined]


def test_idle_ttl_is_still_injected_independently_of_context() -> None:
    """两个维度独立：缺上下文不该拖累 TTL 注入，反之亦然。"""
    config = _config().model_copy(update={"policies": PoliciesConfig(idle_ttl_seconds=900)})
    adapters = build_configured_adapters(config)
    adapter = adapters["lms"]
    assert adapter._load_options.ttl_seconds == 900  # type: ignore[attr-defined]
    assert adapter._load_options.context_length is None  # type: ignore[attr-defined]


def test_idle_ttl_zero_does_not_produce_invalid_ttl_seconds() -> None:
    """``idle_ttl_seconds=0`` 是「显式声明不设超时」，但 ``LmsLoadOptions.ttl_seconds``
    要求 ``ge=1`` ⇒ 0 必须被过滤掉，不能直接透传导致 schema 校验炸掉。"""
    config = _config(16384).model_copy(update={"policies": PoliciesConfig(idle_ttl_seconds=0)})
    adapters = build_configured_adapters(config)
    adapter = adapters["lms"]
    assert adapter._load_options.ttl_seconds is None  # type: ignore[attr-defined]


def test_non_lm_backend_ignores_context_length() -> None:
    config = _config(16384).model_copy(
        update={
            "backends": (
                BackendConfig(
                    id="lms",
                    node_id="node",
                    kind=BackendKind.OMLX_APP,
                    base_url="http://127.0.0.1:8000",
                ),
            )
        }
    )
    adapters = build_configured_adapters(config)
    assert "lms" in adapters
