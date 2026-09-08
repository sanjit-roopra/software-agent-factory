from pathlib import Path

import pytest
from factory_testing import build_config
from pydantic import ValidationError

from software_agent_factory.config import FactoryConfig, MergeConfig, ScopeDriftConfig, load_config
from software_agent_factory.workflow import delivery_policy_fingerprint


def _payload(tmp_path: Path) -> dict:
    payload = build_config(
        tmp_path,
        verify=["uv run --no-sync pytest"],
        pull_request={"enabled": True, "draft": False, "base_branch": "main"},
        ci={"enabled": True},
    ).model_dump(mode="json")
    payload["merge"] = {
        "enabled": True,
        "allowed_repositories": ["acme/example"],
        "required_checks": ["quality", "tests (Python 3.13)"],
    }
    return payload


def test_autonomous_delivery_is_disabled_by_default() -> None:
    config = load_config()
    assert not config.merge.enabled
    assert config.merge.allowed_repositories == []
    assert config.merge.required_checks == []
    assert config.scope_drift.approved_sensitive_files == []


def test_explicit_complete_delivery_policy_is_valid(tmp_path: Path) -> None:
    config = FactoryConfig.model_validate(_payload(tmp_path))
    assert config.merge.enabled
    assert config.merge.method == "squash"


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("pull_request", "enabled", False, "requires"),
        ("ci", "enabled", False, "requires"),
        ("pull_request", "draft", True, "draft=false"),
        ("pull_request", "base_branch", None, "explicit"),
        ("merge", "allowed_repositories", [], "allowed_repositories"),
        ("merge", "required_checks", [], "required_checks"),
        ("repository", "commands", {"verify": []}, "commands.verify"),
    ],
)
def test_merge_requires_explicit_gates(
    tmp_path: Path, section: str, field: str, value: object, message: str
) -> None:
    payload = _payload(tmp_path)
    payload[section][field] = value
    with pytest.raises(ValidationError, match=message):
        FactoryConfig.model_validate(payload)


@pytest.mark.parametrize(
    "repository",
    [
        "*",
        "acme/*",
        "acme",
        "https://github.com/acme/repo",
        "acme/..",
        "acme/repo.git",
        " acme/repo",
    ],
)
def test_merge_repository_allowlist_is_exact(repository: str) -> None:
    with pytest.raises(ValidationError, match="OWNER/REPO"):
        MergeConfig(allowed_repositories=[repository])


def test_merge_rejects_duplicate_repositories_and_checks() -> None:
    with pytest.raises(ValidationError, match="unique"):
        MergeConfig(allowed_repositories=["acme/repo", "ACME/REPO"])
    with pytest.raises(ValidationError, match="unique"):
        MergeConfig(required_checks=["quality", "quality"])


@pytest.mark.parametrize("check", ["", " ", " quality", "quality\nother"])
def test_merge_rejects_empty_or_malformed_checks(check: str) -> None:
    with pytest.raises(ValidationError, match="check names"):
        MergeConfig(required_checks=[check])


@pytest.mark.parametrize("method", ["admin", "auto", "force", ""])
def test_merge_method_cannot_bypass_protection(method: str) -> None:
    with pytest.raises(ValidationError):
        MergeConfig(method=method)


@pytest.mark.parametrize(
    "path",
    [
        "",
        " ",
        "/pyproject.toml",
        "../pyproject.toml",
        "src/../pyproject.toml",
        "./pyproject.toml",
        "**/pyproject.toml",
        ".github/workflows/*.yml",
        "pyproject.toml/",
        "src//pyproject.toml",
        "src\\pyproject.toml",
        ".git/config",
        "C:/pyproject.toml",
        "pyproject.toml\x00",
        "pyproject.toml\n",
    ],
)
def test_sensitive_approval_requires_canonical_exact_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="repository-relative"):
        ScopeDriftConfig(approved_sensitive_files=[path])


def test_sensitive_approvals_are_unique() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ScopeDriftConfig(approved_sensitive_files=["uv.lock", "uv.lock"])


def test_sensitive_approvals_accept_explicit_bootstrap_files() -> None:
    paths = ["pyproject.toml", "uv.lock", ".github/workflows/ci.yml"]
    assert ScopeDriftConfig(approved_sensitive_files=paths).approved_sensitive_files == paths


def test_recovery_fingerprint_binds_quality_and_merge_policy(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    config = FactoryConfig.model_validate(payload)
    fingerprint = delivery_policy_fingerprint(config)
    payload["merge"]["required_checks"] = ["different"]
    assert fingerprint != delivery_policy_fingerprint(FactoryConfig.model_validate(payload))
    payload = config.model_dump(mode="json")
    payload["scope_drift"]["approved_sensitive_files"] = ["uv.lock"]
    assert fingerprint != delivery_policy_fingerprint(FactoryConfig.model_validate(payload))
    payload = config.model_dump(mode="json")
    payload["repository"]["commands"]["verify"] = ["true"]
    assert fingerprint != delivery_policy_fingerprint(FactoryConfig.model_validate(payload))


def test_recovery_fingerprint_is_stable_across_model_routing_and_key_order(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    config = FactoryConfig.model_validate(payload)
    payload["factory"]["data_dir"] = str(tmp_path / "elsewhere")
    payload = dict(reversed(list(payload.items())))
    assert delivery_policy_fingerprint(config) == delivery_policy_fingerprint(
        FactoryConfig.model_validate(payload)
    )
