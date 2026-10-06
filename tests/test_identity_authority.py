import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from frankensurf.identity import IdentityFailure, IdentityRegistry, LocalSecretVault


def enrolled(tmp_path):
    registry = IdentityRegistry(tmp_path / "authority" / "identities.json")
    registry.enroll_executor("local", endpoint="http://127.0.0.1:9331", user_data_dir=str(tmp_path / "chrome"),
                             profile_ref="browser-profile", geography="AU-Sydney", network_context="home-wifi")
    registry.enroll_identity("personal", executor_id="local", domains=["shop.test", "*.shop.test"],
                             image_domains=["cdn.test", "*.cdn.test"])
    return registry


def denied(code, operation):
    with pytest.raises(IdentityFailure) as error:
        operation()
    assert error.value.code == code
    return error.value


def test_private_atomic_registry_and_idempotent_enrollment(tmp_path):
    registry = enrolled(tmp_path)
    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(registry.path.parent.stat().st_mode) == 0o700
    before = registry.path.read_bytes()
    registry.enroll_identity("personal", executor_id="local", domains=["shop.test", "*.shop.test"],
                             image_domains=["cdn.test", "*.cdn.test"])
    assert registry.path.read_bytes() == before
    assert not list(registry.path.parent.glob(".identity-*"))
    assert registry.resolve("personal", "https://shop.test").generation == 1


def test_lookup_policy_denials_are_not_fallbacks(tmp_path):
    registry = enrolled(tmp_path)
    denied("IDENTITY_UNKNOWN", lambda: registry.resolve("missing", "https://shop.test"))
    denied("IDENTITY_PROVIDER_DENIED", lambda: registry.resolve("personal", "https://shop.test", provider="steel"))
    denied("IDENTITY_PROVIDER_DENIED", lambda: registry.resolve("personal", "https://shop.test", provider="http"))
    denied("IDENTITY_POLICY_DENIED", lambda: registry.resolve("personal", "https://shop.test", allow_local_browser=False))
    denied("IDENTITY_ACTION_DENIED", lambda: registry.resolve("personal", "https://shop.test", action="PURCHASE"))
    denied("IDENTITY_DOMAIN_DENIED", lambda: registry.resolve("personal", "https://shop.test.attacker.test"))
    denied("IDENTITY_DOMAIN_DENIED", lambda: registry.resolve("personal", "https://shop.test@attacker.test"))
    denied("IDENTITY_DOMAIN_DENIED", lambda: registry.resolve("personal", "file:///etc/passwd"))


def test_write_authority_is_explicit_and_rechecked(tmp_path):
    registry = enrolled(tmp_path)
    denied("IDENTITY_ACTION_DENIED", lambda: registry.resolve(
        "personal", "https://shop.test/draft", action="WRITE_REVERSIBLE"))
    registry.enroll_identity("writer", executor_id="local", domains=["shop.test"],
        allowed_actions=["READ_AUTHENTICATED", "WRITE_REVERSIBLE"])
    resolved = registry.resolve("writer", "https://shop.test/draft",
        action="WRITE_REVERSIBLE")
    assert resolved.action == "WRITE_REVERSIBLE"
    registry.enroll_identity("writer", executor_id="local", domains=["shop.test"],
        allowed_actions=["READ_AUTHENTICATED"])
    denied("IDENTITY_ACTION_DENIED", lambda: registry.recheck(resolved))


def test_identity_cannot_treat_public_read_or_unknown_strings_as_authority(tmp_path):
    registry = enrolled(tmp_path)
    for actions in (["READ_PUBLIC"], ["WRITE_REVERSIBLE", "WRITE_REVERSIBLE"],
                    ["WRITE_SOMETHING"]):
        denied("IDENTITY_CONFIG_INVALID", lambda actions=actions:
            registry.enroll_identity("invalid", executor_id="local",
                domains=["shop.test"], allowed_actions=actions))


