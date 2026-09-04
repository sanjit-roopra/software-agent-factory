from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from pydantic import ValidationError

from software_agent_factory.config import load_config


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "factory.yaml"
    config_path.write_text(dedent(content).strip() + "\n", encoding="utf-8")
    return config_path


def test_load_config_from_explicit_path_expands_data_dir(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        factory:
          data_dir: "~/.software-factory-test"
          retries:
            same_model_attempts: 2
            max_total_attempts: 6
        models:
          triage: {model: "claude-sonnet-5", reasoning: "medium"}
          refiner: {model: "claude-opus-5", reasoning: "high"}
          researcher: {model: "gpt-5.6-sol", reasoning: "high"}
          planner: {model: "claude-opus-5", reasoning: "high"}
          workers:
            L0: {model: "mai-code-1.1-flash", reasoning: "medium"}
            L1: {model: "claude-sonnet-5", reasoning: "medium"}
            L2: {model: "claude-opus-5", reasoning: "high"}
            L3: {model: "claude-opus-5", reasoning: "high"}
          tester: {model: "claude-sonnet-5", reasoning: "high"}
          reviewer: {model: "gpt-5.6-sol", reasoning: "high"}
        repository:
          branch_prefix: "factory/"
          command_timeout_seconds: 900
          commands:
            install: ["uv sync"]
            verify: ["uv run pytest"]
            build: []
        risk:
          R0: {human_approval: false}
          R1: {human_approval: false}
          R2: {human_approval: true}
          R3: {human_approval: true}
        """,
    )

    config = load_config(config_path)

    assert config.data_dir == Path.home() / ".software-factory-test"
    assert config.retries.same_model_attempts == 2
    assert config.repository.commands.verify == ["uv run pytest"]


def test_load_config_uses_packaged_defaults() -> None:
    config = load_config()

    assert config.data_dir == Path.home() / ".software-factory"
    assert config.models.reviewer.model == "gpt-5.6-sol"
    assert config.repository.branch_prefix == "factory/"
    assert config.risk["R2"].human_approval is True


def test_config_rejects_non_positive_limits(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        factory:
          data_dir: "~/.software-factory-test"
          retries:
            same_model_attempts: 0
            max_total_attempts: 6
        models:
          triage: {model: "claude-sonnet-5", reasoning: "medium"}
          refiner: {model: "claude-opus-5", reasoning: "high"}
          researcher: {model: "gpt-5.6-sol", reasoning: "high"}
          planner: {model: "claude-opus-5", reasoning: "high"}
          workers:
            L0: {model: "mai-code-1.1-flash", reasoning: "medium"}
            L1: {model: "claude-sonnet-5", reasoning: "medium"}
            L2: {model: "claude-opus-5", reasoning: "high"}
            L3: {model: "claude-opus-5", reasoning: "high"}
          tester: {model: "claude-sonnet-5", reasoning: "high"}
          reviewer: {model: "gpt-5.6-sol", reasoning: "high"}
        repository:
          branch_prefix: "factory/"
          command_timeout_seconds: 900
          commands:
            install: []
            verify: []
            build: []
        risk:
          R0: {human_approval: false}
          R1: {human_approval: false}
          R2: {human_approval: true}
          R3: {human_approval: true}
        """,
    )

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_config_rejects_reviewer_family_matching_workers(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        factory:
          data_dir: "~/.software-factory-test"
          retries:
            same_model_attempts: 2
            max_total_attempts: 6
        models:
          triage: {model: "claude-sonnet-5", reasoning: "medium"}
          refiner: {model: "claude-opus-5", reasoning: "high"}
          researcher: {model: "gpt-5.6-sol", reasoning: "high"}
          planner: {model: "claude-opus-5", reasoning: "high"}
          workers:
            L0: {model: "mai-code-1.1-flash", reasoning: "medium"}
            L1: {model: "claude-sonnet-5", reasoning: "medium"}
            L2: {model: "claude-opus-5", reasoning: "high"}
            L3: {model: "claude-opus-5", reasoning: "high"}
          tester: {model: "claude-sonnet-5", reasoning: "high"}
          reviewer: {model: "claude-haiku-4.5", reasoning: "high"}
        repository:
          branch_prefix: "factory/"
          command_timeout_seconds: 900
          commands:
            install: []
            verify: []
            build: []
        risk:
          R0: {human_approval: false}
          R1: {human_approval: false}
          R2: {human_approval: true}
          R3: {human_approval: true}
        """,
    )

    with pytest.raises(ValidationError, match="reviewer model family"):
        load_config(config_path)


_MINIMAL_CONFIG = """
factory:
  data_dir: "~/.software-factory-test"
  retries:
    same_model_attempts: 2
    max_total_attempts: 6
models:
  triage: {model: "claude-sonnet-5", reasoning: "medium"}
  refiner: {model: "claude-opus-5", reasoning: "high"}
  researcher: {model: "gpt-5.6-sol", reasoning: "high"}
  planner: {model: "claude-opus-5", reasoning: "high"}
  workers:
    L0: {model: "mai-code-1.1-flash", reasoning: "medium"}
    L1: {model: "claude-sonnet-5", reasoning: "medium"}
    L2: {model: "claude-opus-5", reasoning: "high"}
    L3: {model: "claude-opus-5", reasoning: "high"}
  tester: {model: "claude-sonnet-5", reasoning: "high"}
  reviewer: {model: "gpt-5.6-sol", reasoning: "high"}
repository:
  branch_prefix: "factory/"
  command_timeout_seconds: 900
risk:
  R0: {human_approval: false}
  R1: {human_approval: false}
  R2: {human_approval: true}
  R3: {human_approval: true}
"""


def test_phase_1_config_still_loads_and_new_sections_default(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, _MINIMAL_CONFIG))

    assert config.agent_timeout_seconds == 900
    assert config.factory.agent_timeout_seconds == 900
    assert config.scope_drift.max_replans == 1
    assert config.pull_request.enabled is False
    assert config.pull_request.remote == "origin"
    assert config.pull_request.base_branch is None
    assert config.pull_request.draft is True
    assert config.pull_request.allowed_hosts == ["github.com"]
    assert config.ci.enabled is False
    assert config.ci.poll_interval_seconds == 30
    assert config.ci.max_wait_seconds == 1800
    assert config.ci.repair_attempts == 3
    assert config.scheduler.enabled is False
    assert config.scheduler.poll_interval_seconds == 30
    assert config.scheduler.max_concurrent_tasks == 1
    assert config.scheduler.stall_timeout_seconds == 900
    assert config.scheduler.required_label == "agent-ready"
    assert config.scheduler.max_runs_per_day == 20
    assert config.repository.env_passthrough == []
    assert config.repository.log_capture_bytes == 32768
    assert config.repository.max_changed_files == 100
    assert ".env" in config.repository.protected_file_patterns
    assert any(pattern.endswith("*.pem") for pattern in config.repository.protected_file_patterns)


def test_packaged_default_config_publishes_every_section() -> None:
    config = load_config()

    assert config.agent_timeout_seconds == 900
    assert config.scope_drift.max_replans == 1
    assert config.pull_request.allowed_hosts == ["github.com"]
    assert config.ci.repair_attempts == 3
    assert config.scheduler.required_label == "agent-ready"
    assert config.scheduler.max_runs_per_day == 20
    assert config.repository.log_capture_bytes == 32768
    assert config.repository.max_changed_files == 100
    assert config.repository.protected_file_patterns


def test_ci_requires_pull_request_enabled(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        _MINIMAL_CONFIG + "\nci:\n  enabled: true\n",
    )

    with pytest.raises(ValidationError, match="ci.enabled requires pull_request.enabled"):
        load_config(config_path)


def test_ci_enabled_is_accepted_with_pull_requests_enabled(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        _MINIMAL_CONFIG + "\nci:\n  enabled: true\npull_request:\n  enabled: true\n",
    )

    config = load_config(config_path)

    assert config.ci.enabled is True
    assert config.pull_request.enabled is True



def _config_with(tmp_path: Path, overrides: dict[str, object], name: str = "factory") -> Path:
    payload = yaml.safe_load(_MINIMAL_CONFIG)
    for section, values in overrides.items():
        assert isinstance(values, dict)
        payload.setdefault(section, {}).update(values)
    config_path = tmp_path / f"{name}.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_path


def test_scheduler_concurrency_is_capped_at_two(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="max_concurrent_tasks must be 1 or 2"):
        load_config(_config_with(tmp_path, {"scheduler": {"max_concurrent_tasks": 3}}))

    allowed = load_config(
        _config_with(tmp_path, {"scheduler": {"max_concurrent_tasks": 2}}, name="two")
    )

    assert allowed.scheduler.max_concurrent_tasks == 2


def test_max_runs_per_day_accepts_null_for_unbounded_and_a_custom_positive_value(
    tmp_path: Path,
) -> None:
    unbounded = load_config(
        _config_with(tmp_path, {"scheduler": {"max_runs_per_day": None}}, name="unbounded")
    )
    assert unbounded.scheduler.max_runs_per_day is None

    custom = load_config(
        _config_with(tmp_path, {"scheduler": {"max_runs_per_day": 5}}, name="custom")
    )
    assert custom.scheduler.max_runs_per_day == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"factory": {"agent_timeout_seconds": 0}},
        {"ci": {"poll_interval_seconds": 0}},
        {"ci": {"max_wait_seconds": 0}},
        {"ci": {"repair_attempts": 0}},
        {"ci": {"poll_interval_seconds": 600, "max_wait_seconds": 60}},
        {"scheduler": {"poll_interval_seconds": 0}},
        {"scheduler": {"max_concurrent_tasks": 0}},
        {"scheduler": {"stall_timeout_seconds": 0}},
        {"scheduler": {"poll_interval_seconds": 600, "stall_timeout_seconds": 60}},
        {"scheduler": {"max_runs_per_day": 0}},
        {"scheduler": {"max_runs_per_day": -1}},
        {"repository": {"log_capture_bytes": 0}},
        {"repository": {"max_changed_files": 0}},
        {"scope_drift": {"max_replans": -1}},
    ],
)
def test_config_rejects_invalid_phase_values(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        load_config(_config_with(tmp_path, overrides))


def test_config_rejects_unknown_sections_and_keys(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(_config_with(tmp_path, {"integrations": {"jira": True}}))

    with pytest.raises(ValidationError):
        load_config(_config_with(tmp_path, {"ci": {"webhook_url": "https://example.invalid"}}))


def test_config_rejects_invalid_pull_request_and_env_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="allowed_hosts must not be empty"):
        load_config(_config_with(tmp_path, {"pull_request": {"allowed_hosts": []}}))

    with pytest.raises(ValidationError, match="bare hostnames"):
        load_config(
            _config_with(tmp_path, {"pull_request": {"allowed_hosts": ["github.com/acme"]}})
        )

    with pytest.raises(ValidationError, match="env_passthrough"):
        load_config(_config_with(tmp_path, {"repository": {"env_passthrough": ["GH_TOKEN=x"]}}))

    with pytest.raises(ValidationError, match="base_branch"):
        load_config(_config_with(tmp_path, {"pull_request": {"base_branch": "  "}}))


# -- packaged default vs. published example --------------------------------


def _packaged_default_path() -> Path:
    import software_agent_factory
    from software_agent_factory.config import DEFAULT_CONFIG_FILENAME

    return Path(software_agent_factory.__file__).parent / DEFAULT_CONFIG_FILENAME


def _example_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "factory.example.yaml"


def test_example_config_mirrors_the_packaged_default_exactly() -> None:
    """``config/factory.example.yaml`` must stay a byte-for-byte-equivalent
    copy of the packaged defaults (comments aside), so copying it can never
    silently change behavior."""
    default_payload = yaml.safe_load(_packaged_default_path().read_text(encoding="utf-8"))
    example_payload = yaml.safe_load(_example_path().read_text(encoding="utf-8"))

    assert example_payload == default_payload


def test_example_config_validates_and_agrees_with_the_packaged_default() -> None:
    default_config = load_config()
    example_config = load_config(_example_path())

    assert example_config.model_dump() == default_config.model_dump()


def test_both_configs_publish_the_same_structural_keys() -> None:
    default_payload = yaml.safe_load(_packaged_default_path().read_text(encoding="utf-8"))
    example_payload = yaml.safe_load(_example_path().read_text(encoding="utf-8"))

    def keys(payload: object, prefix: str = "") -> set[str]:
        if not isinstance(payload, dict):
            return set()
        found: set[str] = set()
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.add(path)
            found |= keys(value, path)
        return found

    assert keys(example_payload) == keys(default_payload)
    # Every section the loader knows about is published, not just the required
    # ones, so an operator can discover every knob from the example file.
    assert {
        "factory",
        "models",
        "repository",
        "scope_drift",
        "pull_request",
        "ci",
        "scheduler",
        "risk",
    } <= keys(example_payload)


def test_all_integrations_are_disabled_by_default() -> None:
    """No command may reach GitHub or a paid model without explicit opt-in."""
    for config in (load_config(), load_config(_example_path())):
        assert config.pull_request.enabled is False
        assert config.ci.enabled is False
        assert config.scheduler.enabled is False
