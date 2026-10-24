"""Global pytest fixtures and environment configuration for omlxc tests."""

import os

import pytest

# Strip proxy environment variables to ensure test isolation from host network proxies
for _key in [
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
]:
    os.environ.pop(_key, None)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--memory-guard-capacity-gb",
        dest="memory_guard_capacity_gb",
        default=None,
        help="Force the local physical memory capacity used by daemon memory admission tests.",
    )


@pytest.fixture(autouse=True)
def _isolate_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure every test runs with proxy environment variables removed."""
    for key in [
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_memory_guard_override(pytestconfig: pytest.Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMLXC_MEMORY_GUARD_CAPACITY_GB", raising=False)
    capacity = pytestconfig.getoption("memory_guard_capacity_gb")
    if capacity is not None:
        monkeypatch.setenv("OMLXC_MEMORY_GUARD_CAPACITY_GB", str(capacity))
