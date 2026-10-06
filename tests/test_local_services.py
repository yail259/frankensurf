import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "local_services.py"
SPEC = importlib.util.spec_from_file_location("frankensurf_local_services", SCRIPT)
assert SPEC and SPEC.loader
services = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = services
SPEC.loader.exec_module(services)


def completed(command, *, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def container(spec, *, image=None, state="running", restart="no"):
    environment = [f"{key}={value}" for key, value in spec.environment.items()]
    bindings = {
        container_port: [{"HostIp": host, "HostPort": port}]
        for container_port, (host, port) in spec.ports.items()
    }
    return {
        "Config": {"Image": image or spec.image, "Env": environment},
        "HostConfig": {
            "PortBindings": bindings,
            "RestartPolicy": {"Name": restart, "MaximumRetryCount": 0},
        },
        "State": {"Status": state},
    }


class FakeDocker:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def __call__(self, command):
        command = tuple(command)
        self.calls.append(command)
        if command[:2] == ("docker", "inspect"):
            record = self.records.get(command[2])
            if record is None:
                return completed(command, stderr="Error: No such object", returncode=1)
            return completed(command, stdout=json.dumps([record]))
        return completed(command, stdout="ok\n")


def always_healthy(url, kind, timeout):
    return True, "ok"


def write_private_config(tmp_path):
    path = tmp_path / "private" / "services.json"
    services.write_config(path, services.DEFAULT_ENDPOINTS)
    return path


def test_config_is_atomic_private_and_exec_imports_only_allowlisted_endpoints(tmp_path):
    path = write_private_config(tmp_path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".services-*"))

    environment = services.exec_environment(path, {"EXISTING": "kept"})
    assert environment == {
        "EXISTING": "kept",
        "FRANKENSURF_SEARCH_URL": "http://127.0.0.1:8088",
        "FRANKENSURF_STEEL_URL": "http://127.0.0.1:3300",
    }
    assert "FRANKENSURF_BROWSER_AGENT_MODEL_URL" not in environment


def test_exact_pinned_service_definitions():
    assert services.SEARCH.name == "frankensurf-search"
    assert services.SEARCH.image == (
        "ghcr.io/searxng/searxng@"
        "sha256:a07a5cd2da2c63d66e559f9e4d3a3db106cfc6c32fb0ac70abe91cc28bcd7350"
    )
    assert services.SEARCH.ports == {"8080/tcp": ("127.0.0.1", "8088")}
    assert services.SEARCH.health_url == "http://127.0.0.1:8088/healthz"
    assert services.STEEL.name == "frankensurf-steel-local"
    assert services.STEEL.image == (
        "ghcr.io/steel-dev/steel-browser-api@"
        "sha256:2948a8d8ba1103146dac1c32a121e511955f43976da6905ada0c32fd19b41f59"
    )
    assert services.STEEL.ports == {
        "3000/tcp": ("127.0.0.1", "3300"),
        "9223/tcp": ("127.0.0.1", "9323"),
    }
    assert services.STEEL.environment == {
        "DOMAIN": "127.0.0.1:3300",
        "CDP_DOMAIN": "127.0.0.1:9323",
        "LOG_STORAGE_ENABLED": "false",
    }
    assert services.STEEL.health_url == "http://127.0.0.1:3300/v1/health"


def test_config_rejects_remote_credentials_paths_and_unknown_fields(tmp_path):
    endpoints = dict(services.DEFAULT_ENDPOINTS)
    for invalid in (
        "https://127.0.0.1:3300",
        "http://example.com:3300",
        "http://user:secret@127.0.0.1:3300",
        "http://127.0.0.1:3300/path",
    ):
        endpoints["steel"] = invalid
        with pytest.raises(services.LifecycleError):
            services.write_config(tmp_path / invalid.split(":")[0] / "services.json", endpoints)

    path = write_private_config(tmp_path / "unknown")
    payload = json.loads(path.read_text())
    payload["unexpected"] = "must not become an environment variable"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(services.LifecycleError, match="unknown or missing fields"):
        services.exec_environment(path, {})


