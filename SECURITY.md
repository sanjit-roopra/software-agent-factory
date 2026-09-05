# Security policy

## Supported versions

This project is in alpha. Security fixes are made on the latest release only.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Older releases | No |

## Report a vulnerability

Do not open a public issue.

Use
[GitHub private vulnerability reporting](https://github.com/sanjit-roopra/software-agent-factory/security/advisories/new).
Include:

- the affected version or commit
- the impact
- steps to reproduce
- any suggested fix

You should receive an acknowledgement within seven days. Fix timing depends on
severity and complexity. We will coordinate disclosure with you and credit you
unless you prefer to remain anonymous.

If private vulnerability reporting is not available, wait until it is enabled
rather than posting sensitive details in public.

## Security model

The factory runs commands and edits repositories on the local machine. Review
configuration before enabling GitHub access or the real Copilot runtime.

The default configuration makes no network calls and uses the fake runtime.
See the [security model](https://sanjit-roopra.github.io/software-agent-factory/reference/safety/)
for the main safeguards and trust boundaries.
