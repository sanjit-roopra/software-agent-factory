# Releases

Delivery ends at a published release artifact. A tag builds a GitHub Release. It
does not install, restart, promote or self-update anything. This is continuous
*delivery*, not deployment.

## Process

```text
v* tag
  ↓
release quality gate (format, lint, types, tests, dependency audit)
  ↓
build and validate distributions and native macOS artifacts
  ↓
attest public-repository artifacts
  ↓
GitHub Release (the workflow refuses to replace an existing one)
  ↓
a human downloads and extracts it
```

Pushing a `v*` tag triggers the release workflow.

## Releases are protected by workflow and platform controls

The workflow checks whether the tag's release already exists and fails if it
does, so re-running a tag cannot replace published artifacts.

GitHub release immutability is enabled for new releases. Existing releases from
`v0.3.0` onward report `immutable=true`. Older historical releases still report
`immutable=false`, so the workflow guard remains useful defense in depth.

Always verify the checksum. Immutability prevents replacement. It does not prove
that the original artifact was the one you intended to download.

## What a release contains

Six files. For {{ factory_version }}:

```text
software-agent-factory-{{ factory_version }}-macos-arm64.tar.gz     PyInstaller onedir
software-agent-factory-{{ factory_version }}-macos-x86_64.tar.gz    PyInstaller onedir
software_agent_factory-{{ factory_version }}-py3-none-any.whl
software_agent_factory-{{ factory_version }}.tar.gz
SHA256SUMS
build-info.json
```

The two macOS archives are built natively on their own runners. There is no
`universal2` build. Download the one matching your CPU.

## Verify before you extract

```bash
shasum -a 256 -c SHA256SUMS --ignore-missing
```

`build-info.json` records the tag, commit, runner image, Python version,
PyInstaller version and architecture, so an archive is traceable to the build
that produced it.

This check confirms the downloaded bytes match the published checksum. Do it
every time. Do this check for immutable releases as well.

## Gatekeeper

Release artifacts are unsigned or ad-hoc signed. Apple Developer ID signing and
notarization are deferred, so macOS quarantines a downloaded archive and refuses
to run it until the attribute is cleared:

```bash
xattr -dr com.apple.quarantine ~/.local/opt/software-agent-factory
```

Every archive ships an `INSTALL.txt` repeating this. Release notes explain it
too.

## Extracting installs nothing

Unpacking an archive starts no service, opens no port, writes nothing outside
where you put it, and changes no system state. A launchd service exists only if
you ran `factory service install`.

## Versioning

Semantic versioning. The latest published release is
{{ factory_version }}. Pre-1.0, expect breaking changes to configuration keys
and CLI flags in minor releases. The changelog notes these changes.

The public documentation follows the current `main` branch. It reads the latest
published version from `project.version` in `pyproject.toml` so install commands
and artifact names stay tied to {{ factory_release_tag }}. Features listed under
`Unreleased` in the changelog can be documented before the next
package is published.

Check what you are running:

```bash
factory --version
```

## Changelog

[`CHANGELOG.md`](https://github.com/sanjit-roopra/software-agent-factory/blob/main/CHANGELOG.md)
is the record of what changed. Release notes are generated from the tag and
follow the same content.

## Links

- [All releases](https://github.com/sanjit-roopra/software-agent-factory/releases)
- [Install instructions](../get-started/install.md)
