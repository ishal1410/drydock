"""Deploy files: what can be pinned without a VM or Docker. None of this proves they run (see README status)."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import sandbox.selftest as S
from sandbox.container import IMAGE

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def test_nothing_is_published_through_docker():
    """Published ports bypass ufw (security B1). Caddy and orch use the host network instead."""
    for name in ("caddy", "orch"):
        assert "ports" not in COMPOSE[name] and COMPOSE[name]["network_mode"] == "host"
        assert COMPOSE[name]["restart"] == "unless-stopped"


def test_orch_binds_to_loopback_one_process_and_ignores_uvicorn_proxy_handling():
    cmd = COMPOSE["orch"]["command"]
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert "--workers" not in cmd and "--no-proxy-headers" in cmd
    assert "healthcheck" in COMPOSE["orch"]


def test_only_orch_gets_the_docker_socket_and_the_key():
    assert any("docker.sock" in v for v in COMPOSE["orch"]["volumes"])
    assert not any("docker.sock" in v for v in COMPOSE["caddy"]["volumes"])
    assert "env_file" not in COMPOSE["caddy"]


def test_compose_states_the_socket_trust_boundary():
    text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "TRUST BOUNDARY" in text and "root on this VM" in text


def test_caddy_proxies_to_loopback_with_a_body_limit():
    text = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    assert "reverse_proxy 127.0.0.1:8000" in text and "max_size 16KB" in text
    assert "{$DRYDOCK_HOST}" in text and "	trusted_proxies" not in text


def test_postgres_digest_is_the_one_the_sandbox_uses():
    assert COMPOSE["live-db"]["image"] == IMAGE
    assert "container.py" in (DEPLOY / "deploy.sh").read_text(encoding="utf-8")


def test_images_are_pinned_by_digest():
    assert "@sha256:" in COMPOSE["caddy"]["image"]
    froms = [ln for ln in (DEPLOY / "Dockerfile").read_text(encoding="utf-8").splitlines() if ln.startswith("FROM")]
    assert len(froms) == 2 and all("@sha256:" in ln for ln in froms)


def test_dockerfile_has_no_trailing_comment_on_an_instruction():
    """Docker reads a trailing # as arguments."""
    for ln in (DEPLOY / "Dockerfile").read_text(encoding="utf-8").splitlines():
        assert ln.startswith("#") or " #" not in ln, ln


def test_image_and_context_never_take_the_key():
    assert not any(".env" in ln for ln in (DEPLOY / "Dockerfile").read_text(encoding="utf-8").splitlines() if ln.startswith("COPY"))
    assert ".env" in (ROOT / ".dockerignore").read_text(encoding="utf-8").split()


@pytest.mark.parametrize("name", ["deploy.sh", "verify.sh"])
def test_scripts_parse_are_marked_untested_and_stay_lf(name):
    path = DEPLOY / name
    head = path.read_text(encoding="utf-8").splitlines()[1]
    # harsh F12: the banner may go once a run is on record; until then a script that never ran must say so
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert head.startswith("# UNTESTED") or f"`deploy/{name}` ran on" in readme, name
    assert "eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
    if os.name != "nt":  # on Windows `bash` is the WSL stub; there, run `bash -n` by hand from Git Bash
        assert subprocess.run(["bash", "-n", path.as_posix()], capture_output=True, text=True).returncode == 0


def test_every_runtime_package_is_copied_into_the_image():
    """A missed COPY only shows up as an ImportError on the VM."""
    docker = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    for pkg in ("core", "eval", "llm", "orch", "policy", "sandbox", "seed", "web/dist"):
        assert f"COPY {pkg} " in docker, pkg


def test_selftest_cli_exits_nonzero_on_a_failed_chip_and_always_destroys(monkeypatch, capsys):
    gone = []
    box = SimpleNamespace(id="box")
    monkeypatch.setattr(S, "start_run_container", lambda: box)
    monkeypatch.setattr(S, "destroy", gone.append)
    monkeypatch.setattr(S, "self_test", lambda b: [S.Chip("a", True, "x"), S.Chip("b", False, "y")])
    assert S.main() == 1 and gone == [box]
    assert "FAIL b" in capsys.readouterr().out
    monkeypatch.setattr(S, "self_test", lambda b: [S.Chip("a", True, "x")])
    assert S.main() == 0


def test_selftest_cli_destroys_on_a_crash(monkeypatch):
    gone = []
    monkeypatch.setattr(S, "start_run_container", lambda: "box")
    monkeypatch.setattr(S, "destroy", gone.append)
    def boom(_box):
        raise RuntimeError("x")
    monkeypatch.setattr(S, "self_test", boom)
    with pytest.raises(RuntimeError):
        S.main()
    assert gone == ["box"]


def test_s13_credential_env_names_are_caught():
    env = ["PATH=/bin", "LLM_API_KEY=x", "ACCESS_TOKEN=x", "DB_PASSWORD=x", "MY_SECRET=x", "PGDATA=/d", "LANG=C"]
    assert S._secret_env(env) == ["LLM_API_KEY", "ACCESS_TOKEN", "DB_PASSWORD", "MY_SECRET"]


def test_s9_image_installs_every_transitive_pin():
    lock = {ln.split("==")[0].lower() for ln in (ROOT / "requirements.lock").read_text(encoding="utf-8").split() if "==" in ln}
    assert {"fastapi", "starlette", "anyio", "h11", "uvicorn", "pyyaml"} <= lock
    assert not lock & {"psycopg", "psycopg-binary", "colorama"}  # venv leftovers and Windows-only pins stay out
    assert "-r requirements.lock" in (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
