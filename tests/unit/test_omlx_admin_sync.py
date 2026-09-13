"""ModelConfig.parameters (temp/top_p/kv_bits) was pure declaration: nothing
in the daemon or any adapter ever read it, so a model's config could say
`kv_bits = 8` while oMLX kept running with its own compiled-in default
(turboquant_kv_enabled=False, 2026-09-13 audit). These tests cover the
bridge that pushes declared parameters to oMLX's real per-model settings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

import omlxc.adapters.omlx_admin_sync as admin_sync_module
from omlxc.adapters.omlx_admin_sync import (
    DeclaredParameters,
    KeychainResolutionError,
    OmlxAdminSyncClient,
    OmlxAdminSyncError,
    SyncResult,
    resolve_keychain_reference,
    sync_declared_parameters,
)
from omlxc.config import AppConfig, BackendConfig, DaemonConfig, ModelConfig, NodeConfig, PlacementConfig, StorageConfig
from omlxc.domain import BackendKind


class TestDeclaredParameters:
    def test_from_mapping_reads_known_fields(self) -> None:
        params = DeclaredParameters.from_mapping({"temp": 0.2, "top_p": 0.9, "kv_bits": 8, "unrelated": "x"})
        assert params == DeclaredParameters(temperature=0.2, top_p=0.9, kv_bits=8.0)

    def test_from_mapping_ignores_non_numeric(self) -> None:
        params = DeclaredParameters.from_mapping({"temp": "not-a-number"})
        assert params.temperature is None

    def test_empty_mapping_is_empty(self) -> None:
        assert DeclaredParameters.from_mapping({}).is_empty()
        assert not DeclaredParameters(temperature=0.1).is_empty()

    def test_kv_bits_enables_turboquant(self) -> None:
        payload = DeclaredParameters(kv_bits=8.0).to_settings_payload()
        assert payload == {"turboquant_kv_enabled": True, "turboquant_kv_bits": 8.0}

    def test_temp_and_top_p_pass_through_without_turboquant(self) -> None:
        payload = DeclaredParameters(temperature=0.2, top_p=0.9).to_settings_payload()
        assert payload == {"temperature": 0.2, "top_p": 0.9}

    def test_no_declared_fields_is_empty_payload(self) -> None:
        assert DeclaredParameters().to_settings_payload() == {}


class TestResolveKeychainReference:
    def test_rejects_non_keychain_reference(self) -> None:
        with pytest.raises(KeychainResolutionError):
            resolve_keychain_reference("plaintext-secret")

    def test_reads_secret_via_security_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="s3cr3t\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        secret = resolve_keychain_reference("keychain://omlx-admin/mbp")
        assert secret == "s3cr3t"
        assert captured["cmd"] == ["security", "find-generic-password", "-s", "omlx-admin", "-a", "mbp", "-w"]

    def test_missing_item_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
        )
        with pytest.raises(KeychainResolutionError):
            resolve_keychain_reference("keychain://omlx-admin/mbp")


class TestOmlxAdminSyncClient:
    @pytest.mark.asyncio
    async def test_sync_without_credential_sends_no_auth_header(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True})

        client = OmlxAdminSyncClient(
            base_url="http://127.0.0.1:8000",
            admin_credential_ref=None,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
        )
        applied = await client.sync_model_parameters("vision", DeclaredParameters(kv_bits=8.0))
        await client.aclose()

        assert applied is True
        assert len(requests) == 1
        assert requests[0].url.path == "/admin/api/models/vision/settings"
        assert "cookie" not in requests[0].headers

    @pytest.mark.asyncio
    async def test_empty_parameters_skips_the_request_entirely(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        client = OmlxAdminSyncClient(
            base_url="http://127.0.0.1:8000",
            admin_credential_ref=None,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
        )
        applied = await client.sync_model_parameters("vision", DeclaredParameters())
        await client.aclose()

        assert applied is False
        assert calls == 0

    @pytest.mark.asyncio
    async def test_logs_in_with_admin_credential_before_first_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("omlxc.adapters.omlx_admin_sync.resolve_keychain_reference", lambda ref: "the-admin-key")
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/admin/auto-login":
                assert request.url.params["key"] == "the-admin-key"
                return httpx.Response(302, headers={"set-cookie": "omlx_admin_session=tok123; Path=/"})
            assert request.headers["cookie"] == "omlx_admin_session=tok123"
            return httpx.Response(200, json={"success": True})

        client = OmlxAdminSyncClient(
            base_url="http://127.0.0.1:8000",
            admin_credential_ref="keychain://omlx-admin/mbp",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
        )
        applied = await client.sync_model_parameters("vision", DeclaredParameters(kv_bits=8.0))
        await client.aclose()

        assert applied is True
        assert seen == ["/admin/auto-login", "/admin/api/models/vision/settings"]

    @pytest.mark.asyncio
    async def test_stale_cookie_triggers_one_relogin_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("omlxc.adapters.omlx_admin_sync.resolve_keychain_reference", lambda ref: "the-admin-key")
        put_attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal put_attempts
            if request.url.path == "/admin/auto-login":
                return httpx.Response(302, headers={"set-cookie": "omlx_admin_session=fresh; Path=/"})
            put_attempts += 1
            if put_attempts == 1:
                return httpx.Response(401, json={"detail": "expired"})
            return httpx.Response(200, json={"success": True})

        client = OmlxAdminSyncClient(
            base_url="http://127.0.0.1:8000",
            admin_credential_ref="keychain://omlx-admin/mbp",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
        )
        applied = await client.sync_model_parameters("vision", DeclaredParameters(kv_bits=8.0))

        assert applied is True
        assert put_attempts == 2

    @pytest.mark.asyncio
    async def test_non_200_raises_sync_error(self) -> None:
        client = OmlxAdminSyncClient(
            base_url="http://127.0.0.1:8000",
            admin_credential_ref=None,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(404, text="model not found")),
                trust_env=False,
            ),
        )
        with pytest.raises(OmlxAdminSyncError):
            await client.sync_model_parameters("does-not-exist", DeclaredParameters(kv_bits=8.0))

    @pytest.mark.asyncio
    async def test_missing_credential_reports_as_sync_error_not_crash(self) -> None:
        client = OmlxAdminSyncClient(
            base_url="http://127.0.0.1:8000",
            admin_credential_ref="keychain://missing/missing",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), trust_env=False
            ),
        )
        with pytest.raises(OmlxAdminSyncError):
            await client.sync_model_parameters("vision", DeclaredParameters(kv_bits=8.0))


def _patch_client_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Make every OmlxAdminSyncClient built by sync_declared_parameters route
    through a fixed mock transport instead of a real network client."""

    def fake_client(*, base_url: str, admin_credential_ref: str | None) -> OmlxAdminSyncClient:
        return OmlxAdminSyncClient(
            base_url=base_url,
            admin_credential_ref=admin_credential_ref,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
        )

    monkeypatch.setattr(admin_sync_module, "OmlxAdminSyncClient", fake_client)


