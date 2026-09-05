# First offline run

This takes about five minutes and costs nothing. The default agent runtime is
`fake`: a deterministic test double that returns valid artifacts without calling
a model. The rest of the system is real — real Git worktrees, real state
machine, real persisted artifacts.

## 1. Pick a target repository

Any Git repository with at least one commit works. To keep the walkthrough
self-contained:

```bash
mkdir -p ~/projects/example && cd ~/projects/example
git init
echo "# example" > README.md
git add -A
git commit -m "init"
```

## 2. Run one work item

```bash
cd /path/to/software-agent-factory
uv run factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --config config/factory.example.yaml \
  --data-dir ./.factory-demo
```

Output:

```text
run id: run-9bb36bbbdf114f53bd9599a103122976
state: PR_READY
workspace: ./.factory-demo/workspaces/WI-c769695fc242
changed files: FACTORY_NOTES.md
```

The run moved through:

```text
CREATED → prepare worktree → profile repository → TRIAGING
        → REFINING → [RESEARCHING] → PLANNING
        → IMPLEMENTING → VERIFYING
        → [RESEARCHING (repository skill)] → IMPLEMENTING (POLISH) → VERIFYING
        → REVIEWING → PR_READY
```

The profile and polish reuse existing states rather than adding
`PROFILING`/`POLISHING`. The example configuration enables one bounded polish
pass, informed by version-aware guidance for the versions this repository
actually declares. That guidance is stored under the data directory, keyed by
the repository and its dependency fingerprint, and reused: the web-only
research call happens only on the first run for a given set of dependencies.
Polish may make no edits, but it still records an attempt and reruns
deterministic verification. If the research or its validation fails, the
factory records a warning on the profile and skips polish; the already-verified
run continues to review. A legacy configuration that omits `polish` defaults to
disabled.

`PR_READY` is the completed endpoint when pull requests are disabled, which they
are in `config/factory.example.yaml`. A nonzero exit code means the run did not
finish successfully; `NEEDS_HUMAN` and `FAILED` are the other endings.

`--data-dir ./.factory-demo` keeps this experiment out of `~/.software-factory`.
Drop it once you are past the demo.

## 3. Look at what happened

```bash
uv run factory runs --data-dir ./.factory-demo
```

```text
run-9bb36bbbdf114f53bd9599a103122976	PR_READY	WI-c769695fc242	2026-09-05T09:06:47+00:00
```

```bash
uv run factory show run-9bb36bbbdf114f53bd9599a103122976 --data-dir ./.factory-demo
```

`show` prints the persisted run as JSON: state, attempt history, budgets,
timestamps and artifact references.

## 4. Look at the files

Nothing is hidden in a database. Everything is JSON on disk.

```text
.factory-demo/runs/run-9bb.../
├── run.json              state machine, attempts, budgets, timestamps
├── work-item.json
├── repository-profile.json
│                           technologies, tools, markers, fingerprints,
│                           version files, dependency declarations, warnings
├── triage.json           complexity, risk, whether research is needed
├── specification.json    acceptance criteria
├── execution-plan.json
├── change-set.json       what the implementer claims it did
├── patch.diff            what the controller actually observed
├── verification.json     install / verify / build results
├── repository-skill.json  immutable snapshot of the effective simplify and
│                           polish guidance this run used (generated guidance
│                           plus any valid human overlay)
├── repository-skill-overlay.json
│                           the overlay exactly as it was read, when valid
├── repository-skill-use.json
│                           provenance: repository key, dependency fingerprint,
│                           where the guidance came from, content hashes
├── test-report.json      independent tester
├── review.json           independent reviewer
└── attempts/
    ├── 01/               initial implementation snapshot
    └── 02/               bounded polish snapshot
```

The distinction between `change-set.json` and `patch.diff` matters: the tester
and reviewer are given the controller-derived diff, not the implementer's own
account of it.

The Git worktree stays on disk too, under `workspaces/`. Workspaces are
preserved by default so you can inspect or reuse the change.

The reusable guidance itself lives outside the run, under
`<data_dir>/repository-skills/v1/<repository-key>/...`, together with the
optional `repository-skill-overlay.yaml` you may write by hand. Run
`uv run factory skill path --repo ~/projects/example` to see the exact
locations, and read
[Repository skills and overlays](../guides/repository-skills.md).

## 5. Check the derived metrics

```bash
uv run factory status --data-dir ./.factory-demo
```

```text
runs: 1 total, 1 scanned
states: 1 succeeded, 0 escalated, 0 failed, 0 active (0 stale)
attempts: 2 total, 2 implementation, 0 CI repair, 0 scope replan(s)
first-pass success: 100% (1/1)

health:
  stale runs: 0
  stale locks: 0 (of 0 checked)
  orphaned workspaces: 0 (of 1 checked)

status: complete
```

`status` is read-only. It recomputes everything from the persisted artifacts on
each call and will not even create the data directory.

The first-pass metric ignores the planned polish attempt. It measures whether
the initial implementation needed repair for an implementer, verification,
scope or review failure.

## What this run did and did not do

Did:

- created a Git worktree for the work item
- ran the pipeline through the workflow controller
- profiled repository capabilities without shell, network or imports
- persisted typed artifacts and a per-attempt snapshot
- wrote a structured JSON log to `<data-dir>/logs/factory.log`

Did not:

- call a model, or spend anything
- make any network request
- commit, push, or open a pull request
- start a server or install a service

## Next

- [Real Copilot runs](copilot.md) to use actual models.
- [Configure a repository](../guides/configure-repository.md) to make
  verification run your project's real lint, tests and build. Until you do, the
  `install`, `verify` and `build` command lists are empty and verification has
  nothing deterministic to check.
