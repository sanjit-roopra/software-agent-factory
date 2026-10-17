# Model selection, cost and benchmark guide

This guide is a dated decision aid for the models available through GitHub
Copilot. It combines:

- authoritative GitHub pricing, plan and capability documentation,
- exact-model software-engineering benchmarks where they exist,
- reasoning, instruction-following, research, security and long-context
  evidence,
- the constraints of this factory's current deterministic router.

**Snapshot date:** 2026-10-17.

Model catalogs, prices and benchmark leaderboards change. Recheck the linked
sources before making a long-lived routing decision.

## Executive recommendation

There is no single best model across every factory role.

| Need | Strong candidates | Why |
| --- | --- | --- |
| Lowest-cost mechanical work | `gpt-6-luna`, `mai-code-1.1-flash` | Luna costs about $0.0075 for the illustrative small call below, half the MAI price. MAI is coding-specialized. |
| Cost-effective implementation | `gpt-6-luna` at `high` | Lowest price with a 1M window. No exact DeepSWE score was found yet, so validate it locally. |
| Hard implementation | `gpt-6.1-sol`, `gpt-6-astra` | Sol costs a fifth of Astra. Astra leads the current DeepSWE comparison. |
| Hard planning and novel reasoning | `gpt-6-astra`, `claude-opus-5` | Strongest current reasoning and agentic evidence, but expensive. |
| Web research and synthesis | `claude-opus-5` | Best available exact-model BrowseComp and synthesis evidence, with major benchmark setup caveats. |
| Cost-effective testing experiment | `gpt-6-luna` | Lowest price. No exact Terminal-Bench 4 score was found yet. Validate locally before adopting. |
| High-value final review | `claude-sonnet-5.5` | A different family from the GPT workers, at the same price as `gpt-6.1-sol`. |
| Security-focused testing | `claude-opus-5`, then `gpt-6-astra` | Opus has stronger disclosed prompt-injection resistance. Astra is an optional offensive second pass for high-risk changes. |
| Untrusted-repository review | `claude-opus-5` has the strongest published prompt-injection result | This conflicts with the factory's reviewer-family rule when any Claude worker is configured. Use it as a separate specialist or change the worker families. |

The strongest public autonomous-coding results are currently **GPT-6 Astra and
Claude Opus 5**. Their best DeepSWE v1.1 scores overlap statistically, so the
ranking does not establish one winner. No exact benchmark scores were found
yet for GPT-6 Luna, GPT-6.1 Sol or the Claude 5.5 models. A local bakeoff is
more useful than selecting the top headline score.

The packaged `default` profile is:

```yaml
models:
  triage:     { model: "gpt-6-luna",        reasoning: "low",    context_tier: "default" }
  planner:    { model: "gpt-6.1-sol",       reasoning: "high",   context_tier: "default" }
  workers:
    L0:       { model: "gpt-6-luna",        reasoning: "medium", context_tier: "default" }
    L1:       { model: "gpt-6-luna",        reasoning: "high",   context_tier: "default" }
    L2:       { model: "gpt-6.1-sol",       reasoning: "high",   context_tier: "default" }
    L3:       { model: "gpt-6.1-sol",       reasoning: "xhigh",  context_tier: "default" }
  tester:     { model: "gpt-6-luna",        reasoning: "medium", context_tier: "default" }
  reviewer:   { model: "claude-sonnet-5.5", reasoning: "high",   context_tier: "default" }
```

It uses Luna for cheap work and Sol for planning and complex implementation.
Every role runs on `--runtime`. The reviewer is a Claude model, so its family
differs from every GPT worker.

The packaged `economy` profile uses `gpt-6-luna` for every role except the
reviewer, which stays `claude-sonnet-5.5`.

The packaged `mixed` profile runs the planner (`opus`) and the reviewer
(`sonnet`) on Claude Code at `high`. It runs triage, the workers and the tester
on pi with the same Luna and Sol choices as the default. Each role names its
runtime, so `--runtime` does not change this profile.

The packaged `claude` profile uses Claude Code aliases for every role.

Select a profile with `--model-profile <name>` on `run`, `project`, `start`,
`doctor`, `skill refresh` or `service install`. Profiles are complete routing
tables, not partial overlays.

Adaptive execution routing can also select a profile per route option with `routing.options[].model_profile`.
When an option specifies a profile name from `model_profiles`, implementation attempts use the workers from that profile.
If omitted or set to `default`, implementation attempts use the default models.

