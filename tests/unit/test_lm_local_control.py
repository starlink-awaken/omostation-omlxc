"""Contracts for the **local control channel** of the LM Studio adapter (§5.69).

⚠⚠⚠ **Why this file exists**（§5.69 实测）：

`LmStudioAdapter` 原先**只有一条**控制通道 —— SSH ⇒
**「LM Studio 就跑在本机」这件事反而没法控制**
（要为本机另开一条 SSH 通道 ⇒ **访问控制红线**）。
而 `composition.py` 的既有约定**已经写着「loopback ⇒ 不用 SSH」**
⇒ ⇒ **缺的正是这一个「本地控制」模式**，不是缺一条 SSH 通道。

⚠⚠⚠ **同一性校验是硬前提**：`lms` CLI 控制的是**本机默认那个实例**，
而后端指向的 `base_url` **可能**是另一个 ⇒
控制命令会打到错的实例上 ⇒ **加载/卸载错模型**
⇒ ⇒ **端口对不上就 fail-closed**（宁可没有控制通道，也不要控制错实例）。

⚠ **断言不许依赖「这台机器恰好在跑什么」**（§5.52 纪律）⇒
「能不能开本地控制」与「实例是不是同一个」**都能注入** ⇒ 单测不依赖本机装了什么。
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx
import pytest

from omlxc.adapters.lmstudio import LmStudioAdapter, ProcessOutput
from omlxc.config.schema import BackendConfig, BackendKind
from omlxc.daemon.composition import build_configured_adapter
from omlxc.domain.protocols import AdapterErrorCode, OperationStatus

LOCAL_EXECUTABLE = "/opt/lmstudio/bin/lms"


def _transport(*, downloaded_ids: Iterable[str] = ("model-a",)) -> httpx.MockTransport:
    """⚠ **两个面的形状不一样**（§5.68 实测）：

    - `/v1/models`（**OpenAI 兼容面**）⇒ 只列**已加载**的 ⇒ 空闲时 `data` 是 `[]`；
    - `/api/v1/models`（**原生面**）⇒ 列**全部已下载** ⇒ `models[].key` 就是路由表用的 id。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={"models": [{"key": model_id, "loaded_instances": []} for model_id in downloaded_ids]},
            )
        raise AssertionError(request.url.path)

    return httpx.MockTransport(handler)


def _runner(
    calls: list[tuple[str, ...]],
    *,
    status_stdout: str = "The server is running on port 1234.",
    ps_stdout: str = "[]",
):
    async def runner(argv: tuple[str, ...], timeout: float) -> ProcessOutput:
        del timeout
        calls.append(argv)
        if argv[-2:] == ("server", "status"):
            return ProcessOutput(returncode=0, stdout=status_stdout, stderr="")
        if argv[-2:] == ("ps", "--json"):
            return ProcessOutput(returncode=0, stdout=ps_stdout, stderr="")
        return ProcessOutput(returncode=0, stdout="", stderr="")

    return runner


# ── 本地控制：argv 里没有 ssh ───────────────────────────────────────


@pytest.mark.asyncio
async def test_local_control_runs_lms_directly_without_ssh() -> None:
    """⚠⚠⚠ **本地控制 ⇒ argv 里没有 ssh** —— 这是整件事的意义。

    ⇒ 有 ssh ⇒ 就还是「为本机开了一条 SSH 通道」⇒ **访问控制红线原样还在**。
    """
    calls: list[tuple[str, ...]] = []
    adapter = LmStudioAdapter(
        backend_id="lm",
        base_url="http://127.0.0.1:1234",
        lms_executable=LOCAL_EXECUTABLE,
        local_control=True,
        process_runner=_runner(calls),
        transport=_transport(),
    )

    await adapter.list_models()

    assert calls, "本地控制必须真的跑过命令"
    assert all(argv[0] == LOCAL_EXECUTABLE for argv in calls), calls
    assert not any("ssh" in argv for argv in calls), calls


@pytest.mark.asyncio
async def test_local_control_is_off_without_an_executable() -> None:
    """⚠ **默认关**：没给 `lms_executable` ⇒ **不许**开本地控制（fail-closed）。

    ⇒ launchd 起的服务 PATH 不含 `~/.local/bin` ⇒ 不能靠 PATH 猜。
    """
    calls: list[tuple[str, ...]] = []
    adapter = LmStudioAdapter(
        backend_id="lm",
        base_url="http://127.0.0.1:1234",
        local_control=True,
        process_runner=_runner(calls),
        transport=_transport(),
    )

    await adapter.list_models()

    # ⚠⚠⚠ **断言要落在「机制」上，不能只落在「效果」上**（§5.69 变异验证实测）：
    # 「没有控制通道」与「控制通道存在但命令失败」的可观察结果**完全一样**
    # （都不发命令）⇒ 只断言 `calls == []` ⇒ **真正的变异打不中**（等价变异）。
    # ⇒ ⇒ 必须同时断言**机制**：**控制通道压根没被算作可用**。
    assert adapter._has_control_channel() is False
    assert calls == []


@pytest.mark.asyncio
async def test_local_control_is_off_when_flag_is_false() -> None:
    """⚠ **开关是显式的** —— 给了可执行文件也不代表开了本地控制。"""
    calls: list[tuple[str, ...]] = []
    adapter = LmStudioAdapter(
        backend_id="lm",
        base_url="http://127.0.0.1:1234",
        lms_executable=LOCAL_EXECUTABLE,
        local_control=False,
        process_runner=_runner(calls),
        transport=_transport(),
    )

    await adapter.list_models()

    assert adapter._has_control_channel() is False
    assert calls == []