def test_config_rejects_symlink_file_and_directory(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("unchanged")
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    link = folder / "services.json"
    link.symlink_to(target)
    with pytest.raises(services.LifecycleError, match="must not be a symlink"):
        services.write_config(link, services.DEFAULT_ENDPOINTS)
    assert target.read_text() == "unchanged"

    real_folder = tmp_path / "real"
    real_folder.mkdir(mode=0o700)
    linked_folder = tmp_path / "linked"
    linked_folder.symlink_to(real_folder, target_is_directory=True)
    with pytest.raises(services.LifecycleError, match="must not be a symlink"):
        services.write_config(linked_folder / "services.json", services.DEFAULT_ENDPOINTS)


def test_status_is_read_only_and_reports_external_model_health(tmp_path):
    path = write_private_config(tmp_path)
    docker = FakeDocker(
        {
            services.SEARCH.name: container(services.SEARCH, restart="unless-stopped"),
            services.STEEL.name: container(services.STEEL, restart="unless-stopped"),
        }
    )

    def probe(url, kind, timeout):
        return (kind != "llama"), ("ok" if kind != "llama" else "connection refused")

    result = services.status(path, run=docker, probe=probe)
    assert result["config"]["valid"] is True
    assert [item["healthy"] for item in result["managed"]] == [True, True]
    assert result["external"] == [
        {
            "service": "browser_agent_model",
            "endpoint": "http://127.0.0.1:18087",
            "configured_browser_agent": False,
            "healthy": False,
            "health_detail": "connection refused",
            "restart_persistent": False,
            "managed": False,
        }
    ]
    assert docker.calls == [
        ("docker", "inspect", services.SEARCH.name),
        ("docker", "inspect", services.STEEL.name),
    ]


def test_mismatched_container_causes_no_docker_mutation(tmp_path):
    docker = FakeDocker(
        {services.SEARCH.name: container(services.SEARCH, image="example.invalid/other")}
    )
    with pytest.raises(services.LifecycleError, match="refusing to mutate or replace"):
        services.start_services(
            ("search",), repo_root=tmp_path, timeout=1, run=docker, probe=always_healthy
        )
    assert docker.calls == [("docker", "inspect", services.SEARCH.name)]


def test_all_services_are_preflighted_before_any_mutation(tmp_path):
    docker = FakeDocker(
        {
            services.SEARCH.name: container(services.SEARCH, restart="no"),
            services.STEEL.name: container(services.STEEL, image="example.invalid/other"),
        }
    )
    with pytest.raises(services.LifecycleError, match="refusing to mutate or replace"):
        services.start_services(
            ("search", "steel"),
            repo_root=tmp_path,
            timeout=1,
            run=docker,
            probe=always_healthy,
        )
    assert docker.calls == [
        ("docker", "inspect", services.SEARCH.name),
        ("docker", "inspect", services.STEEL.name),
        ("docker", "inspect", "frankensurf-steel"),
    ]


def test_running_legacy_steel_conflict_causes_no_mutation(tmp_path):
    docker = FakeDocker(
        {
            services.STEEL.name: container(services.STEEL),
            "frankensurf-steel": {"State": {"Status": "running"}},
        }
    )
    with pytest.raises(services.LifecycleError, match="Both .* are running"):
        services.start_services(
            ("steel",), repo_root=tmp_path, timeout=1, run=docker, probe=always_healthy
        )
    assert docker.calls == [
        ("docker", "inspect", services.STEEL.name),
        ("docker", "inspect", "frankensurf-steel"),
    ]


def test_healthy_exact_container_gets_restart_policy_once(tmp_path):
    docker = FakeDocker({services.SEARCH.name: container(services.SEARCH, restart="no")})
    first = services.start_services(
        ("search",), repo_root=tmp_path, timeout=1, run=docker, probe=always_healthy
    )
    assert first[0]["restart_policy_changed"] is True
    assert docker.calls == [
        ("docker", "inspect", services.SEARCH.name),
        ("docker", "update", "--restart", "unless-stopped", services.SEARCH.name),
    ]

    docker = FakeDocker(
        {services.SEARCH.name: container(services.SEARCH, restart="unless-stopped")}
    )
    second = services.start_services(
        ("search",), repo_root=tmp_path, timeout=1, run=docker, probe=always_healthy
    )
    assert second[0]["restart_policy_changed"] is False
    assert docker.calls == [("docker", "inspect", services.SEARCH.name)]


def test_stopped_exact_container_is_started_then_made_persistent(tmp_path):
    docker = FakeDocker(
        {services.SEARCH.name: container(services.SEARCH, state="exited", restart="no")}
    )
    result = services.start_services(
        ("search",), repo_root=tmp_path, timeout=1, run=docker, probe=always_healthy
    )
    assert result[0]["started"] is True
    assert docker.calls == [
        ("docker", "inspect", services.SEARCH.name),
        ("docker", "start", services.SEARCH.name),
        ("docker", "update", "--restart", "unless-stopped", services.SEARCH.name),
    ]


def test_cli_configure_and_exec_use_private_allowlisted_config(tmp_path, monkeypatch, capsys):
    path = tmp_path / "private" / "services.json"
    assert services.main(["--config", str(path), "configure"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["configured"] == str(path)

    executed = {}

    def fake_exec(program, command, environment):
        executed.update(program=program, command=command, environment=environment)

    monkeypatch.setattr(services.os, "execvpe", fake_exec)
    assert services.main(
        ["--config", str(path), "exec", "--", "python3", "-c", "print('ok')"]
    ) == 0
    assert executed["program"] == "python3"
    assert executed["command"] == ["python3", "-c", "print('ok')"]
    assert executed["environment"]["FRANKENSURF_SEARCH_URL"] == "http://127.0.0.1:8088"
    assert executed["environment"]["FRANKENSURF_STEEL_URL"] == "http://127.0.0.1:3300"
    assert "FRANKENSURF_BROWSER_AGENT_MODEL_URL" not in executed["environment"]


def test_cli_status_and_start_dispatch_without_hidden_mutation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "private" / "services.json"
    observed = {}

    def fake_status(config_path):
        observed["status_path"] = config_path
        return {"read_only": True}

    def fake_start(identifiers, *, repo_root, timeout):
        observed.update(identifiers=identifiers, repo_root=repo_root, timeout=timeout)
        return [{"healthy": True}]

    monkeypatch.setattr(services, "status", fake_status)
    monkeypatch.setattr(services, "start_services", fake_start)
    assert services.main(["--config", str(path), "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"read_only": True}
    assert observed["status_path"] == path

    assert services.main(["start", "all", "--health-timeout", "12.5"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"healthy": True}]
    assert observed["identifiers"] == ("search", "steel")
    assert observed["timeout"] == 12.5
    assert observed["repo_root"] == SCRIPT.parent.parent
