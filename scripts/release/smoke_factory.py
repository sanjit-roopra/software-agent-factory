"""Offline smoke-test an installed or frozen ``factory`` executable.

Exercises exactly what a downloaded release promises and nothing that costs
money or touches the network: ``--version``, ``--help``, ``doctor``,
``runs``, ``status``, a full ``--runtime fake`` run, the read-only
``service status`` query, and the explicit prerequisite failure a machine
without ``git`` must produce. No command here installs, loads or removes a
launchd service, so running this on a developer machine cannot disturb an
existing one.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

SMOKE_PARENT_MARKER = ".software-agent-factory-smoke-parent"
SMOKE_WORKSPACE_PREFIX = "smoke-run-"
INSTALL_FILENAME = "INSTALL.txt"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Exit code the CLI uses for "this environment or configuration cannot do
#: what you asked" (``cli.CONFIG_ERROR_EXIT_CODE``), including a missing
#: external prerequisite.
PREREQUISITE_EXIT_CODE = 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--factory-executable", type=Path)
    group.add_argument("--archive", type=Path)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--expect-version", required=True)
    parser.add_argument("--expect-architecture")
    return parser.parse_args()


def _run(
    *args: str,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_AUTHOR_NAME"] = "Factory Smoke"
    env["GIT_AUTHOR_EMAIL"] = "factory-smoke@example.invalid"
    env["GIT_COMMITTER_NAME"] = "Factory Smoke"
    env["GIT_COMMITTER_EMAIL"] = "factory-smoke@example.invalid"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(env_overrides or {})
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _assert_ok(result: subprocess.CompletedProcess[str], *, contains: str) -> None:
    if result.returncode != 0:
        details = "\n".join(
            (
                f"Command failed ({result.returncode}): {result.args}",
                "STDOUT:",
                result.stdout,
                "STDERR:",
                result.stderr,
            )
        )
        raise SystemExit(details)
    if contains not in result.stdout and contains not in result.stderr:
        details = "\n".join(
            (
                f"Expected to find {contains!r} in output for {result.args}",
                "STDOUT:",
                result.stdout,
                "STDERR:",
                result.stderr,
            )
        )
        raise SystemExit(details)


def _absolute_without_resolution(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def _existing_path_chain(path: Path) -> tuple[Path, ...]:
    absolute = _absolute_without_resolution(path)
    current = Path(absolute.anchor)
    paths = [current]
    for part in absolute.parts[1:]:
        current /= part
        if current.exists():
            paths.append(current)
    return tuple(paths)


def _reject_symlink_ambiguity(path: Path) -> None:
    for candidate in _existing_path_chain(path):
        if candidate.is_symlink():
            raise SystemExit(f"Refusing to use symlinked smoke path: {path}")


def _is_same_path_or_ancestor(candidate: Path, protected: Path) -> bool:
    return candidate == protected or candidate in protected.parents


def _has_distinctive_smoke_component(path: Path) -> bool:
    return "smoke" in path.name.lower() or "smoke" in path.parent.name.lower()


def _validate_workspace_parent(path: Path) -> Path:
    candidate = _absolute_without_resolution(path)
    anchor = Path(candidate.anchor)
    home = Path.home()
    protected_paths = (anchor, home, REPO_ROOT)

    _reject_symlink_ambiguity(candidate)
    if any(_is_same_path_or_ancestor(candidate, protected) for protected in protected_paths):
        raise SystemExit(f"Refusing to use a protected workspace root: {candidate}")
    if not _has_distinctive_smoke_component(candidate):
        raise SystemExit(
            "Refusing to use a non-distinctive workspace root; include 'smoke' in the path."
        )

    if candidate.exists():
        if not candidate.is_dir():
            raise SystemExit(f"Smoke workspace root must be a directory: {candidate}")
        for child in candidate.iterdir():
            if child.name == SMOKE_PARENT_MARKER and child.is_file():
                continue
            if child.is_symlink():
                raise SystemExit(f"Refusing to use symlinked smoke path: {child}")
            if child.name.startswith(SMOKE_WORKSPACE_PREFIX) and child.is_dir():
                continue
            raise SystemExit(
                f"Refusing to reuse non-marker/non-smoke workspace root contents: {candidate}"
            )
    else:
        candidate.mkdir(parents=True, exist_ok=True)

    marker = candidate / SMOKE_PARENT_MARKER
    marker.write_text("Dedicated to software-agent-factory smoke workspaces.\n", encoding="utf-8")
    return candidate


def _create_workspace(parent: Path) -> Path:
    for _attempt in range(16):
        workspace = parent / f"{SMOKE_WORKSPACE_PREFIX}{uuid4().hex[:12]}"
        if workspace.exists():
            continue
        workspace.mkdir()
        return workspace
    raise SystemExit(f"Unable to allocate a unique smoke workspace beneath {parent}")


def _cleanup_workspace(workspace: Path) -> None:
    if workspace.name.startswith(SMOKE_WORKSPACE_PREFIX) and workspace.parent.is_dir():
        shutil.rmtree(workspace, ignore_errors=True)


def _configure_repo(repo: Path) -> None:
    _assert_ok(
        _run("git", "config", "user.email", "factory-smoke@example.invalid", cwd=repo),
        contains="",
    )
    _assert_ok(_run("git", "config", "user.name", "Factory Smoke", cwd=repo), contains="")
    _assert_ok(_run("git", "config", "commit.gpgsign", "false", cwd=repo), contains="")


def _seed_repo(repo: Path) -> None:
    _assert_ok(
        _run("git", "init", "-b", "main", cwd=repo),
        contains="Initialized empty Git repository",
    )
    _configure_repo(repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _assert_ok(_run("git", "add", "-A", cwd=repo), contains="")
    _assert_ok(_run("git", "commit", "-m", "initial commit", cwd=repo), contains="1 file changed")


def _smoke_cli(executable: Path, data_dir: Path, *, expect_version: str) -> None:
    _assert_executable_mode(executable)
    _assert_ok(_run(str(executable), "--help"), contains="factory")
    _assert_ok(_run(str(executable), "--version"), contains=expect_version)
    _assert_ok(
        _run(str(executable), "runs", "--data-dir", str(data_dir)),
        contains="no runs found",
    )


def _smoke_doctor(
    executable: Path,
    data_dir: Path,
    *,
    expect_architecture: str | None,
) -> None:
    """``factory doctor`` must pass on a machine with ``git`` but no
    ``gh``/``copilot`` requirement, and must never make a paid call.

    ``--data-dir`` keeps the writability probe inside the smoke workspace
    instead of creating the operator's real data directory as a side effect.
    """
    result = _run(str(executable), "doctor", "--json", "--data-dir", str(data_dir))
    _assert_ok(result, contains='"success"')
    payload = json.loads(result.stdout)
    if payload.get("success") is not True:
        raise SystemExit(f"factory doctor reported failures:\n{result.stdout}")
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    if statuses.get("git") != "ok":
        raise SystemExit(f"factory doctor did not find git:\n{result.stdout}")
    if expect_architecture is not None:
        platform_check = next(
            (check for check in payload["checks"] if check["name"] == "platform"),
            None,
        )
        if platform_check is None or expect_architecture not in platform_check["message"]:
            raise SystemExit(
                "Frozen executable architecture does not match the archive label "
                f"{expect_architecture!r}:\n{result.stdout}"
            )
    for optional in ("gh", "copilot"):
        if statuses.get(optional) == "error":
            raise SystemExit(
                f"factory doctor required {optional} for a default offline run:\n{result.stdout}"
            )


def _smoke_status(executable: Path, data_dir: Path) -> None:
    """``factory status`` is read-only and must report the persisted run."""
    result = _run(str(executable), "status", "--json", "--data-dir", str(data_dir))
    _assert_ok(result, contains='"snapshot"')
    payload = json.loads(result.stdout)
    if payload["snapshot"]["counts"]["succeeded"] < 1:
        raise SystemExit(f"factory status did not report the smoke run:\n{result.stdout}")


def _smoke_service_status_is_read_only(executable: Path) -> None:
    """``factory service status`` must answer without touching launchd state.

    On macOS it reports the (absent) LaunchAgent; anywhere else it must
    refuse explicitly rather than pretend. Neither path installs, loads or
    removes anything, so this is safe to run on a developer machine that
    already has a service installed.
    """
    result = _run(str(executable), "service", "status")
    if platform.system() == "Darwin":
        _assert_ok(result, contains="label:")
        return
    if result.returncode != PREREQUISITE_EXIT_CODE or "macOS" not in result.stderr:
        raise SystemExit(
            "Expected 'factory service status' to refuse off macOS:\n"
            f"exit {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def _smoke_missing_git_prerequisite(executable: Path, repo: Path, workspace: Path) -> None:
    """With no ``git`` on ``PATH``, ``factory run`` must fail explicitly.

    Phase 15.2 acceptance criterion: an explicit prerequisite error and exit
    code 2, never a traceback from deep inside the workspace code.
    """
    empty_path_dir = workspace / "empty-path"
    empty_path_dir.mkdir(exist_ok=True)
    result = _run(
        str(executable),
        "run",
        "--repo",
        str(repo),
        "--title",
        "Missing git",
        "--description",
        "There is no git on PATH",
        "--data-dir",
        str(workspace / "prereq-data"),
        env_overrides={"PATH": str(empty_path_dir)},
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != PREREQUISITE_EXIT_CODE:
        raise SystemExit(
            f"Expected exit {PREREQUISITE_EXIT_CODE} without git on PATH, got "
            f"{result.returncode}:\n{combined}"
        )
    if "git" not in combined or "Traceback" in combined:
        raise SystemExit(f"Expected an explicit git prerequisite error, got:\n{combined}")


def _smoke_fake_run(executable: Path, repo: Path, data_dir: Path) -> None:
    result = _run(
        str(executable),
        "run",
        "--repo",
        str(repo),
        "--title",
        "Smoke task",
        "--description",
        "Offline fake-runtime smoke test",
        "--runtime",
        "fake",
        "--data-dir",
        str(data_dir),
    )
    _assert_ok(result, contains="state: PR_READY")
    _assert_ok(_run(str(executable), "runs", "--data-dir", str(data_dir)), contains="PR_READY")


def _assert_executable_mode(executable: Path) -> None:
    if not executable.is_file():
        raise SystemExit(f"Executable does not exist: {executable}")
    mode = executable.stat().st_mode
    if stat.S_IMODE(mode) & stat.S_IXUSR == 0 or not os.access(executable, os.X_OK):
        raise SystemExit(f"Executable is not marked executable: {executable}")


def _load_bundle_from_archive(
    archive: Path, workspace: Path, *, expect_architecture: str | None
) -> Path:
    archive_path = archive.resolve()
    if not archive_path.is_file():
        raise SystemExit(f"Archive does not exist: {archive}")

    extract_root = workspace / "archive"
    extract_root.mkdir()
    with tarfile.open(archive_path, "r:gz") as bundle_archive:
        members = bundle_archive.getmembers()
        top_levels: set[str] = set()
        for member in members:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise SystemExit(f"Unsafe archive member: {member.name}")
            if member_path.parts and not member_path.parts[0].startswith("._"):
                top_levels.add(member_path.parts[0])
        if len(top_levels) != 1:
            raise SystemExit(f"Expected exactly one top-level bundle directory in {archive}")
        bundle_archive.extractall(extract_root, filter="fully_trusted")

    bundle_dir = extract_root / top_levels.pop()
    instructions = bundle_dir / INSTALL_FILENAME
    if not instructions.is_file():
        raise SystemExit(f"Archive is missing {INSTALL_FILENAME}: {archive}")
    instruction_text = instructions.read_text(encoding="utf-8")
    required_lines = (
        "./factory --version",
        "./factory doctor",
        "./factory service install",
        "required: git",
        "optional: gh",
        "optional: copilot",
        "unsigned or ad-hoc signed",
    )
    if expect_architecture is not None and expect_architecture not in instruction_text:
        raise SystemExit(
            f"Archive instructions do not mention architecture {expect_architecture}: {archive}"
        )
    for line in required_lines:
        if line not in instruction_text:
            raise SystemExit(f"Archive instructions are missing {line!r}: {archive}")
    return bundle_dir


def main() -> int:
    args = parse_args()
    workspace_parent = _validate_workspace_parent(args.workspace_root)
    workspace_root = _create_workspace(workspace_parent)
    success = False
    try:
        if args.archive is not None:
            bundle_dir = _load_bundle_from_archive(
                args.archive,
                workspace_root,
                expect_architecture=args.expect_architecture,
            )
            executable = bundle_dir / "factory"
        else:
            executable = args.factory_executable.resolve()

        data_dir = workspace_root / "data"
        repo = workspace_root / "repo"
        _smoke_cli(executable, data_dir, expect_version=args.expect_version)
        _smoke_doctor(
            executable,
            data_dir,
            expect_architecture=args.expect_architecture,
        )
        _smoke_service_status_is_read_only(executable)
        repo.mkdir(parents=True, exist_ok=True)
        _seed_repo(repo)
        _smoke_missing_git_prerequisite(executable, repo, workspace_root)
        _smoke_fake_run(executable, repo, data_dir)
        _smoke_status(executable, data_dir)
        success = True
        return 0
    finally:
        if success:
            _cleanup_workspace(workspace_root)


if __name__ == "__main__":
    raise SystemExit(main())
