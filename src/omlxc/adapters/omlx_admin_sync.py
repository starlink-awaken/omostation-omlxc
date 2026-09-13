"""Push declared per-model parameters (temp/top_p/kv_bits) to a live oMLX App.

``ModelConfig.parameters`` in config.toml has historically been pure
declaration: every adapter and the daemon's own request-building code parse
it into ``ModelConfig`` and never read it again, so a model's config could
say ``kv_bits = 8`` while the backend actually always ran with oMLX's own
compiled-in default (``turboquant_kv_enabled = False``, 2026-09-13 audit).

oMLX exposes ``PUT /api/models/{id}/settings`` to change a model's real,
persisted settings (temperature, top_p, and TurboQuant KV cache compression),
but that endpoint requires an admin session unless the server has
``skip_api_key_verification`` set. This module bridges the two: given an
optional Keychain-stored admin API key, it logs in via oMLX's own
``/auto-login`` flow to obtain a session cookie, then PUTs the declared
parameters — retrying the login once on a stale/expired cookie.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from omlxc.domain import BackendKind
from omlxc.domain.security import is_keychain_reference

if TYPE_CHECKING:
    from omlxc.config import AppConfig

_SESSION_COOKIE_NAME = "omlx_admin_session"
_LOGIN_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
_SETTINGS_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=2.0)


class KeychainResolutionError(Exception):
    """Raised when a ``keychain://service/account`` reference cannot be read."""


class OmlxAdminSyncError(Exception):
    """Raised when pushing declared parameters to oMLX's admin API fails."""


def resolve_keychain_reference(reference: str) -> str:
    """Resolve a ``keychain://service/account`` reference to its secret value.

    Reads only from the current user's login Keychain via the ``security``
    CLI; the secret is never written back to config or logged.
    """
    if not is_keychain_reference(reference):
        raise KeychainResolutionError(f"not a valid Keychain reference: {reference!r}")
    _, _, rest = reference.partition("://")
    service, _, account = rest.partition("/")
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeychainResolutionError(f"could not invoke Keychain for {reference}") from exc
    if result.returncode != 0:
        raise KeychainResolutionError(f"Keychain item not found for {reference}")
    secret = result.stdout.strip("\n")
    if not secret:
        raise KeychainResolutionError(f"Keychain item for {reference} is empty")
    return secret


