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

## Releases are write-once by convention, not by guarantee

The workflow checks whether the tag's release already exists and fails if it
does, so re-running a tag cannot replace published artifacts.

That is a workflow rule, not a platform rule. It does not prevent someone
editing or deleting a release through the GitHub UI or API.

GitHub has its own release immutability feature. It is a repository setting, it
is off by default, and the current releases report `immutable=false`. Enable
immutable releases in the repository settings before relying on platform
enforcement.

Until then, verify what you downloaded rather than trusting that it cannot have
changed.

## What a release contains

Six files. For 0.3.0:

```text
software-agent-factory-0.3.0-macos-arm64.tar.gz     PyInstaller onedir
software-agent-factory-0.3.0-macos-x86_64.tar.gz    PyInstaller onedir
software_agent_factory-0.3.0-py3-none-any.whl
software_agent_factory-0.3.0.tar.gz
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

Because release immutability is not enforced by the platform yet, this check is
how you detect a swapped artifact. Do it every time.

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

Semantic versioning. The current release is 0.3.0. Pre-1.0, expect breaking
changes to configuration keys and CLI flags in minor releases; they are called
out in the changelog.

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