def test_subdomain_and_photo_scope_are_explicit(tmp_path):
    registry = enrolled(tmp_path)
    resolved = registry.resolve("personal", "https://orders.shop.test/listing")
    assert registry.permits_url(resolved, "https://photos.cdn.test/a.jpg", image=True)
    assert registry.permits_url(resolved, "https://shop.test/a.jpg", image=True)
    denied("IDENTITY_DOMAIN_DENIED", lambda: registry.permits_url(resolved, "https://cdn.test", image=False))
    denied("IDENTITY_DOMAIN_DENIED", lambda: registry.permits_url(resolved, "https://notcdn.test/a.jpg", image=True))
    registry.enroll_identity("wildcard", executor_id="local", domains=["*.shop.test"])
    denied("IDENTITY_DOMAIN_DENIED", lambda: registry.resolve("wildcard", "https://shop.test"))
    assert registry.resolve("wildcard", "https://sub.shop.test").id == "wildcard"


@pytest.mark.parametrize("mode", ["SYNC_ALLOWED", "CLOUD_MANAGED", "EPHEMERAL"])
def test_unimplemented_authority_mode_never_implicitly_exports(tmp_path, mode):
    registry = enrolled(tmp_path)
    registry.enroll_identity("other", executor_id="local", domains=["shop.test"], authority_mode=mode)
    denied("IDENTITY_AUTHORITY_UNSUPPORTED", lambda: registry.resolve("other", "https://shop.test"))


def test_health_and_revocation_are_reloaded_on_every_decision(tmp_path):
    registry = enrolled(tmp_path)
    resolved = registry.resolve("personal", "https://shop.test")
    another_process_view = IdentityRegistry(registry.path)
    another_process_view.set_executor_health("local", "offline")
    denied("IDENTITY_EXECUTOR_OFFLINE", lambda: registry.recheck(resolved))
    another_process_view.set_executor_health("local", "healthy")
    denied("IDENTITY_CHANGED", lambda: registry.recheck(resolved))
    resolved = registry.resolve("personal", "https://shop.test")
    another_process_view.set_identity_health("personal", "reauth")
    denied("IDENTITY_REAUTH_REQUIRED", lambda: registry.recheck(resolved))
    another_process_view.set_identity_health("personal", "challenge")
    denied("CAPTCHA", lambda: registry.resolve("personal", "https://shop.test"))
    another_process_view.revoke("personal")
    denied("IDENTITY_REVOKED", lambda: registry.recheck(resolved))
    revoked_generation = registry.status("personal")["identities"][0]["generation"]
    registry.revoke("personal")
    assert registry.status("personal")["identities"][0]["generation"] == revoked_generation


def test_profile_network_and_domain_changes_fence_existing_resolution(tmp_path):
    registry = enrolled(tmp_path)
    resolved = registry.resolve("personal", "https://shop.test")
    registry.enroll_executor("local", endpoint="http://127.0.0.1:9331", user_data_dir=str(tmp_path / "chrome"),
                             profile_ref="browser-profile", profile_version=2, geography="AU-Sydney", network_context="new-network")
    denied("IDENTITY_CHANGED", lambda: registry.recheck(resolved))
    current = registry.resolve("personal", "https://shop.test")
    assert current.cache_scope != resolved.cache_scope
    registry.enroll_identity("personal", executor_id="local", domains=["elsewhere.test"])
    denied("IDENTITY_DOMAIN_DENIED", lambda: registry.recheck(current))


def test_safe_status_and_resolved_repr_omit_private_binding_and_checks(tmp_path):
    registry = enrolled(tmp_path)
    registry.enroll_identity("personal", executor_id="local", domains=["shop.test"],
                             auth_check={"url": "https://shop.test/account/private-path", "authenticated_selector": "#private-proof",
                                         "login_selector": "#private-login"})
    status = json.dumps(registry.status())
    resolved = registry.resolve("personal", "https://shop.test")
    for private in (resolved.endpoint, resolved.user_data_dir, "browser-profile", "#private-proof", "private-path", "#private-login"):
        assert private not in status
        assert private not in repr(resolved)
    assert resolved.profile_binding["context_selector"] == "default"
    with pytest.raises(TypeError):
        resolved.profile_binding["user_data_dir"] = "/different"
    with pytest.raises(TypeError):
        resolved.auth_check["url"] = "https://attacker.test"


