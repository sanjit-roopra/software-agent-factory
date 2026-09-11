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

`factory run` and `factory start` check required tools before starting work.
If a required tool is missing, they print one message line and exit with code `2`.
The CLI never displays a Python traceback.

`factory doctor` explains every requirement for your configuration.

## Install from source

```bash
git clone https://github.com/sanjit-roopra/software-agent-factory.git
cd software-agent-factory
uv sync --locked --group dev
uv run factory --version
```

You can run every command in this documentation as `uv run factory ...` from a source checkout.
You can also run `factory ...` from an installed wheel or an extracted archive.

## Install a released macOS archive

Each release attaches a native `arm64` archive, a native `x86_64` archive (there
is no `universal2` build), a wheel, an sdist, `SHA256SUMS` and
`build-info.json`.

```bash
# 1. download the archive for your architecture and SHA256SUMS, then verify
shasum -a 256 -c SHA256SUMS --ignore-missing

# 2. extract and move it somewhere permanent
tar -xzf software-agent-factory-{{ factory_version }}-macos-arm64.tar.gz
mkdir -p ~/.local/opt
mv software-agent-factory ~/.local/opt/software-agent-factory

# 3. clear the Gatekeeper quarantine flag, then run it
xattr -dr com.apple.quarantine ~/.local/opt/software-agent-factory
~/.local/opt/software-agent-factory/factory --version
~/.local/opt/software-agent-factory/factory doctor
```

!!! warning "Archives are unsigned or ad-hoc signed"

    Remove the quarantine attribute with `xattr` before you run the binary.
    macOS quarantines downloaded archives and refuses to run unsigned binaries.
    Apple Developer ID signing and notarization are deferred.
    Every archive ships an `INSTALL.txt` file repeating these steps.

Do not skip step 1.
GitHub immutability protects current releases from replacement.
The checksum confirms that the downloaded bytes match the published artifact.
Read [Releases](../project/releases.md#releases-are-protected-by-workflow-and-platform-controls).

Extracting an archive installs nothing, starts nothing, and changes no system state.
Specifically, it does not install a background service.
If you want a service, read [Monitor and run continuously](../guides/operations.md#background-service-macos).

## Install the wheel

If Python 3.13 is available:

```bash
pip install software_agent_factory-{{ factory_version }}-py3-none-any.whl
factory --version
```

## Check the machine

```bash
factory doctor
```

`doctor` reports the platform and build type (source or frozen).
It checks `launchctl`, `git`, configuration syntax, configured command paths,
and write access to the data directory.
It never makes a paid model call.
The only `copilot` action is a bounded `copilot --version` probe with `--runtime copilot`.

```text
ok    platform    macOS arm64
ok    executable  source / interpreter at .../.venv/bin/python3
ok    launchctl   found at /bin/launchctl
ok    git         'git' found at /usr/bin/git (git version 2.50.1)
ok    config      valid config ((packaged default))
ok    data_dir    ~/.software-factory is writable

doctor: ok (0 error(s), 0 warning(s))
```

It exits with a nonzero code if a check fails. Warnings alone do not cause failure.

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

Change this path with `factory.data_dir` in configuration.
You can also override the path per command with `--data-dir`.
The factory writes nothing outside this directory, unless you install the LaunchAgent service.

## Next

- [First offline run](first-run.md)
