import json
import os
from pathlib import Path

import pytest

from frankensurf import browser_use_config as config
from frankensurf.browser_use_binding import load_binding
from frankensurf.browser_use_provider import BrowserUseProvider


@pytest.fixture
def operator_config(monkeypatch, tmp_path):
    for name in config.ENV_FIELDS.values():
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    directory = home / ".config/frankensurf"
    directory.mkdir(parents=True, mode=0o700)
    interpreter = home / "agent-venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("isolated interpreter fixture")
    distribution = interpreter.parent.parent / "lib/python3.14/site-packages" / ("browser_use-" + config.SDK_VERSION + ".dist-info")
    distribution.mkdir(parents=True)
    browser = home / "browser"
    browser.write_text("local browser fixture")
    factory = home / "factory.py"
    factory.write_text("# trusted factory version one\n")
    value = {"factory":str(factory)+":make_model", "billing":"unmetered", "revision":"fixture-v1", "python":str(interpreter), "browser":str(browser)}
    path = directory / "browser-agent.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value


def test_private_persisted_configuration_and_environment_precedence(operator_config, monkeypatch):
    path, value = operator_config
    observed = config.snapshot()
    assert observed.billing == "unmetered" and observed.revision == "fixture-v1"
    assert observed.factory.is_absolute() and observed.browser.is_absolute()
    assert len(observed.fingerprint) == 64
    monkeypatch.setenv("FRANKENSURF_BROWSER_USE_BINDING_REVISION", "fixture-v2")
    monkeypatch.setenv("FRANKENSURF_BROWSER_USE_BILLING", "paid")
    updated = config.snapshot()
    assert updated.billing == "paid" and updated.revision == "fixture-v2"
    assert updated.fingerprint != observed.fingerprint
    assert BrowserUseProvider().manifest.paid
    assert str(path) not in BrowserUseProvider().manifest.version


@pytest.mark.parametrize("field,value", [("unknown", "extra"), ("billing", "free"), ("revision", True),
                                         ("factory", "relative.py:make_model"), ("python", "relative-python"), ("browser", "relative-browser")])
def test_persisted_configuration_rejects_untrusted_structure(operator_config, field, value):
    path, data = operator_config
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(config.ConfigurationError):
        config.snapshot()
    assert not config.configured()


@pytest.mark.parametrize("target,permissions", [("file",0o644),("directory",0o755),("ancestor",0o777)])
def test_persisted_configuration_rejects_loose_permissions(operator_config, target, permissions):
    path, _ = operator_config
    item = path if target == "file" else path.parent if target == "directory" else path.parent.parent
    item.chmod(permissions)
    with pytest.raises(config.ConfigurationError):
        config.snapshot()


@pytest.mark.parametrize("target", ["file", "directory", "factory"])
def test_configuration_rejects_symlinks(operator_config, target):
    path, data = operator_config
    item = path if target == "file" else path.parent if target == "directory" else Path(data["factory"].rpartition(":")[0])
    moved = item.with_name(item.name + "-moved")
    item.rename(moved)
    item.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(config.ConfigurationError):
        config.snapshot()


def test_configuration_rejects_duplicate_json_keys(operator_config):
    path, _ = operator_config
    path.write_text('{"factory":"/tmp/example.py:make_model","billing":"paid","billing":"unmetered","revision":"v1"}')
    with pytest.raises(config.ConfigurationError):
        config.snapshot()


def test_binding_provenance_invalidates_version_for_factory_billing_revision(operator_config, monkeypatch):
    path, data = operator_config
    first = BrowserUseProvider().manifest.version
    factory = Path(data["factory"].rpartition(":")[0])
    factory.write_text("# trusted factory version two\n")
    second = BrowserUseProvider().manifest.version
    assert second != first
    monkeypatch.setenv("FRANKENSURF_BROWSER_USE_BILLING", "paid")
    third = BrowserUseProvider().manifest.version
    assert third not in {first, second}
    monkeypatch.setenv("FRANKENSURF_BROWSER_USE_BINDING_REVISION", "new-external-model")
    fourth = BrowserUseProvider().manifest.version
    assert fourth not in {first, second, third}
    assert all(version.startswith(config.SDK_VERSION + "+") and len(version.split("+")[1]) == 64 for version in (first,second,third,fourth))


def test_factory_executes_immutable_source_not_changed_file(tmp_path):
    source = tmp_path / "factory.py"
    code = b"from frankensurf.browser_use_binding import ModelBinding\nclass LLM:\n    model='original'\n    async def ainvoke(self,*args,**kwargs): return None\ndef make_model(): return ModelBinding(LLM(),'unmetered')\n"
    source.write_bytes(code.replace(b"original", b"changed!"))
    binding = load_binding(source, "make_model", "unmetered", source_bytes=code)
    assert binding.llm.model == "original"