@pytest.mark.parametrize("endpoint", ["https://api.steel.dev", "http://example.test:9331", "http://user:secret@127.0.0.1:9331",
                                      "http://127.0.0.1:9331/?token=secret", "http://127.0.0.1:bad", "ws://127.0.0.1:9331"])
def test_executor_endpoint_rejects_cloud_credentials_and_ambiguity(tmp_path, endpoint):
    registry = IdentityRegistry(tmp_path / "identities.json")
    error = denied("IDENTITY_CONFIG_INVALID", lambda: registry.enroll_executor("local", endpoint=endpoint, user_data_dir=str(tmp_path)))
    assert endpoint not in str(error)
    assert "secret" not in str(error)
    assert not registry.path.exists()


def test_strict_schema_rejects_credentials_in_registry_and_unsafe_files(tmp_path):
    registry = enrolled(tmp_path)
    data = json.loads(registry.path.read_text())
    data["identities"]["personal"]["cookies"] = "never-store-this"
    registry.path.write_text(json.dumps(data))
    denied("IDENTITY_REGISTRY_INVALID", lambda: registry.status())
    del data["identities"]["personal"]["cookies"]
    registry.path.write_text(json.dumps(data))
    registry.path.chmod(0o644)
    denied("IDENTITY_STORE_UNSAFE", lambda: registry.resolve("personal", "https://shop.test"))
    registry.path.chmod(0o600)
    registry.path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(data))
    target.chmod(0o600)
    registry.path.symlink_to(target)
    denied("IDENTITY_STORE_UNSAFE", lambda: registry.status())


def test_auth_check_requires_positive_proof_and_domain_scope(tmp_path):
    registry = enrolled(tmp_path)
    for check in ({"url": "https://shop.test", "login_selector": "#login"},
                  {"url": "https://attacker.test", "authenticated_selector": "#account"},
                  {"url": "https://shop.test", "authenticated_selector": "#account", "cookie": "secret"}):
        denied("IDENTITY_CONFIG_INVALID", lambda: registry.enroll_identity("invalid", executor_id="local", domains=["shop.test"], auth_check=check))
    assert registry.status()["identity_count"] == 1


def test_nonblocking_leases_are_shared_across_instances_and_identity_aliases(tmp_path):
    registry = enrolled(tmp_path)
    registry.enroll_identity("alias", executor_id="local", domains=["shop.test"])
    second_registry = IdentityRegistry(registry.path)
    resolved = registry.resolve("personal", "https://shop.test")
    alias = second_registry.resolve("alias", "https://shop.test")
    with registry.lease(resolved):
        denied("IDENTITY_IN_USE", lambda: second_registry.lease(resolved).__enter__())
        denied("IDENTITY_IN_USE", lambda: second_registry.lease(alias).__enter__())
    with second_registry.lease(alias):
        pass
    assert all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in registry.lease_dir.iterdir())


def test_same_profile_on_another_endpoint_remains_exclusive(tmp_path):
    registry = enrolled(tmp_path)
    registry.enroll_executor("alias-executor", endpoint="http://127.0.0.1:9444", user_data_dir=str(tmp_path / "chrome"))
    registry.enroll_identity("alias", executor_id="alias-executor", domains=["shop.test"])
    with registry.lease(registry.resolve("personal", "https://shop.test")):
        denied("IDENTITY_IN_USE", lambda: registry.lease(registry.resolve("alias", "https://shop.test")).__enter__())


def test_cross_process_lease_and_release_after_exception(tmp_path):
    registry = enrolled(tmp_path)
    script = """import sys
from frankensurf.identity import IdentityRegistry, IdentityFailure
registry=IdentityRegistry(sys.argv[1])
try:
    with registry.lease(registry.resolve('personal','https://shop.test')):
        print('acquired')
except IdentityFailure as error:
    print(error.code)
"""
    with pytest.raises(RuntimeError):
        with registry.lease(registry.resolve("personal", "https://shop.test")):
            child = subprocess.run([sys.executable, "-c", script, str(registry.path)], check=True, capture_output=True, text=True)
            assert child.stdout.strip() == "IDENTITY_IN_USE"
            raise RuntimeError("simulated executor failure")
    child = subprocess.run([sys.executable, "-c", script, str(registry.path)], check=True, capture_output=True, text=True)
    assert child.stdout.strip() == "acquired"


