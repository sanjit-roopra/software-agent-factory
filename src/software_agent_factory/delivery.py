"""Fetch an explicitly authorized delivery target without moving a checkout."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import FactoryConfig
from .github import (
    CommandRunner,
    GitCommandError,
    GitTimeoutError,
    MergeNotAllowedError,
    default_command_runner,
    parse_remote_repository,
)


@dataclass(frozen=True)
class DeliveryTarget:
    repository: str
    host: str
    commit_sha: str


def fetch_delivery_target(
    config: FactoryConfig,
    repo_path: Path,
    expected_repository: str,
    *,
    expected_host: str | None = None,
    runner: CommandRunner = default_command_runner,
) -> DeliveryTarget:
    """Pin the transport URL and fetch into a per-call ref, not shared FETCH_HEAD."""

    def git(*args: str) -> str:
        try:
            result = runner(["git", "-C", str(repo_path), *args])
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(args, exc.timeout) from None
        if result.returncode != 0:
            raise GitCommandError(args, result.returncode, result.stderr)
        return result.stdout.strip()

    base = config.pull_request.base_branch
    if not config.merge.enabled or not base:
        raise MergeNotAllowedError("fetching a delivery target requires explicit merge policy")
    git("check-ref-format", f"refs/heads/{base}")
    remote_url = git("remote", "get-url", config.pull_request.remote)
    try:
        repository = parse_remote_repository(remote_url)
    except ValueError as exc:
        raise MergeNotAllowedError(
            "delivery remote does not identify a supported repository"
        ) from exc
    if (
        repository.full_name.casefold() != expected_repository.casefold()
        or repository.full_name.casefold()
        not in {name.casefold() for name in config.merge.allowed_repositories}
        or repository.host.casefold()
        not in {host.casefold() for host in config.pull_request.allowed_hosts}
        or (expected_host is not None and repository.host.casefold() != expected_host.casefold())
    ):
        raise MergeNotAllowedError("delivery repository identity changed before target fetch")

    reference = f"refs/software-agent-factory/fetch/{uuid4().hex}"
    try:
        git(
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--",
            remote_url,
            f"refs/heads/{base}:{reference}",
        )
        commit = git("rev-parse", "--verify", f"{reference}^{{commit}}")
    finally:
        git("update-ref", "-d", reference)
    return DeliveryTarget(repository=expected_repository, host=repository.host, commit_sha=commit)
