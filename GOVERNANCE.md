# Governance

Software Agent Factory is currently maintained by one project maintainer.

## How decisions are made

Small fixes use normal pull request review.

Changes to workflow authority, safety rules, persistence, model routing, release
policy, or supported platforms need:

1. a public issue that explains the problem
2. the smallest practical design
3. an architecture decision record when the baseline changes
4. passing deterministic checks

The maintainer makes the final decision. Decisions should be based on project
goals, safety, maintenance cost, and evidence from tests.

## Maintainer responsibilities

Maintainers:

- review and merge pull requests
- publish releases
- handle security reports
- enforce the code of conduct
- keep CI, dependencies, and documentation current

## Becoming a maintainer

Regular contributors may be invited after they show sound judgement, reliable
review work, and a clear understanding of the safety model.

## Removing a maintainer

A maintainer may step down at any time. Access may also be removed for a
security risk, repeated policy violations, or long-term inactivity.

## Project continuity

The project should add a second maintainer before it depends on independent
maintainer approval for every change. Until then, the single-maintainer status
is explicit rather than hidden.