def test_revoke_between_resolution_and_lease_is_fenced(tmp_path):
    registry = enrolled(tmp_path)
    resolved = registry.resolve("personal", "https://shop.test")
    registry.revoke("personal")
    denied("IDENTITY_REVOKED", lambda: registry.lease(resolved).__enter__())


def test_vault_is_separate_opaque_private_and_not_loaded_by_authority(tmp_path):
    vault = LocalSecretVault(tmp_path / "credentials")
    secret = b"test-secret-with-browser-unrelated-credential"
    reference = vault.put(secret)
    registry = enrolled(tmp_path)
    registry.enroll_identity("personal", executor_id="local", domains=["shop.test"], vault_refs=[reference])
    resolved = registry.resolve("personal", "https://shop.test")
    assert secret.decode() not in registry.path.read_text()
    assert reference not in json.dumps(registry.status())
    assert reference not in repr(resolved)
    assert vault.get(reference) == secret
    assert stat.S_IMODE(vault.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(next(vault.directory.iterdir()).stat().st_mode) == 0o600
    denied("VAULT_REFERENCE_INVALID", lambda: vault.get("../../profile/Cookies"))
    vault.delete(reference)
    assert registry.resolve("personal", "https://shop.test").id == "personal"
    denied("VAULT_REFERENCE_MISSING", lambda: vault.get(reference))



def test_windows_absolute_profiles_are_canonical_without_touching_them(tmp_path):
    registry = IdentityRegistry(tmp_path / "authority" / "identities.json")
    registry.enroll_executor("windows", endpoint="http://127.0.0.1:9331", user_data_dir=r"C:\Users\Yao\Chrome\..\Chrome Profile")
    registry.enroll_identity("personal", executor_id="windows", domains=["shop.test"])
    resolved = registry.resolve("personal", "https://shop.test")
    assert resolved.user_data_dir == r"c:\users\yao\chrome profile"
    registry.enroll_executor("windows", endpoint="http://127.0.0.1:9331", user_data_dir="c:/users/yao/CHROME PROFILE")
    assert registry.resolve("personal", "https://shop.test").cache_scope == resolved.cache_scope
    assert registry.resolve("personal", "https://shop.test").executor_generation == 1


@pytest.mark.parametrize("path", [r"C:Users\Yao\Chrome", r"\\server\share\Chrome", "//server/share/Chrome", r"\\?\C:\Users\Yao\Chrome", "relative/profile"])
def test_nonlocal_or_relative_profile_bindings_are_rejected(tmp_path, path):
    registry = IdentityRegistry(tmp_path / "authority" / "identities.json")
    denied("IDENTITY_CONFIG_INVALID", lambda: registry.enroll_executor("windows", endpoint="http://127.0.0.1:9331", user_data_dir=path))
    assert not registry.path.exists()


def test_directory_failure_is_typed_and_omits_private_path(tmp_path, monkeypatch):
    import frankensurf.identity as module
    original = Path.mkdir
    def deny_private(self, *args, **kwargs):
        if self.name == "private-secret-name":
            raise PermissionError("sensitive private-secret-name")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", deny_private)
    registry = IdentityRegistry(tmp_path / "private-secret-name" / "identities.json")
    error = denied("IDENTITY_STORE_UNSAFE", lambda: registry.enroll_executor("local", endpoint="http://127.0.0.1:9331", user_data_dir=str(tmp_path)))
    assert "private-secret-name" not in str(error)


def test_atomic_write_failure_is_typed_and_keeps_previous_registry(tmp_path, monkeypatch):
    import frankensurf.identity as module
    registry = enrolled(tmp_path)
    before = registry.path.read_bytes()
    def deny_replace(*args, **kwargs):
        raise PermissionError("sensitive private file destination")
    monkeypatch.setattr(module.os, "replace", deny_replace)
    error = denied("IDENTITY_STORE_UNSAFE", lambda: registry.set_executor_health("local", "offline"))
    assert "destination" not in str(error)
    assert registry.path.read_bytes() == before
    assert not list(registry.path.parent.glob(".identity-*"))
