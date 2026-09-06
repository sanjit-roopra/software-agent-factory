# Repository skills and human overlays

Repository skills are advisory guidance for the optional post-green polish
attempt. There are two artifacts, and they are trusted differently:

| Artifact | Written by | Bound to | Lifetime |
| --- | --- | --- | --- |
| Generated skill | the configured Researcher | the canonical repository identity and the profile's `dependency_fingerprint` | reused until the dependency fingerprint changes |
| Overlay | you | the repository only | survives dependency changes; the factory never edits it |

Both live outside your repository, under the factory's `factory.data_dir`.
Nothing is ever written into your checkout or its worktree, and the factory
never loads skills from the target repository.

## Generated skills are repository-wide and reused

Generation describes the repository, not the task. The Researcher receives the
normalized `RepositoryProfile` and the configured source lists
(`polish.official_documentation_origins` and `polish.practice_reference_urls`)
and nothing else — no changed filenames, no source code, no README content, no
task prose and no diff.

A normal run therefore does not research anything:

- if a generated skill exists for the current `dependency_fingerprint`, it is
  loaded and reused;
- guidance is generated only when the current fingerprint has no generated
  skill yet;
- an existing generated file is never overwritten;
- every load is validated again in full — schema, agreement with the current
  profile, and every cited source against the configured allowlists;
- when dependencies change, the new fingerprint selects a new generated file
  and earlier files stay on disk untouched;
- there is no TTL and no expiry. Guidance does not go stale on a timer.

Storage is repository-scoped and keyed, following the template:

```text
<data_dir>/repository-skills/v1/<repository-key>/...
```

Ask the factory for the real paths rather than guessing them:

```bash
uv run factory skill path --repo ~/projects/example
```

If a stored generated skill no longer revalidates — for example because it
cites a source you have since removed from `polish.official_documentation_origins`
— the run records a warning naming the file, leaves it on disk exactly as it
is, and skips polish. The warning points at `factory skill refresh`, which is
the only command that may replace generated guidance.

### Two first runs at the same time

Reuse means a repository normally researches once per set of dependencies, but
that is not a cross-process lock. Two truly concurrent first runs for the same
missing fingerprint may each make one bounded Researcher call. Publication is
atomic and no-clobber, so exactly one result is kept, the other run loads the
winner, and both revalidate the winner in full before using it.

The cost of that race is one extra research call. It cannot corrupt storage,
produce two competing files, change which guidance is used, or touch your
overlay.

### Moving or re-cloning a repository

The repository key is derived from the canonical local Git common directory —
the path on this machine — not from a remote URL. Moving a checkout to another
path, or cloning it again elsewhere, therefore selects a *new* repository key
with no generated skills and no overlay. Every linked worktree of the same
checkout keeps sharing one key.

Before you move a repository:

```bash
uv run factory skill path --repo ~/projects/example
```

Note the repository directory, move the checkout, then either move or copy that
directory to the path reported for the new location, or let the next run
regenerate guidance at the new key and write your overlay there yourself. The
factory does not follow a moved repository for you, it never creates an overlay
on your behalf, and it never deletes the guidance left behind at the old key.

## The overlay is yours

Human customization goes in a repository-level `repository-skill-overlay.yaml`
in that same repository-scoped directory — outside the target repository.

It carries guidance prose only:

```yaml
mode: extend

simplify:
  summary: House rules for simplification in this service.
  guidance:
    - Prefer a plain function over a class with one method.
    - Keep request handlers free of database access; use the repository layer.
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
  warning and ignores the overlay; valid generated guidance may still apply.

## Edit workflow

```bash
# 1. Find the paths for this repository.
uv run factory skill path --repo ~/projects/example

# 2. Create or edit repository-skill-overlay.yaml at the reported path.
$EDITOR <reported-overlay-path>

# 3. Check what the factory will accept, without changing anything.
uv run factory skill validate --repo ~/projects/example
```

`validate` is read-only: it reports what the current generated skill and
overlay are, and why either would be ignored. It never repairs, rewrites or
creates a file.

To refresh generated guidance deliberately — for example after upgrading
dependencies, without waiting for the next run:

```bash
uv run factory skill refresh --repo ~/projects/example --runtime copilot
```

`refresh` touches generated guidance only. It never creates, rewrites or
removes your overlay. `--runtime fake` is the default and makes no model call;
`--runtime copilot` is a real, paid call.

## What a run records

Before any agent sees guidance, the run stores immutable snapshots of what it
actually used, in the run directory:

| File | Contents |
| --- | --- |
| `repository-skill.json` | the effective guidance the agents received |
| `repository-skill-overlay.json` | your overlay exactly as it was read, when it was valid |
| `repository-skill-use.json` | provenance: repository key, dependency fingerprint, where the guidance came from, overlay mode and whether it applied, and content hashes |

Snapshots are taken once. Editing the overlay while a run is in flight affects
later runs only, never the run already in progress.

## Limits

Guidance is advisory prompt text. It cannot change tools, models, commands,
workflow states, retry budgets, permissions, quality gates, dependencies or
scope, and it cannot approve a change. The polish attempt applies simplify
first and polish second, in one bounded attempt after the first successful
deterministic verification, and full deterministic verification then runs again
before testing and review.

The dashboard never writes: it cannot generate, refresh, edit or delete a
skill or an overlay.

See also [Safety and trust boundaries](../reference/safety.md) and the `polish`
section of the [configuration reference](../reference/configuration.md#polish).