def _config(tmp_path: Path, *, backends, models, placements) -> AppConfig:
    return AppConfig(
        daemon=DaemonConfig(socket_path=tmp_path / "daemon.sock"),
        storage=StorageConfig(database_path=tmp_path / "state.db"),
        nodes=(NodeConfig(id="local", display_name="Local", platform="macos"),),
        backends=backends,
        models=models,
        placements=placements,
    )


class TestSyncDeclaredParameters:
    @pytest.mark.asyncio
    async def test_only_omlx_app_placements_are_synced(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        put_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            put_paths.append(request.url.path)
            return httpx.Response(200, json={"success": True})

        _patch_client_transport(monkeypatch, handler)
        omlx_app = BackendConfig(
            id="omlx-app", node_id="local", kind=BackendKind.OMLX_APP, base_url="http://127.0.0.1:8000"
        )
        lm_studio = BackendConfig(
            id="lm-studio", node_id="local", kind=BackendKind.LM_STUDIO, base_url="http://127.0.0.1:1234"
        )
        vision = ModelConfig(id="vision", category="vision", role="vision", engine="mlx_vlm", parameters={"kv_bits": 8})
        config = _config(
            tmp_path,
            backends=(omlx_app, lm_studio),
            models=(vision,),
            placements=(
                PlacementConfig(id="v-omlx", model_id="vision", backend_id="omlx-app", backend_model_id="vision"),
                PlacementConfig(
                    id="v-lms", model_id="vision", backend_id="lm-studio", backend_model_id="qwen/vision-lms"
                ),
            ),
        )

        results = await sync_declared_parameters(config)

        assert put_paths == ["/admin/api/models/vision/settings"]
        assert len(results) == 2
        by_backend = {r.backend_id: r for r in results}
        assert by_backend["omlx-app"].status == "applied"
        assert by_backend["lm-studio"].status == "skipped"
        assert "no per-model settings API" in (by_backend["lm-studio"].detail or "")

    @pytest.mark.asyncio
    async def test_model_without_declared_parameters_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        _patch_client_transport(monkeypatch, handler)
        omlx_app = BackendConfig(
            id="omlx-app", node_id="local", kind=BackendKind.OMLX_APP, base_url="http://127.0.0.1:8000"
        )
        plain = ModelConfig(id="plain", category="coding", role="chat", engine="batched")
        config = _config(
            tmp_path,
            backends=(omlx_app,),
            models=(plain,),
            placements=(PlacementConfig(id="p", model_id="plain", backend_id="omlx-app", backend_model_id="plain"),),
        )

        results = await sync_declared_parameters(config)

        assert calls == 0
        assert results == [SyncResult("plain", "omlx-app", "plain", "skipped", "no declared parameters")]

    @pytest.mark.asyncio
    async def test_one_placement_failure_does_not_stop_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "broken" in request.url.path:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"success": True})

        _patch_client_transport(monkeypatch, handler)
        omlx_app = BackendConfig(
            id="omlx-app", node_id="local", kind=BackendKind.OMLX_APP, base_url="http://127.0.0.1:8000"
        )
        ok_model = ModelConfig(id="ok", category="coding", role="chat", engine="batched", parameters={"kv_bits": 8})
        broken_model = ModelConfig(
            id="broken", category="coding", role="chat", engine="batched", parameters={"kv_bits": 8}
        )
        config = _config(
            tmp_path,
            backends=(omlx_app,),
            models=(ok_model, broken_model),
            placements=(
                PlacementConfig(id="ok-p", model_id="ok", backend_id="omlx-app", backend_model_id="ok"),
                PlacementConfig(id="broken-p", model_id="broken", backend_id="omlx-app", backend_model_id="broken"),
            ),
        )

        results = await sync_declared_parameters(config)

        by_model = {r.model_id: r for r in results}
        assert by_model["ok"].status == "applied"
        assert by_model["broken"].status == "failed"
