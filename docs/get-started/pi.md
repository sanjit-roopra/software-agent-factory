# Real pi runs

`--runtime pi` runs the agents through pi, a coding agent program. Pi is the
recommended runtime for real runs. This page is for operators who want to run
the factory with pi.

The maintainers made pi the recommended runtime on 2026-09-30, after the A/B
benchmark below. [ADR-031](../decisions.md#adr-031-pi-is-a-recommended-agent-runtime) records the decision.

!!! danger "This costs money"

    Every stage of a run is a separate pi call. Each repair attempt adds one call.
    The factory does not estimate spend before a run. `--runtime fake` stays the
    default on every command.

## Why pi

Pi keeps a saved session for each work item and role. The Implementer and the
Reviewer continue their own session in each repair or re-review round. They send
only what changed. The provider can then serve the earlier history from its cache.

The A/B benchmark ran 3 small tasks. Both runtimes used the same models for each
role. Both runtimes passed 3 of 3 tasks.

| Measure | Copilot | Pi | Pi as share of Copilot |
| --- | --- | --- | --- |
| Input and cache-write tokens | 276,576 | 111,637 | 40% |
| Cache-read tokens | 649,701 | 263,351 | 41% |
| Output tokens | 20,070 | 14,632 | 73% |
| Wall time | 415 s | 271 s | 65% |

Three tasks are a small sample. The benchmark driver is
`scripts/performance/runtime_ab.py`. It replays closed issues once for each
runtime and writes a side-by-side report. Its budget options are
`--max-copilot-premium-requests` and `--max-pi-usd`.

## Install pi

You need Node 22.19 or later. You need pi 0.99.1 or later.

```bash
npm install -g @earendil-works/pi-coding-agent
pi --version
```

Do not install the older `@mariozechner/pi-coding-agent` package. It stopped at
version 0.73.1 and the factory does not support it.

## Log in

Pi bills through GitHub Copilot by default. The `pi.provider` setting names the
provider. Its default is `github-copilot`.

1. Run `pi`.
2. Type `/login`.
3. Choose `github-copilot`.
4. Follow the prompts, then exit pi.

Pi saves the credential in `~/.pi/agent/auth.json`. If `PI_CODING_AGENT_DIR` is
set, pi uses that directory instead.

## Check the setup

```bash
factory doctor --runtime pi
```

Doctor checks four things in this order. It reports the first failure.

1. The `pi` executable is on `PATH`.
2. The pi version is 0.99.1 or later.
3. The Node version is 22.19 or later.
4. A credential exists for the configured provider.

Each failure message names the fix. Doctor makes no model call.

## Run it

```bash
uv run factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --config ~/my-factory.yaml \
  --runtime pi
```

The states, artifacts and gates are the same as with Copilot.

Every command that accepts `--runtime` accepts `pi`. These are `run`, `project`,
`start`, `doctor` and `service install`. See [CLI reference](../reference/cli.md).

## Models and cost units

Pi uses the models you configure for each role. Copilot and pi use the same
`models` settings. Read [Model selection](../reference/model-selection.md) for the
rules about model ids.

The two runtimes count cost in different units. The factory never adds them.

| Runtime | Cost unit |
| --- | --- |
| Copilot | Premium requests for each call. |
| Pi | A list-price estimate in USD, from the pi price list. It is not spend. |

The dashboard shows the estimate with the label "List-price estimate". Use GitHub
Copilot billing for real spend.

## How pi sessions work

The factory starts one `pi --mode rpc` process for each agent call. It keeps no
pi process between calls.

Only the Implementer and the Reviewer continue a session. Other roles run
without a saved session. The factory continues a session only when all of these
are true:

- The session file exists and the factory can read it.
- The last call on the session succeeded.
- The model, provider and reasoning level match the last call.
- The last call ended less than `pi.session_reuse_max_age_seconds` ago.

Otherwise the factory starts a new session and sends the full prompt. Session
files live under `<factory.data_dir>/pi-sessions/`. They hold prompts, repository
content and tool output. Only your user can read them.

See [Configuration](../reference/configuration.md#pi) for the `pi` settings.

## Limits

- `factory skill refresh --runtime pi` is not supported. Use `--runtime copilot`.
- Pi has no web fetch tool. The factory cannot generate a repository skill on pi.
- The optional polish step needs a generated repository skill. If none is stored
  for the current dependency state, the factory records a warning and skips polish.
- Run `factory skill refresh --runtime copilot` once. Later pi runs reuse the
  stored skill.
- A command filter blocks `git commit`, `git push`, `gh`, `curl` and `wget` for the Implementer, as Copilot does. The filter matches command patterns. It is not a security boundary, and it does not block other network access. See [The pi command filter](../reference/safety.md#the-pi-command-filter).
- The Copilot `context_tier` setting has no pi equivalent. Pi ignores it.

## Next

- [Real Copilot runs](copilot.md) covers the shared parts: repository setup, the
  stage list, repository skills and safe practice.
- [Configure a repository](../guides/configure-repository.md)
- [Safety and trust boundaries](../reference/safety.md)
