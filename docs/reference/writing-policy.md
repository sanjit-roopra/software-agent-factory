# Writing policy

The factory uses concise controlled English for all text that it authors.
The policy is advisory. A finding is logged. It never fails a run, never
causes a retry and never blocks delivery.

The prompts give the policy to agents. The controller checks only the text
that it publishes:

- Generated project issues
- Pull request titles and bodies
- Commit messages

The factory uses selected checks from
[SimpleEnglish](https://github.com/AminBlg/SimpleEnglish) v2.0.2. The included
source is pinned to revision
`61ee200efbd423050aab982eed94226229891ae0`.

## Rules

The factory checks these rules in publication text and reports each finding:

- Use 20 words or fewer for an instruction sentence.
- Use 25 words or fewer for a descriptive sentence.
- Keep each field within its total word limit.
- Remove filler terms.
- Do not use semicolons.
- Do not use em dashes.
- Do not use Latin abbreviations such as `e.g.` or `i.e.`.

The prompts also tell agents to use active voice, simple tenses and one idea
per sentence. The output contract of each role lists the word limit of each
field and a short list of filler words to avoid.

## Repository documentation

`README.md` and every Markdown file in `docs/` use the pinned SimpleEnglish skill.
`AGENTS.md` requires its strict ASD-STE100 guidance for each documentation change.

The documentation gate adds these mechanical rules:

- Do not use contractions.
- Use only `can`, `will`, or `must` for modal meaning.
- Use simple tenses.
- Avoid selected comma-plus-`-ing` clauses.

The gate classifies direct instructions and condition-first sentences as
procedural text. It treats the other sentences as descriptive text.

Run the gate locally:

```bash
uv run --no-sync python scripts/docs/check_simple_english.py
```

CI, the Pages workflow, and the release workflow run the same command.
The gate reports each finding with its file, line, and rule.

## Findings

The controller does not check agent results (ADR-036). The output contract of
each role still states the word limits.

- The controller logs findings for generated issue, pull request and commit
  text as warnings. It still publishes the text.
- Blank publication text is an error, because it is not a wording problem.
- Blank agent text fails model validation and takes the ordinary retry.

A retry happens only for a structural failure. Examples are invalid JSON, a
schema error and missing required data. The retry prompt names the failure.

The controller does not rewrite the artifact. This rule protects facts and
safety language.

## Exact text

The policy does not change:

- Code
- Identifiers
- File paths
- Commands
- URLs
- Quoted errors
- Raw command output
- Human-authored source text

Project child work items receive only their task description. The factory does
not copy the full project brief into every child prompt.

## Compliance limit

The checks follow ASD-STE100 Simplified Technical English principles. They do
not implement the full controlled dictionary or validate word meaning.

No tool can guarantee ASD-STE100 compliance.
The official standard is available from
[asd-ste100.org](https://www.asd-ste100.org/).