# ── 同一性校验：端口对不上 ⇒ 不许控制 ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_stdout",
    [
        "",
        "The server is running on port 9999.",
        "server running",
    ],
)
async def test_local_control_refuses_when_the_instance_does_not_match(
    status_stdout: str,
) -> None:
    """⚠⚠⚠ **端口对不上 ⇒ 不许发控制命令**（fail-closed）。

    ⇒ `lms` CLI 控制的是**本机默认实例**；`base_url` 可能指向另一个
    ⇒ 控制错实例 = **加载/卸载错模型**。
    ⇒ **宁可没有控制通道，也不要控制错实例。**
    """
    calls: list[tuple[str, ...]] = []
    adapter = LmStudioAdapter(
        backend_id="lm",
        base_url="http://127.0.0.1:1234",
        lms_executable=LOCAL_EXECUTABLE,
        local_control=True,
        process_runner=_runner(calls, status_stdout=status_stdout),
        transport=_transport(),
    )

    models = await adapter.list_models()
    result = await adapter.load_model("model-a")

    assert models[0].loaded is None
    assert result.status is OperationStatus.FAILED
    assert result.error is not None
    assert result.error.code is AdapterErrorCode.UNSUPPORTED
    # ⚠⚠⚠ **控制命令一条都不许发**（只允许那次「核对身份」的 status）
    assert not any(argv[-2:] == ("ps", "--json") for argv in calls), calls
    assert not any(argv[1:3] == ("load", "model-a") for argv in calls), calls


@pytest.mark.asyncio
async def test_local_control_verifies_identity_before_any_write() -> None:
    """⚠⚠⚠ **先核对身份，再控制** —— 顺序不能反。

    ⇒ 顺序反了 ⇒ 「核对」只是装饰 ⇒ 控制命令已经发出去了。
    """
    calls: list[tuple[str, ...]] = []
    adapter = LmStudioAdapter(
        backend_id="lm",
        base_url="http://127.0.0.1:1234",
        lms_executable=LOCAL_EXECUTABLE,
        local_control=True,
        process_runner=_runner(calls),
        transport=_transport(),
    )

    await adapter.list_models()

    assert calls[0][-2:] == ("server", "status"), calls


# ── 有本地控制 ⇒ 受控加载真的发生（不再「放行」）────────────────────


@pytest.mark.asyncio
async def test_local_control_makes_guarded_load_reachable() -> None:
    """⚠⚠⚠ **本地控制 ⇒ 加载路径不再「放行」**。

    ⇒ 放行 = 让 LM Studio 自己 JIT（按全局 defaultContextLength）
    ⇒ 那正是 §5.67 记录的**内存洞**（本机曾被观察到 262144）。
    """
    calls: list[tuple[str, ...]] = []
    adapter = LmStudioAdapter(
        backend_id="lm",
        base_url="http://127.0.0.1:1234",
        lms_executable=LOCAL_EXECUTABLE,
        local_control=True,
        process_runner=_runner(calls),
        transport=_transport(),
    )

    await adapter.list_models()
    await adapter.load_model("model-a")

    # ⚠ `lms load <key> -y` —— `-y` 是**默认恒开**的（LmsLoadOptions.yes 默认 True），
    # 否则非交互环境会挂在提示符上。
    assert any(argv[1:3] == ("load", "model-a") for argv in calls), calls
    assert any(argv[-1] == "-y" for argv in calls if argv[1:3] == ("load", "model-a")), calls


# ── 配置层：loopback 才有本地控制 ───────────────────────────────────


def _backend(**overrides) -> BackendConfig:
    data = {
        "id": "lm",
        "node_id": "node",
        "kind": BackendKind.LM_STUDIO,
        "base_url": "http://127.0.0.1:1234",
    }
    data.update(overrides)
    return BackendConfig(**data)


def test_loopback_backend_gets_local_control() -> None:
    """⚠ **loopback + 给了 lms_executable ⇒ 开本地控制**（不开 SSH）。"""
    adapter = build_configured_adapter(
        _backend(lms_executable=LOCAL_EXECUTABLE),
    )

    assert adapter._has_control_channel() is True


def test_remote_backend_never_gets_local_control() -> None:
    """⚠⚠⚠ **远端后端不许开本地控制** —— 那是**跨机**部署，只能走 SSH。

    ⇒ 开了就等于「在本机跑 `lms` 去控制另一台机器上的 LM Studio」⇒ **控制错机器**。
    """
    adapter = build_configured_adapter(
        _backend(base_url="http://100.64.43.36:1234", lms_executable=LOCAL_EXECUTABLE),
    )

    assert adapter._has_control_channel() is False


def test_ssh_control_endpoint_wins_over_local_control(tmp_path) -> None:
    """⚠⚠⚠ **SSH 优先**：配了 `control_endpoint` 就走 SSH（哪怕 base_url 是 loopback）。

    ⇒ 否则同一份配置有两种解释 ⇒ **行为不可预测**。
    """
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("node.invalid ssh-ed25519 AAAATEST\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    adapter = build_configured_adapter(
        _backend(
            lms_executable=LOCAL_EXECUTABLE,
            control_endpoint="operator@node.invalid",
            known_hosts_file=known_hosts,
        ),
    )

    assert adapter._has_control_channel() is True
    assert adapter._local_control is False


def test_lms_executable_must_be_an_absolute_path() -> None:
    """⚠ **必须是绝对路径** —— launchd 的 PATH 不含 `~/.local/bin`。"""
    with pytest.raises(ValueError, match="absolute path"):
        _backend(lms_executable="lms")


def test_lms_executable_is_only_valid_for_lm_backends() -> None:
    """⚠ **只对 LM 后端有效** —— 别的地方配了它没有意义。"""
    with pytest.raises(ValueError, match="only valid for LM backends"):
        _backend(kind=BackendKind.OLLAMA, lms_executable=LOCAL_EXECUTABLE)
