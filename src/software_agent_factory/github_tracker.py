"""GitHub Issues-backed tracker provider for the local scheduler.

This module implements the Phase 13 concrete tracker adapter described in
``PLAN.md``: poll GitHub Issues via the GitHub CLI, normalize them into
``scheduler.TrackerItem`` instances, and revalidate individual issues
immediately before dispatch.

Like ``software_agent_factory.github``, all subprocesses are invoked through
argument lists via an injectable runner so tests can fully fake ``gh`` without
real network access.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .github import CommandRunner, GitHubCommandError, default_command_runner
from .scheduler import TrackerItem, TrackerProvider

_ISSUE_JSON_FIELDS = "id,number,title,body,state,labels,createdAt,url"
_DEFAULT_CANDIDATE_LIMIT = 1000
_MAX_CANDIDATE_LIMIT = 1000
_NUMERIC_PRIORITY_LABELS: dict[str, str] = {
    "p0": "P0",
    "priority:p0": "P0",
    "p1": "P1",
    "priority:p1": "P1",
    "p2": "P2",
    "priority:p2": "P2",
    "p3": "P3",
    "priority:p3": "P3",
}
_NAMED_PRIORITY_LABELS: dict[str, str] = {
    "priority:critical": "P0",
    "priority:urgent": "P0",
    "priority:high": "P1",
    "priority:medium": "P2",
    "priority:low": "P3",
}
_PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


@dataclass
class GitHubIssueProvider(TrackerProvider):
    """Concrete ``TrackerProvider`` backed by ``gh issue`` commands.

    ``token`` is never passed on the command line. When supplied, it is only
    provided to ``gh`` through the child environment as ``GH_TOKEN`` and is
    excluded from ``repr()``.
    """

    repository: str
    required_label: str
    local_repository_path: Path | str
    gh_path: str = "gh"
    token: str | None = field(default=None, repr=False)
    runner: CommandRunner = default_command_runner
    candidate_limit: int = _DEFAULT_CANDIDATE_LIMIT

    def __post_init__(self) -> None:
        repo_parts = self.repository.split("/")
        if self.repository.count("/") != 1 or any(not part.strip() for part in repo_parts):
            raise ValueError("repository must use the 'owner/name' format")
        if not self.required_label.strip():
            raise ValueError("required_label must not be empty")
        if not self.gh_path.strip():
            raise ValueError("gh_path must not be empty")
        if isinstance(self.candidate_limit, bool) or not isinstance(self.candidate_limit, int):
            raise ValueError(
                f"candidate_limit must be an integer between 1 and {_MAX_CANDIDATE_LIMIT}"
            )
        if self.candidate_limit < 1 or self.candidate_limit > _MAX_CANDIDATE_LIMIT:
            raise ValueError(f"candidate_limit must be between 1 and {_MAX_CANDIDATE_LIMIT}")

        self.local_repository_path = Path(self.local_repository_path)

    def fetch_candidates(self) -> Sequence[TrackerItem]:
        args = [
            "issue",
            "list",
            "--repo",
            self.repository,
            "--state",
            "open",
            "--label",
            self.required_label,
            "--limit",
            str(self.candidate_limit),
            "--json",
            _ISSUE_JSON_FIELDS,
        ]
        result = self._run(args)
        raw_items = self._parse_json(result, args, expect_type=list)
        candidates: list[TrackerItem] = []
        for raw_item in raw_items:
            item = self._normalize_issue(raw_item, args=args)
            if item is not None:
                candidates.append(item)
        return tuple(candidates)

    def fetch_by_ids(self, opaque_ids: Sequence[str]) -> Sequence[TrackerItem]:
        items: list[TrackerItem] = []
        for opaque_id in opaque_ids:
            number = self._parse_opaque_id(opaque_id)
            args = [
                "issue",
                "view",
                str(number),
                "--repo",
                self.repository,
                "--json",
                _ISSUE_JSON_FIELDS,
            ]
            result = self._run(args, check=False)
            if result.returncode != 0:
                if self._is_missing_issue_error(result.stderr):
                    continue
                raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)

            raw_item = self._parse_json(result, args, expect_type=dict)
            item = self._normalize_issue(raw_item, args=args)
            if item is not None:
                items.append(item)
        return tuple(items)

    def _env(self) -> Mapping[str, str] | None:
        return {"GH_TOKEN": self.token} if self.token else None

    def _run(
        self, args: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            [self.gh_path, *args],
            cwd=self.local_repository_path,
            env=self._env(),
        )
        if check and result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        return result

    def _parse_json(
        self,
        result: subprocess.CompletedProcess[str],
        args: Sequence[str],
        *,
        expect_type: type[list[Any]] | type[dict[str, Any]],
    ) -> list[Any] | dict[str, Any]:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            command = " ".join((self.gh_path, *args))
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                f"invalid JSON from {command}: {exc}",
            ) from exc
        if not isinstance(payload, expect_type):
            expected_name = "array" if expect_type is list else "object"
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                f"expected JSON {expected_name} from gh output, got {type(payload).__name__}",
            )
        return payload

    def _normalize_issue(
        self,
        payload: object,
        *,
        args: Sequence[str],
    ) -> TrackerItem | None:
        if not isinstance(payload, dict):
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                f"invalid issue payload: expected object, got {type(payload).__name__}",
            )

        number = self._require_issue_number(payload, args=args)
        labels = self._extract_label_names(payload, args=args)
        state = str(payload.get("state") or "").strip().upper()
        if not state:
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                "invalid issue payload: missing state",
            )

        title = str(payload.get("title") or "").strip()
        if not title:
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                f"invalid issue payload for {self.repository}#{number}: missing title",
            )

        url = str(payload.get("url") or "").strip()
        is_pull_request = self._looks_like_pull_request(payload, url=url)
        if is_pull_request:
            return None

        created_at = self._parse_created_at(payload, args=args, number=number)
        opaque_id = f"{self.repository}#{number}"
        dispatchable = (
            state == "OPEN"
            and self._has_required_label(labels)
            and not is_pull_request
        )

        return TrackerItem(
            opaque_id=opaque_id,
            identifier=opaque_id,
            title=title,
            description=str(payload.get("body") or ""),
            state=state,
            labels=tuple(labels),
            priority=self._priority_from_labels(labels),
            created_at=created_at,
            blockers=(),
            dispatchable=dispatchable,
            repository_path=str(self.local_repository_path),
        )

    def _require_issue_number(self, payload: Mapping[str, object], *, args: Sequence[str]) -> int:
        raw_number = payload.get("number")
        if not isinstance(raw_number, int) or raw_number < 1:
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                f"invalid issue payload: expected a positive integer number, got {raw_number!r}",
            )
        return raw_number

    def _parse_created_at(
        self,
        payload: Mapping[str, object],
        *,
        args: Sequence[str],
        number: int,
    ) -> datetime:
        raw_created_at = str(payload.get("createdAt") or "").strip()
        if not raw_created_at:
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                f"invalid issue payload for {self.repository}#{number}: missing createdAt",
            )
        try:
            created_at = datetime.fromisoformat(raw_created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                f"invalid issue payload for {self.repository}#{number}: invalid createdAt "
                f"{raw_created_at!r}",
            ) from exc
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                "invalid issue payload for "
                f"{self.repository}#{number}: createdAt must be timezone-aware",
            )
        return created_at.astimezone(timezone.utc)

    def _extract_label_names(
        self, payload: Mapping[str, object], *, args: Sequence[str]
    ) -> tuple[str, ...]:
        raw_labels = payload.get("labels") or []
        if not isinstance(raw_labels, list):
            raise GitHubCommandError(
                (self.gh_path, *args),
                0,
                "invalid issue payload: expected labels to be a list, got "
                f"{type(raw_labels).__name__}",
            )

        names: list[str] = []
        for label in raw_labels:
            if not isinstance(label, dict):
                raise GitHubCommandError(
                    (self.gh_path, *args),
                    0,
                    f"invalid issue payload: expected label object, got {type(label).__name__}",
                )
            name = str(label.get("name") or "").strip()
            if name:
                names.append(name)
        return tuple(names)

    def _priority_from_labels(self, labels: Sequence[str]) -> str | None:
        matches: list[str] = []
        for label in labels:
            normalized = label.strip().lower()
            priority = _NUMERIC_PRIORITY_LABELS.get(normalized) or _NAMED_PRIORITY_LABELS.get(
                normalized
            )
            if priority is not None:
                matches.append(priority)
        if not matches:
            return None
        return min(matches, key=lambda candidate: _PRIORITY_RANK[candidate])

    def _has_required_label(self, labels: Sequence[str]) -> bool:
        required = self.required_label.strip().casefold()
        return any(label.casefold() == required for label in labels)

    def _looks_like_pull_request(self, payload: Mapping[str, object], *, url: str) -> bool:
        if "/pull/" in url:
            return True
        return bool(payload.get("pullRequest") or payload.get("pull_request"))

    def _parse_opaque_id(self, opaque_id: str) -> int:
        prefix = f"{self.repository}#"
        if not opaque_id.startswith(prefix):
            if "#" in opaque_id:
                raise ValueError(
                    f"opaque id {opaque_id!r} does not belong to repository {self.repository!r}"
                )
            raise ValueError(
                f"opaque id {opaque_id!r} must use the format {self.repository!r} + '#<number>'"
            )

        raw_number = opaque_id[len(prefix) :]
        try:
            number = int(raw_number)
        except ValueError as exc:
            raise ValueError(
                f"opaque id {opaque_id!r} must end in a positive issue number"
            ) from exc
        if number < 1:
            raise ValueError(f"opaque id {opaque_id!r} must end in a positive issue number")
        return number

    def _is_missing_issue_error(self, stderr: str) -> bool:
        lowered = stderr.lower()
        return any(
            marker in lowered
            for marker in (
                "could not resolve to an issue",
                "could not resolve to issue",
                "no issue found",
                "not found",
            )
        )
