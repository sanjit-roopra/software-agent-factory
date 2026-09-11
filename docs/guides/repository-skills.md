# Repository skills and human overlays

Repository skills are advisory guidance for the optional post-green polish
attempt. There are two artifacts, and they are trusted differently:

| Artifact | Written by | Bound to | Lifetime |
| --- | --- | --- | --- |
| Generated skill | the configured Researcher | the canonical repository identity and the profile's `dependency_fingerprint` | reused until the dependency fingerprint changes |
| Overlay | you | the repository only | survives dependency changes, and the factory never edits it |

Both live outside your repository, under the factory's `factory.data_dir`.
Nothing is ever written into your checkout or its worktree, and the factory
never loads skills from the target repository.

## Generated skills are repository-wide and reused

Generation describes the repository, not the task. The Researcher receives the
normalized `RepositoryProfile` and the configured source lists
(`polish.official_documentation_origins` and `polish.practice_reference_urls`).
It receives no changed filenames, source code, README content, task prose, or diff.

A standard run does not do research:

- If a generated skill exists for the current `dependency_fingerprint`, the factory
  loads and reuses it.
- The factory generates guidance only when the current fingerprint has no
  generated skill yet.
- The factory never overwrites an existing generated file.
- Every load is validated again in full. Validation checks schema, agreement with
  the profile, and cited sources against allowlists.
- When dependencies change, the new fingerprint selects a new generated file.
  Earlier files stay on disk untouched.
- There is no TTL and no expiration date. Guidance does not expire on a timer.

Storage is repository-scoped and keyed. It uses this path template:

```text
<data_dir>/repository-skills/v1/<repository-key>/...
```

Ask the factory for the real paths rather than guessing them:

```bash
uv run factory skill path --repo ~/projects/example
```

A stored generated skill can fail revalidation if you remove a cited source
from `polish.official_documentation_origins`. In that case, the run records a
warning naming the file. It leaves the file on disk as written and skips polish.
The warning points to `factory skill refresh`. That command is the only
command that can replace generated guidance.

### Two first runs at the same time

Reuse means that a repository researches once per set of dependencies.
That behavior is not a cross-process lock. Two concurrent first runs for the same
missing fingerprint can each run one bounded generation sequence.
The sequence starts with one Researcher call and allows one retry after a failure.
Invalid output or provenance includes the exact bounded rejection reason.
An infrastructure failure receives one ordinary retry. Publication is atomic
and does not overwrite existing files. Exactly one result is kept. The other
run loads the winner, and both runs revalidate the winner in full before using it.

The cost of that race is at most one extra sequence (two calls). It cannot
corrupt storage, produce two competing files, change which guidance is used, or
touch your overlay.

### Moving or re-cloning a repository

The factory derives the repository key from the canonical local Git common
directory. This directory is the path on this machine.
The factory does not use a remote URL for this key.
A moved or new clone selects a new repository key.
The new key has no generated skills or overlay.
Every linked worktree of the same checkout shares one key.

Before you move a repository:

```bash
uv run factory skill path --repo ~/projects/example
```

Record the repository directory, then move the checkout.
Copy the directory to the path reported for the new location.
Alternatively, let the next run regenerate guidance and write your overlay there.
The factory does not follow moved repositories.
It never creates an overlay on your behalf.
It never deletes guidance at the old key.

## The overlay is yours

Human customization goes in a repository-level `repository-skill-overlay.yaml`
file in that repository-scoped directory, outside the target repository.

It carries guidance prose only:

```yaml
mode: extend

simplify:
  summary: House rules for simplification in this service.
  guidance:
    - Prefer a plain function over a class with one method.
    - Keep request handlers free of database access. Use the repository layer.
  avoid:
    - Do not introduce new abstraction layers to remove two lines of duplication.
  validation:
    - The public HTTP contract in docs/api.md must not change.

polish:
  summary: House rules for polish in this service.
  guidance:
    - Name tests after the behaviour they pin, not the function they call.
  avoid:
    - Do not add new runtime dependencies.
```

Rules that make the overlay safe to hand-edit:

- `mode` is `extend` or `replace`. `extend` adds your guidance to the generated
  guidance. `replace` makes your blocks the guidance for the sections you
  provide.
- `simplify` and `polish` are optional and have the same shape as generated
  guidance: `summary`, `guidance`, and optional `avoid` and `validation`.
- There are no targets, sources, versions or fingerprints. Version-specific
  claims stay the Researcher's job, grounded in official documentation.
- Because the overlay carries no fingerprint, it survives dependency changes
  and keeps applying to later runs.
- The factory never creates, rewrites, normalizes, refreshes or deletes it. It
  is your file.
- An invalid overlay is preserved exactly as you wrote it. The run records a
  warning and ignores the overlay. Valid generated guidance can still apply.

## Edit workflow

```bash
# 1. Find the paths for this repository.
uv run factory skill path --repo ~/projects/example

# 2. Create or edit repository-skill-overlay.yaml at the reported path.
$EDITOR <reported-overlay-path>

# 3. Check what the factory will accept, without changing anything.
uv run factory skill validate --repo ~/projects/example
```

`validate` is read-only. It reports current generated skills and overlays,
and explains why either file is ignored. It never repairs, rewrites, or
creates a file.

To refresh generated guidance deliberately (for example after updating
dependencies, without waiting for the next run):

```bash
uv run factory skill refresh --repo ~/projects/example --runtime copilot
```

`refresh` updates generated guidance only. It never creates, rewrites, or
removes your overlay. `--runtime fake` is the default and makes no model call.
`--runtime copilot` is a paid call.

## What a run records

Before agents receive guidance, the run stores immutable snapshots in
the run directory:

| File | Contents |
| --- | --- |
| `repository-skill.json` | the effective guidance the agents received |
| `repository-skill-overlay.json` | your overlay exactly as it was read, when it was valid |
| `repository-skill-use.json` | provenance: repository key, dependency fingerprint, where the guidance came from, overlay mode and whether it applied, and content hashes |

Snapshots are taken once. Editing the overlay while a run is in flight affects
later runs only, never the run already in progress.

## Limits

Guidance is advisory prompt text.
It cannot change tools, models, commands, workflow states, retry budgets, permissions,
quality gates, dependencies, or scope.
It cannot approve a change.
The polish attempt applies simplify first and polish second.
This bounded attempt runs after the first successful verification.
Full deterministic verification runs again before testing and review.

The dashboard never writes: it cannot generate, refresh, edit or delete a
skill or an overlay.

See also [Safety and trust boundaries](../reference/safety.md) and the `polish`
section of the [configuration reference](../reference/configuration.md#polish).