Check that the account lists `gpt-6.1-sol` and `claude-sonnet-5.5` before a
real run with the defaults.

## How Copilot billing works

GitHub's normal 2026 billing model is usage-based **AI Credits**:

- prices are stated in USD per one million tokens,
- one AI Credit is $0.01,
- input, cached input, output and sometimes cache writes are charged
  separately,
- reasoning tokens are generally billed as output,
- larger context and higher reasoning can materially increase cost,
- explicit model selection does not receive the 10% Auto-selection discount.

Premium-request multipliers now apply only to eligible legacy annual Pro and
Pro+ subscriptions that remained on request-based billing. They are not the
right basis for normal current cost comparisons.

The Copilot runtime requests the CLI's usage-output file and persists only
values the CLI reports: input/output/reasoning/cache tokens, nano-AIU and
premium-request cost where available. These are shown by `factory status` and
the local dashboard. Persisted telemetry remains in the reported units. The
dashboard also derives an AI usage value in USD from nano-AIU when available.
That display is not necessarily the invoice charge. The figures below remain
planning inputs rather than reconstructed billing.

Official sources:

- [GitHub model pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
- [Individual usage-based billing](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-individuals)
- [Organization and enterprise billing](https://docs.github.com/en/copilot/concepts/billing/organizations-and-enterprises/usage-based-billing)
- [Legacy annual-plan multipliers](https://docs.github.com/en/copilot/reference/copilot-billing/request-based-billing-legacy/model-multipliers-for-annual-plans)

### Monthly included AI Credits

| Plan | Monthly allowance |
| --- | ---: |
| Copilot Pro | 1,500 |
| Copilot Pro+ | 7,000 |
| Copilot Max | 20,000 |
| Copilot Business | 1,900 per seat, pooled |
| Copilot Enterprise | 3,900 per seat, pooled |

Organization policy, rollout state and account eligibility can still hide a
model even when GitHub's global documentation lists it as generally available.
The packaged factory defaults use GPT-6.1 Sol and Claude Sonnet 5.5. Check
that the account can select both before a real run.

## Available model and price table

The table covers the exact models in the Copilot picker snapshot supplied for
this research. It is not the complete global Copilot catalog. GitHub also lists
models such as GPT-5.4 nano, Claude Sonnet 4.6, Claude Fable 5/5.1, Claude Opus
4.8 fast mode, Kimi K2.7 Code and Kimi K3. Account, plan, policy and rollout
determine what an individual picker exposes.

Prices are **input / cached input / cache write / output**, in USD per one
million tokens. The illustrative call uses **50,000 uncached input tokens and
5,000 output tokens**, excludes cache-write charges and uses the normal context
tier. It is useful for relative comparison only. Real agent calls can have
larger outputs, hidden reasoning tokens, cache effects and long-context rates.

| Model | Factory selector | Pro | Selectable 1M | Reasoning levels | Price I/C/W/O | Illustrative call |
| --- | --- | :---: | :---: | --- | --- | ---: |
| GPT-6 Luna | `gpt-6-luna` | - | Yes | low, medium, high, xhigh, max | $0.10 / $0.01 / $0.125 / $0.50 | $0.0075 / 0.75 credits |
| GPT-6.1 Sol | `gpt-6.1-sol` | - | Yes | low, medium, high, xhigh, max | $2 / $0.10 / $2.50 / $10 | $0.1500 / 15.00 credits |
| Claude Haiku 5.5 | `claude-haiku-5.5` | - | Yes | low, medium, high, xhigh, max | $0.10 / $0.01 / $0.125 / $0.50 | $0.0075 / 0.75 credits |
| Claude Sonnet 5.5 | `claude-sonnet-5.5` | - | Yes | low, medium, high, xhigh, max | $2 / $0.10 / $2.50 / $10 | $0.1500 / 15.00 credits |
| Claude Opus 5.5 | `claude-opus-5.5` | - | Yes | low, medium, high, xhigh, max | $4 / $0.20 / $5 / $20 | $0.3000 / 30.00 credits |
| Claude Sonnet 5 | `claude-sonnet-5` | Yes | Yes | low, medium, high, xhigh, max | $2 / $0.20 / $2.50 / $10 | $0.1500 / 15.00 credits |
| Claude Opus 5 | `claude-opus-5` | No | Yes | low, medium, high, xhigh, max | $5 / $0.50 / $6.25 / $25 | $0.3750 / 37.50 credits |
| MAI-Code-1.1-Flash | `mai-code-1.1-flash` | Yes | No | low, medium, high | $0.20 / $0.02 / - / $1.20 | $0.0160 / 1.60 credits |
| GPT-6 Astra | `gpt-6-astra` | No | Yes | low, medium, high, xhigh, max | $10 / $1 / $12.50 / $50 | $0.7500 / 75.00 credits |
| Grok 4.6 | `grok-4.6` | Yes | No | low, medium, high, xhigh | $2 / $0.50 / - / $6 | $0.1300 / 13.00 credits |
| Claude Opus 4.8 | `claude-opus-4.8` | No | Yes | low, medium, high, xhigh, max | $5 / $0.50 / $6.25 / $25 | $0.3750 / 37.50 credits |
| Claude Opus 4.7 | `claude-opus-4.7` | No | Yes | low, medium, high, xhigh, max | $5 / $0.50 / $6.25 / $25 | $0.3750 / 37.50 credits |
| Claude Haiku 4.5 | `claude-haiku-4.5` | Yes | No | fixed | $1 / $0.10 / $1.25 / $5 | $0.0750 / 7.50 credits |
| GPT-5.5 | `gpt-5.5` | No | Yes | low, medium, high, xhigh | $5 / $0.50 / - / $30 | $0.4000 / 40.00 credits |
| GPT-5.4 | `gpt-5.4` | Yes | Yes | low, medium, high, xhigh | $2.50 / $0.25 / - / $15 | $0.2000 / 20.00 credits |
| GPT-5.4 mini | `gpt-5.4-mini` | Yes | No | low, medium, high, xhigh | $0.75 / $0.075 / - / $4.50 | $0.0600 / 6.00 credits |
| GPT-5.3-Codex | `gpt-5.3-codex` | Yes | Yes* | low, medium, high, xhigh | $1.75 / $0.175 / - / $14 | $0.1575 / 15.75 credits |
| GPT-5 mini | `gpt-5-mini` | Yes | No | low, medium, high | $0.25 / $0.025 / - / $2 | $0.0225 / 2.25 credits |
| MAI-Code-1-Flash | `mai-code-1-flash-picker`** | Yes | No | low, medium, high | $0.75 / $0.075 / - / $4.50 | $0.0600 / 6.00 credits |
| Grok 4.5 | `grok-4.5` | Yes | No | low, medium, high | $2 / $0.50 / - / $6 | $0.1300 / 13.00 credits |

`*` GitHub's public capability table marks GPT-5.3-Codex as selectable 1M,
while live catalog metadata observed during this research reported a 400K total
window. Verify the picker and `/context` display for the account before relying
on 1M.

`**` The live CLI catalog exposed `mai-code-1-flash-picker`. Some GitHub
documentation uses `mai-code-1-flash`. Confirm the exact selector shown by the
installed CLI before configuring this older model.

The GPT-6 Luna, GPT-6.1 Sol and Claude 5.5 rows come from the pi 1.2.0
`github-copilot` model catalog. `-` in the Pro column means plan availability
was not checked for that model.

### Long-context price changes

| Model family | Threshold | Long-tier I/C/W/O per million tokens |
| --- | ---: | --- |
| GPT-6 Luna | More than 272K input | $0.20 / $0.02 / $0.25 / $0.75 |
| GPT-6.1 Sol | More than 272K input | $4 / $0.20 / $5 / $15 |
| Claude Haiku 5.5 | More than 100K input | $0.50 / $0.05 / $0.625 / $2.50 |
| GPT-6 Astra | More than 272K input | $20 / $2 / $25 / $75 |
| GPT-5.5 | More than 272K input | $10 / $1 / - / $45 |
| GPT-5.4 | More than 272K input | $5 / $0.50 / - / $22.50 |
| Grok 4.5 and 4.6 | More than 200K input | $4 / $1 / - / $12 |

GitHub does not clearly state whether crossing the threshold reprices the whole
interaction or only tokens above it. Budget conservatively until that is
clarified.

Grok is not listed for Copilot's selectable **1M** tier. Its provider
context is larger than normal tiers, and GitHub publishes a long-context
price above 200K input. `No` in the table means "no selectable 1M", not "short
context".

## Context windows in this factory

GitHub Copilot CLI supports:

```text
--context default|long_context
--model MODEL
--reasoning-effort LEVEL
```

Official references:

- [Supported models and capabilities](https://docs.github.com/en/copilot/reference/ai-models/supported-models)
- [Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [Copilot CLI context management](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/context-management)

Each role exposes `context_tier: default|long_context`.
`CopilotAgentRuntime` passes `--context` explicitly, so a factory run does not
inherit a mutable interactive CLI setting. The packaged profiles use `default`.
Enable `long_context` only for roles whose model supports it and whose task
actually needs the larger window.

Do not treat nominal context as effective repository understanding:

- system prompts, tools, conversation and output reserve consume the window,
- Copilot starts automatic compaction near capacity and compaction is lossy,
- long-context benchmarks decline substantially as distractors increase,
- repositories are dependency graphs rather than linear documents,
- retrieval quality and verification usually matter more than raw capacity.

Use targeted search, repository maps and typed artifacts first. Treat 1M as a
burst option for a curated evidence pack, not the default for every stage.

## Models on the pi runtime

`--runtime pi` uses the same configured model and reasoning level for each role.
The factory passes the model id to pi with `--model` and the reasoning level with
`--thinking`. It does not change either value.

The model id must be one that the pi provider lists. The default provider is
`github-copilot`. A role can name another provider with `provider`. See
[Configuration](configuration.md#models). To see the ids, run this command:

```bash
pi --list-models
```

Use only ids from that list. Copilot and pi can name the same model differently,
so check the list before you switch runtimes.

Pi has no equivalent of the Copilot `--context` option. The factory ignores
`context_tier` on pi and writes a debug log line. On Copilot, the setting still
applies.

## Comparable benchmark snapshot

No public benchmark covers every model under one identical benchmark framework.
The table therefore separates engineering-agent evidence from broader evaluations:

- **DeepSWE v1.1:** autonomous repository work using a common mini-swe-agent
  benchmark setup. The value is the best published effort configuration's pass@1.
  Mean cost is the benchmark's provider-priced task cost, not GitHub AI Credit
  billing.
- **Terminal-Bench 4:** terminal tasks. The rows share benchmark version and
  trial count but still use different agent scaffolds.
- **AA Index:** Artificial Analysis Intelligence Index v4.2, an evolving
  independent composite.
- **IFBench:** out-of-distribution instruction-constraint following.
- **AA-LCR:** realistic long-document reasoning.

`V` marks a vendor-reported value where no matching official benchmark
submission was found. A blank cell means no qualifying exact-model score was
found. It does not mean zero.

### Engineering-agent evidence

| Model | DeepSWE pass@1 | Effort | 95% CI | Mean $/task | Terminal-Bench 4 |
| --- | ---: | --- | ---: | ---: | ---: |
| GPT-6 Astra | 74.12% | xhigh | 71.25-76.98 | $6.52 | 58.18% |
| Claude Opus 5 | 73.65% | max | 69.78-77.52 | $11.84 | 51.82% |
| Grok 4.6 | 67.48% | medium | 65.20-69.76 | $3.45 | 20.30% |
| GPT-5.5 | 67.04% | xhigh | 60.57-73.50 | $7.23 | - |
| Claude Opus 4.8 | 58.97% | max | 57.21-60.74 | $13.22 | 23.64% |
| Claude Sonnet 5 | 53.85% | max | 49.61-58.08 | $26.40 | 12.42% |
| Grok 4.5 | 53.76% | high | 51.48-56.04 | $2.42 | 12.42% |
| GPT-5.4 | 51.77% | xhigh | 50.27-53.27 | $5.65 | - |

The DeepSWE mean-cost column is workload-specific. For example, Sonnet 5 used
far more steps and cached input than Grok 4.6 in this benchmark framework. It
does not contradict the token price table. It shows why price per token is not
cost per successful task.

Most headline DeepSWE rows use `max` or `xhigh`, while the candidate factory
configuration above intentionally starts several roles at `medium` or `high`.
Do not expect the headline score at the cheaper setting. Compare the exact
effort intended for production during the local bakeoff.

### General reasoning and instruction evidence

| Model | AA Index | IFBench | AA-LCR |
| --- | ---: | ---: | ---: |
| GPT-6 Astra | 54.7 | - | 80.7% |
| Claude Opus 5 | 54.1 | - | - |
| Grok 4.6 | 50.6 | - | 80.3% |
| Claude Opus 4.8 | 47.8 | 62.2% | - |
| GPT-5.5 | 45.6 | 75.9% | 84.3% |
| Grok 4.5 | 45.5 | - | - |
| Claude Sonnet 5 | 45.1 | - | - |
| Claude Opus 4.7 | 44.3 | 58.6% | - |
| GPT-5.4 | 42.8 | 73.9% | - |
| GPT-5.3-Codex | 36.9 | 75.4% | - |
| GPT-5.4 mini | 31.9 | 73.3% | - |
| GPT-5 mini | 18.4 | 75.4% | - |
| Claude Haiku 4.5 | 17.4 | 42.0% | - |
| MAI-Code-1.1-Flash | - | - | - |
| MAI-Code-1-Flash | - | - | - |

Benchmark sources:

- [DeepSWE](https://deepswe.datacurve.ai/) and its [live v1.1 data](https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json)
- [Terminal-Bench](https://www.tbench.ai/)
- [Artificial Analysis model comparisons](https://artificialanalysis.ai/)
- [IFBench](https://github.com/allenai/IFBench)
- [Artificial Analysis long-context reasoning](https://artificialanalysis.ai/evaluations/artificial-analysis-long-context-reasoning)

### How to interpret the snapshot

1. DeepSWE's top two are a leading pair, not a proven strict ordering.
   Their confidence intervals overlap.
2. Terminal-Bench measures the model plus its agent scaffold. Claude Code,
   Codex, Grok Build and mini-swe-agent are not equivalent.
3. Terminal-Bench 2.1 and 4 are different task sets. A model's 80% result on
   2.1 and 12% result on 4 is not an 68-point regression.
4. Artificial Analysis is useful independent triangulation, but its index and
   weights evolve.
5. IFBench measures mechanically verifiable constraints, not the full quality
   of specification refinement.
6. Missing exact-model evidence must remain missing. Do not substitute an older
   family member and present it as the current model.

## What the requested ranking sites tell us

### DeepSWE

Best source among the three for autonomous software-engineering selection. It
contains 113 original long-horizon tasks across 91 repositories and uses
behavioral verifiers. Use it to shortlist configurations, then test them in the
factory's own Copilot CLI test framework.

Limitations include only a few whole-benchmark repeats, best-effort selection
bias, language skew toward TypeScript/Go/Python, public-task contamination over
time and a generic agent scaffold.

### BenchLM

[BenchLM](https://benchlm.ai/) is a useful meta-leaderboard. Its overall score
weights Agentic 22%, Code tasks 20%, Reason tasks 17%, Multimodal/Grounded 12%,
Knowledge 12%, Multilingual 7%, Instruction Following 5% and Math 5%.

Use its Coding and Agentic category evidence to triangulate a shortlist. Do not
use a one-point overall difference as a procurement decision. Source coverage
varies, some ranks are estimated, and calibration details are not publicly
reproducible.

### OpenRouter

[OpenRouter rankings](https://openrouter.ai/rankings) measure token traffic and
adoption, not model quality. They are useful for ecosystem maturity and
operational popularity only.

Popularity can reflect price, free tiers, prompt length,
availability and integrations rather than successful work.

## Capability-specific evidence

### Coding and repository work

- Claude Opus 5 has the strongest broad vendor-published SWE-bench set:
  96.0% Verified, 79.2% Pro and 89.5% Multilingual under Anthropic's
  five-trial maximum-effort setup.
- DeepSWE's directly comparable leading pair is Astra and Opus 5 at
  73.7-74.1%.
- MAI-Code-1.1-Flash reports 72.6% SWE-bench Verified and 62.9%
  Terminal-Bench 2.1 in Microsoft production environments. No directly
  comparable DeepSWE score was found.
- GPT-5.3-Codex remains a plausible coding specialist, but its exact current
  public coverage is thinner than the newer general models.

Primary reports:

- [Claude Opus 5 system card](https://www.anthropic.com/claude-opus-5-system-card)
- [Claude Sonnet 5 system card](https://www.anthropic.com/claude-sonnet-5-system-card)
- [GPT-6 Astra announcement](https://openai.com/index/gpt-6-astra/)
- [Grok 4.6 model card](https://media.x.ai/v1/website/card-4p6-4cd2dc57.pdf)
- [MAI-Code-1.1-Flash model card](https://microsoft.ai/pdf/MAI-Code-1.1-Flash-Model-Card.PDF)

### Reasoning and instruction following

- GPT-6 Astra and Claude Opus 5 have the strongest current evidence for hard
  planning and novel interactive reasoning.
- Astra scored 62.7% on ARC-AGI-3's standard interface and 98.6% using the
  provider adapter. That enormous difference shows that test framework design can be
  as important as model choice.
- GPT-5.5, GPT-5.3-Codex, GPT-5 mini and GPT-5.4 have strong exact IFBench
  results.

Sources:

- [ARC Prize leaderboards](https://arcprize.org/leaderboard)
- [GPQA](https://arxiv.org/abs/2311.12022)
- [Humanity's Last Exam](https://arxiv.org/abs/2501.14249)
- [IFBench](https://github.com/allenai/IFBench)
- [tau-bench](https://github.com/sierra-research/tau2-bench)

### Research and factual synthesis

Exact-model public evidence is sparse and frequently measures a complete deep
research product rather than the bare model.

- Claude Opus 5 has the strongest directly reported single-agent BrowseComp
  result among the candidates with available evidence.
- Claude Sonnet 5 is the best-supported lower-cost research alternative.
- GPT-6 Astra has strong factuality and analytical proxies, but no exact
  directly comparable BrowseComp result was found in this review.

Factory transfer is limited because published BrowseComp runs can use search,
fetch, code execution, compaction and multi-million-token budgets. Repository
skill generation in this factory has only allowlisted `web_fetch`.

Sources:

- [BrowseComp](https://arxiv.org/abs/2504.12516)
- [BrowseComp-Plus](https://github.com/texttron/BrowseComp-Plus)
- [DeepResearch Bench](https://deepresearch-bench.github.io/)
- [Search Arena](https://github.com/lmarena/search-arena)
- [FACTS Grounding](https://deepmind.google/blog/facts-grounding-a-new-benchmark-for-evaluating-the-factuality-of-large-language-models/)

### Security

Security is not one capability:

- secure code generation,
- vulnerability detection,
- patch correctness,
- exploit development,
- defensive analysis,
- resistance to instructions hidden in untrusted content.

CWE-Bench provides the strongest public review evidence. It runs a blind
repository audit where exploits must be blocked while regressions still pass:

| Model/system | CWE-Bench pass@1 |
| --- | ---: |
| Grok 4.6 | 38.2% |

GPT-6 Astra has strong offensive evidence with 85.4% on SEC-Bench Pro.
It lacks public CWE-Bench results, false-positive measurements, or
regression-preserving patch scores. It is a strong candidate for an optional
second security pass on high-risk changes, not yet the best-supported sole
reviewer.

Claude Opus 5 remains useful as a complementary security tester because it
has the strongest disclosed prompt-injection resistance in the reviewed
comparisons. Grok 4.6 has credible review evidence on private evaluations.
However, it has weaker disclosed safeguard evidence.

MAI-Code-1.1-Flash's model card says CyberBench, CyberSecEval and SecRepo were
used, but publishes no versions, scores or task breakdown. Keep it on low-risk,
deterministically verifiable work until stronger evidence is available.

Do not infer secure coding from a SWE-bench score, or reviewer precision from a
CTF score. For security-sensitive changes, models supplement SAST, dependency
checks, unit tests, fuzz tests, and human review. They do not replace them.

Sources:

- [Claude Opus 5 system card](https://www.anthropic.com/claude-opus-5-system-card)
- [Claude Sonnet 5 system card](https://www.anthropic.com/claude-sonnet-5-system-card)
- [GPT-6 Astra system card](https://deploymentsafety.openai.com/gpt-6-astra)
- [Grok 4.6 model card](https://media.x.ai/v1/website/card-4p6-4cd2dc57.pdf)
- [CWE-Bench](https://cwe-bench.com/#leaderboard)
- [CyberSecEval](https://github.com/meta-llama/PurpleLlama/tree/main/CybersecurityBenchmarks)
- [SecCodePLT](https://github.com/SecCodePLT/SecCodePLT)
- [AutoPatchBench](https://engineering.fb.com/2025/04/29/ai-research/autopatchbench-benchmark-ai-powered-security-fixes/)

### Long context

Independent long-context evidence reinforces that capacity is not
comprehension:

- AA-LCR places GPT-5.5, GPT-6 Astra and Grok 4.6 between 80.3% and 84.3%.
- On Context Arena's harder eight-needle MRCR, several nominal 1M models fall
  sharply at the largest bins.
- RULER's general result is that effective context is often much shorter than
  the claimed window.

Sources:

- [Context Arena](https://contextarena.ai/)
- [RULER](https://github.com/NVIDIA/RULER)
- [LongBench](https://github.com/THUDM/LongBench)

## Role-by-role decision framework

These weights are recommended for a local bakeoff. They intentionally differ
by role rather than producing one global model score.

| Role | Primary dimensions |
| --- | --- |
| Triage | 30% instruction following, 25% cost/latency, 20% calibration, 15% repository understanding, 10% reasoning |
| Planner | 30% decomposition/reasoning, 25% agentic reliability, 20% repository coding, 15% instruction following and ambiguity handling, 10% cost |
| L0 worker | 35% cost/latency, 30% deterministic success, 20% instruction following, 15% low false-change rate |
| L1 worker | 35% coding success, 25% tool use, 20% instruction following, 10% cost, 10% security |
| L2/L3 worker | 30% repository coding, 25% reasoning, 20% tool reliability, 15% security, 10% cost |
| Tester | 25% failure discovery, 25% terminal/tool use, 20% adversarial thinking, 15% instruction following, 15% cost |
| Reviewer | 30% defect/security recall, 25% reasoning, 20% false-positive control, 15% instruction following, 10% cost |

Do not fill missing public evidence with zero. Track an evidence-coverage score
separately, and penalize uncertainty only after the capability score is
computed from observed dimensions.

## Required local bakeoff

Public leaderboards are priors. The factory must make routing decisions from
persisted outcomes. Test with the exact Copilot CLI, prompts, tool permissions,
and retry policies.

Build a fixed evaluation set containing:

- 10-20 L0 mechanical tasks,
- 20-30 ordinary L1 changes,
- 15-20 cross-module L2 changes,
- 5-10 architecture/debugging L3 tasks,
- known-bug and known-good diffs for reviewer precision,
- repository/version questions with authoritative-source answer keys,
- security cases covering authentication, authorization, injection, unsafe
  deserialization, command execution and path handling,
- long-context cases that require tracing dependencies rather than retrieving
  one string.

For every model, record:

- exact model ID and snapshot where available,
- context tier and reasoning effort,
- pass/fail under deterministic verification,
- accepted patch rate,
- regressions and scope drift,
- reviewer true positives, false positives and missed defects,
- schema-valid artifact rate,
- time to accepted result,
- input, cached, cache-write, thought, and output tokens when available,
- total AI Credits and dollars,
- retries and failure category.

Use identical task order, prompts, permissions, timeouts and attempt budgets.
Run multiple trials for nondeterministic roles. Select Pareto-efficient models
by role instead of optimizing one blended score.

## Current factory limitations that affect selection

1. The factory does not query the live Copilot model catalog before a run.
   Unsupported model or reasoning combinations fail only when the CLI executes.
2. Reasoning is validated only as a non-empty string, not against each model's
   supported levels.
3. One reviewer is configured for all risk levels. The router cannot use Astra
   only for high-risk review.
4. The reviewer-family check uses the string before the first hyphen as the
   family. It is a useful guard, not a provider ontology.
5. There is no implemented Failure Investigator role despite its appearance in
   architecture documentation.
6. Runtime telemetry is best effort because Copilot's usage-output schema is
   experimental. Missing or malformed fields remain unknown.
7. Reported premium-request cost and nano-AIU are preserved as distinct raw
   units. The dashboard can derive a USD usage value from nano-AIU for display.
   It does not turn premium-request units into current billing.
8. Named profiles are complete `models` blocks. Other custom YAML sections are
   still not deep-merged with packaged defaults.

Model evaluation and routing must remain separate tasks. First establish a
measured policy. Then change configuration and runtime surfaces to express it.

## Source-quality rules used here

Evidence was included using this preference order:

1. GitHub documentation for Copilot prices, plans, availability and controls.
2. Benchmark-maintainer leaderboards and reproducible submission files.
3. Model-owner system cards and evaluation reports.
4. Independent aggregators with disclosed methods.
5. Secondary reporting only when no primary exact-model result was available,
   clearly labeled and excluded from decisive comparisons.

Scores were not combined when benchmark version, scaffold, effort, attempt
count or model ID differed. Product-level deep-research systems were not
treated as bare-model evidence. OpenRouter traffic was not treated as a quality
score.
