"""Docker-gated suites skip with a plain reason when the daemon is down: a skip here means NOT RUN, never passed."""
import subprocess

import pytest

DOCKER_MODULES = {"test_apply", "test_core", "test_seed"}  # module-scoped autouse fixtures build the template
DOCKER_TESTS = {"test_destroy", "test_destroy_raises_when_not_removed"}


def _docker_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _needs_docker(item) -> bool:
    module = item.module.__name__.rsplit(".", 1)[-1]
    return (module in DOCKER_MODULES
            or (module == "test_sandbox" and ("box" in item.fixturenames or item.originalname in DOCKER_TESTS)))


def pytest_collection_modifyitems(items):
    gated = [i for i in items if _needs_docker(i)]
    if gated and not _docker_up():
        skip = pytest.mark.skip(reason="Docker unreachable: NOT RUN")
        for item in gated:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch):
    """Retries and pacing never really sleep in tests, and each test starts with an empty pacing window."""
    from llm import client
    monkeypatch.setattr(client, "_sleep", lambda s: None)
    client.reset_meters()
    for name in ("LLM_TPM_LIMIT", "LLM_FALLBACKS"):
        monkeypatch.delenv(name, raising=False)