@dataclass(frozen=True, slots=True)
class DeclaredParameters:
    """The subset of ``ModelConfig.parameters`` this module understands."""

    temperature: float | None = None
    top_p: float | None = None
    kv_bits: float | None = None

    @classmethod
    def from_mapping(cls, parameters: dict[str, Any]) -> DeclaredParameters:
        return cls(
            temperature=_as_float(parameters.get("temp")),
            top_p=_as_float(parameters.get("top_p")),
            kv_bits=_as_float(parameters.get("kv_bits")),
        )

    def is_empty(self) -> bool:
        return self.temperature is None and self.top_p is None and self.kv_bits is None

    def to_settings_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        if self.kv_bits is not None:
            # TurboQuant only activates when explicitly enabled; declaring a
            # bit depth in config.toml is the operator's intent to turn it on.
            payload["turboquant_kv_enabled"] = True
            payload["turboquant_kv_bits"] = self.kv_bits
        return payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class OmlxAdminSyncClient:
    """Logs in to one oMLX App instance and pushes per-model settings to it."""

    def __init__(
        self,
        *,
        base_url: str,
        admin_credential_ref: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_credential_ref = admin_credential_ref
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._session_cookie: str | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def sync_model_parameters(self, model_id: str, parameters: DeclaredParameters) -> bool:
        """PUT the declared parameters to oMLX. Returns True if applied.

        Returns False (never raises) when ``parameters`` has nothing to
        apply. Raises :class:`OmlxAdminSyncError` on any real failure so the
        caller can decide how to report a per-model sync failure without one
        model's error hiding another's.
        """
        payload = parameters.to_settings_payload()
        if not payload:
            return False

        response = await self._put_settings(model_id, payload)
        if response.status_code == 401:
            self._session_cookie = None
            response = await self._put_settings(model_id, payload)
        if response.status_code != 200:
            raise OmlxAdminSyncError(
                f"oMLX rejected settings update for {model_id!r}: HTTP {response.status_code} {response.text[:200]}"
            )
        return True

    async def _put_settings(self, model_id: str, payload: dict[str, Any]) -> httpx.Response:
        headers = await self._auth_headers()
        return await self._client.put(
            f"{self._base_url}/admin/api/models/{model_id}/settings",
            json=payload,
            headers=headers,
            timeout=_SETTINGS_TIMEOUT,
        )

    async def _auth_headers(self) -> dict[str, str]:
        if self._session_cookie is None and self._admin_credential_ref is not None:
            await self._login()
        if self._session_cookie is None:
            return {}
        return {"Cookie": f"{_SESSION_COOKIE_NAME}={self._session_cookie}"}

    async def _login(self) -> None:
        try:
            api_key = resolve_keychain_reference(self._admin_credential_ref)  # type: ignore[arg-type]
        except KeychainResolutionError as exc:
            raise OmlxAdminSyncError(str(exc)) from exc
        response = await self._client.get(
            f"{self._base_url}/admin/auto-login",
            params={"key": api_key, "redirect": "/admin/dashboard"},
            headers={"Accept": "application/json"},
            follow_redirects=False,
            timeout=_LOGIN_TIMEOUT,
        )
        cookie = response.cookies.get(_SESSION_COOKIE_NAME)
        if not cookie:
            raise OmlxAdminSyncError("oMLX auto-login did not return a session cookie (wrong admin key?)")
        self._session_cookie = cookie


@dataclass(frozen=True, slots=True)
class SyncResult:
    model_id: str
    backend_id: str
    backend_model_id: str
    status: str  # "applied" | "skipped" | "failed"
    detail: str | None = None


async def sync_declared_parameters(config: AppConfig) -> list[SyncResult]:
    """Push every OMLX_APP-placed model's declared parameters to its backend.

    One :class:`OmlxAdminSyncClient` (and thus one login) is reused per
    backend across all of that backend's placements. A failure on one
    placement is captured as a ``"failed"`` result and does not stop the
    others.
    """
    models_by_id = {model.id: model for model in config.models}
    backends_by_id = {backend.id: backend for backend in config.backends}
    clients: dict[str, OmlxAdminSyncClient] = {}
    results: list[SyncResult] = []

    try:
        for placement in config.placements:
            backend = backends_by_id.get(placement.backend_id)
            model = models_by_id.get(placement.model_id)
            if backend is None or model is None:
                continue

            parameters = DeclaredParameters.from_mapping(model.parameters)
            if parameters.is_empty():
                results.append(
                    SyncResult(model.id, backend.id, placement.backend_model_id, "skipped", "no declared parameters")
                )
                continue
            if backend.kind is not BackendKind.OMLX_APP:
                results.append(
                    SyncResult(
                        model.id,
                        backend.id,
                        placement.backend_model_id,
                        "skipped",
                        f"{backend.kind.value} has no per-model settings API to sync kv_bits/temp/top_p to",
                    )
                )
                continue

            client = clients.get(backend.id)
            if client is None:
                client = OmlxAdminSyncClient(
                    base_url=backend.base_url, admin_credential_ref=backend.admin_credential_ref
                )
                clients[backend.id] = client

            try:
                applied = await client.sync_model_parameters(placement.backend_model_id, parameters)
            except OmlxAdminSyncError as exc:
                results.append(SyncResult(model.id, backend.id, placement.backend_model_id, "failed", str(exc)))
            else:
                status = "applied" if applied else "skipped"
                results.append(SyncResult(model.id, backend.id, placement.backend_model_id, status))
    finally:
        for client in clients.values():
            await client.aclose()

    return results
