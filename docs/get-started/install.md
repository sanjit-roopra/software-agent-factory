# Install

The factory supports macOS. A source checkout needs Python 3.13+. Other
platforms are not tested or supported. `factory service` uses launchd and is
macOS-only.

## External tools

The factory bundles no toolchain. It looks for these on `PATH`:

| Tool | When it is needed |
| --- | --- |
| `git` | Always. |
| `gh` | Only when `pull_request.enabled`, `ci.enabled` or `scheduler.enabled` is true. |
| `copilot` | Only with `--runtime copilot`. |

`factory run` and `factory start` check this before doing any work. If a tool
they actually need is missing they print one line and exit with code `2`. You
never get a traceback.

`factory doctor` explains every requirement for your configuration.

## Install from source

```bash
git clone https://github.com/sanjit-roopra/software-agent-factory.git
cd software-agent-factory
uv sync --locked --group dev
uv run factory --version
```

Every command in these docs can be run as `uv run factory ...` from a source
checkout, or as `factory ...` from an installed wheel or an extracted archive.

## Install a released macOS archive

Each release attaches a native `arm64` archive, a native `x86_64` archive (there
is no `universal2` build), a wheel, an sdist, `SHA256SUMS` and
`build-info.json`.

```bash
# 1. download the archive for your architecture plus SHA256SUMS, then verify
shasum -a 256 -c SHA256SUMS --ignore-missing

# 2. extract and move it somewhere permanent
tar -xzf software-agent-factory-0.2.0-macos-arm64.tar.gz
mkdir -p ~/.local/opt
mv software-agent-factory ~/.local/opt/software-agent-factory

# 3. clear the Gatekeeper quarantine flag, then run it
xattr -dr com.apple.quarantine ~/.local/opt/software-agent-factory
~/.local/opt/software-agent-factory/factory --version
~/.local/opt/software-agent-factory/factory doctor
```

!!! warning "Archives are unsigned or ad-hoc signed"

    Apple Developer ID signing and notarization are deferred. macOS quarantines
    a downloaded archive and refuses to run it until you remove the quarantine
    attribute with the `xattr` command above. Every archive ships an
    `INSTALL.txt` repeating these steps.

Do not skip step 1. GitHub release immutability is a repository setting that is
not enabled yet, so a published release is not guaranteed by the platform to be
unchanged. See [Releases](../project/releases.md#releases-are-write-once-by-convention-not-by-guarantee).

Extracting an archive installs nothing, starts nothing and changes no system
state. In particular it does not install a background service. See
[Monitor and run continuously](../guides/operations.md#background-service-macos)
if you want one.

## Install the wheel

With Python 3.13 already available:

```bash
pip install software_agent_factory-0.2.0-py3-none-any.whl
factory --version
```

## Check the machine

```bash
factory doctor
```

`doctor` reports the platform, whether this is a frozen or source build,
`launchctl`, `git`, whether the configuration parses, the executables behind
your configured repository commands, and whether the data directory is
writable. It never makes a paid model call: the only `copilot` interaction is a
bounded `copilot --version` probe, and only with `--runtime copilot`.

```text
ok    platform    macOS arm64
ok    executable  source / interpreter at .../.venv/bin/python3
ok    launchctl   found at /bin/launchctl
ok    git         'git' found at /usr/bin/git (git version 2.50.1)
ok    config      valid config ((packaged default))
ok    data_dir    ~/.software-factory is writable

doctor: ok (0 error(s), 0 warning(s))
```

It exits nonzero if any check errored. Warnings alone do not fail it.

## Where state lives

Everything the factory persists goes under one data directory, `~/.software-factory`
by default:

```text
~/.software-factory/
├── runs/         one directory per run: run.json plus typed artifacts
├── workspaces/   one Git worktree per work item
├── locks/        short-lived exclusive locks
└── logs/         factory.log, rotated and size-bounded
```

Change it with `factory.data_dir` in configuration, or override it per command
with `--data-dir`. Nothing is written outside it, except the LaunchAgent plist
if you explicitly install the service.

## Next

- [First offline run](first-run.md)
