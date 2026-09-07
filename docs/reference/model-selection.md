# Model selection, cost and benchmark guide

This guide is a dated decision aid for the models available through GitHub
Copilot. It combines:

- authoritative GitHub pricing, plan and capability documentation,
- exact-model software-engineering benchmarks where they exist,
- reasoning, instruction-following, research, security and long-context
  evidence,
- the constraints of this factory's current deterministic router.

**Snapshot date:** 2026-09-07.

Model catalogs, prices and benchmark leaderboards change. Recheck the linked
sources before making a long-lived routing decision.

## Executive recommendation

There is no single best model across every factory role.

| Need | Strong candidates | Why |
| --- | --- | --- |
| Lowest-cost mechanical work | `mai-code-1.1-flash`, `gpt-5.6-luna` | Both cost about $0.016 for the illustrative small call below. MAI is coding-specialized; Luna has broader reasoning evidence. |
| Cost-effective implementation | `gemini-3.8-flash`, `gpt-5.6-terra` | Gemini 3.8 has unusually strong DeepSWE results for its measured task cost. Terra offers broader reasoning evidence and selectable long context. |
| Hard implementation | `claude-opus-5`, `gpt-5.6-sol` | Strong repository, terminal, research and security evidence. |
| Hard planning and novel reasoning | `gpt-6-astra`, `claude-opus-5` | Strongest current reasoning and agentic evidence, but expensive. |
| Web research and synthesis | `claude-opus-5`, then `gpt-5.6-sol` | Best available exact-model BrowseComp and synthesis evidence, with major harness caveats. |
| Cost-effective testing experiment | `gemini-3.8-flash`, `gpt-5.6-terra` | Attractive economics and coding evidence, but weaker Terminal-Bench 4 results than the frontier models; validate locally before adopting. |
| High-value final review | `gpt-6-astra`, `gpt-5.6-sol` | Strong reasoning and analysis while remaining independent from Claude/MAI worker families. |
| Security-focused testing | `gpt-5.6-sol`, `claude-opus-5` | Strongest exact offensive-security and vulnerability-research evidence. |
| Untrusted-repository review | `claude-opus-5` has the strongest published prompt-injection result | This conflicts with the factory's reviewer-family rule when any Claude worker is configured; use it as a separate specialist or change the worker families. |

The strongest public autonomous-coding cluster is currently **GPT-6 Astra,
Gemini 3.8 Flash, Claude Opus 5 and GPT-5.6 Sol**. Their best DeepSWE v1.1
scores overlap statistically, so the ranking does not establish one universal
winner. Price differs by more than an order of magnitude, which makes a local
bakeoff more valuable than selecting the top headline score.

For the current factory, a sensible evidence-based experiment is below. Paste
this `models` block into a complete copy of `config/factory.example.yaml`;
custom configuration files are not merged with the packaged defaults.

```yaml
models:
  triage:     { model: "gpt-5.6-terra",        reasoning: "medium" }
  refiner:    { model: "gpt-5.5",              reasoning: "high" }
  researcher: { model: "claude-opus-5",        reasoning: "high" }
  planner:    { model: "claude-opus-5",        reasoning: "high" }
  workers:
    L0:       { model: "mai-code-1.1-flash",   reasoning: "medium" }
    L1:       { model: "gemini-3.8-flash",     reasoning: "high" }
    L2:       { model: "claude-sonnet-5",      reasoning: "high" }
    L3:       { model: "claude-opus-5",        reasoning: "high" }
  tester:     { model: "gemini-3.8-flash",     reasoning: "high" }
  reviewer:   { model: "gpt-5.6-sol",          reasoning: "high" }
```

This is a **candidate for measurement**, not a new packaged default. It keeps a
cheap L0, tests Gemini 3.8's price/performance at L1, retains Claude for complex
implementation, and preserves a different-family final reviewer. A
quality-first trial could replace the planner and reviewer with GPT-6 Astra,
but its uncached list-price illustrative call is 2.5 times Sol and about 4.7
times Terra. Real agent-loop cost can rank models differently because cached
input, reasoning output, step count and retry behavior dominate.

This candidate requires Copilot Pro+ or another plan that includes Opus 5,
GPT-5.5 and GPT-5.6 Sol. On Copilot Pro, use only rows marked `Yes` in the
price table and re-check the reviewer-family constraint.

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

The factory does not currently receive or reconstruct token usage, so its
status and dashboard cannot report actual model cost. The figures below are
planning inputs, not runtime accounting.

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
The packaged factory defaults use Claude Opus 5 and GPT-5.6 Sol, so Copilot Pro
alone is not sufficient for a real run with the unchanged defaults.

## Available model and price table

The table covers the exact models in the Copilot picker snapshot supplied for
this research. It is not the complete global Copilot catalog. GitHub also lists
models such as GPT-5.4 nano, Claude Sonnet 4.6, Claude Fable 5/5.1, Claude Opus
4.8 fast mode, Kimi K2.7 Code and Kimi K3; account, plan, policy and rollout
determine what an individual picker exposes.

Prices are **input / cached input / cache write / output**, in USD per one
million tokens. The illustrative call uses **50,000 uncached input tokens and
5,000 output tokens**, excludes cache-write charges and uses the normal context
tier. It is useful for relative comparison only; real agent calls can have
larger outputs, hidden reasoning tokens, cache effects and long-context rates.

| Model | Factory selector | Pro | Selectable 1M | Reasoning levels | Price I/C/W/O | Illustrative call |
| --- | --- | :---: | :---: | --- | --- | ---: |
| Claude Sonnet 5 | `claude-sonnet-5` | Yes | Yes | low, medium, high, xhigh, max | $2 / $0.20 / $2.50 / $10 | $0.1500 / 15.00 credits |
| Claude Opus 5 | `claude-opus-5` | No | Yes | low, medium, high, xhigh, max | $5 / $0.50 / $6.25 / $25 | $0.3750 / 37.50 credits |
| GPT-5.6 Sol | `gpt-5.6-sol` | No | Yes | low, medium, high, xhigh, max | $4 / $0.40 / $5 / $20 | $0.3000 / 30.00 credits |
| GPT-5.6 Terra | `gpt-5.6-terra` | Yes | Yes | low, medium, high, xhigh, max | $2 / $0.20 / $2.50 / $12 | $0.1600 / 16.00 credits |
| GPT-5.6 Luna | `gpt-5.6-luna` | Yes | Yes | low, medium, high, xhigh, max | $0.20 / $0.02 / $0.25 / $1.20 | $0.0160 / 1.60 credits |
| Gemini 3.7 Flash | `gemini-3.7-flash` | Yes | No | low, medium, high | $0.75 / $0.075 / - / $3.75 | $0.0563 / 5.63 credits |
| Gemini 3.8 Flash | `gemini-3.8-flash` | Yes | No | low, medium, high | $0.75 / $0.075 / - / $3.75 | $0.0563 / 5.63 credits |
| Gemini 3.6 Flash | `gemini-3.6-flash` | Yes | No | minimal, low, medium, high | $0.75 / $0.075 / - / $3.75 | $0.0563 / 5.63 credits |
| Gemini 3.5 Flash | `gemini-3.5-flash` | Yes | No | minimal, low, medium, high | $1.50 / $0.15 / - / $9 | $0.1200 / 12.00 credits |
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

`**` The live CLI catalog exposed `mai-code-1-flash-picker`; some GitHub
documentation uses `mai-code-1-flash`. Confirm the exact selector shown by the
installed CLI before configuring this older model.

The Gemini 3.6, 3.7 and 3.8 Flash rates are promotional through
2026-12-31 according to GitHub's pricing page.

### Long-context price changes

| Model family | Threshold | Long-tier I/C/W/O per million tokens |
| --- | ---: | --- |
| GPT-5.6 Sol | More than 272K input | $8 / $0.80 / $10 / $30 |
| GPT-5.6 Terra | More than 272K input | $4 / $0.40 / $5 / $18 |
| GPT-5.6 Luna | More than 200K input | $0.40 / $0.04 / $0.50 / $1.80 |
| GPT-6 Astra | More than 272K input | $20 / $2 / $25 / $75 |
| GPT-5.5 | More than 272K input | $10 / $1 / - / $45 |
| GPT-5.4 | More than 272K input | $5 / $0.50 / - / $22.50 |
| Grok 4.5 and 4.6 | More than 200K input | $4 / $1 / - / $12 |

GitHub does not clearly state whether crossing the threshold reprices the whole
interaction or only tokens above it. Budget conservatively until that is
clarified.

Grok is not listed for Copilot's selectable **1M** tier, but its provider
context is larger than the normal Copilot tier and GitHub publishes a
long-context price above 200K input. `No` in the table means "no selectable
1M", not "short context".

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

The factory currently exposes `model` and `reasoning`, but **not**
`context_tier`. `CopilotAgentRuntime` therefore does not pass `--context`, and
factory calls use the CLI's default context tier even when a model supports 1M.
Adding 1M support would require a typed configuration field, validation,
propagation through `AgentRequest`, and the runtime argument.

Do not treat nominal context as effective repository understanding:

- system prompts, tools, conversation and output reserve consume the window,
- Copilot starts automatic compaction near capacity and compaction is lossy,
- long-context benchmarks decline substantially as distractors increase,
- repositories are dependency graphs rather than linear documents,
- retrieval quality and verification usually matter more than raw capacity.

Use targeted search, repository maps and typed artifacts first. Treat 1M as a
burst option for a curated evidence pack, not the default for every stage.

## Comparable benchmark snapshot

No public benchmark covers every model under one identical harness. The table
therefore separates engineering-agent evidence from broader evaluations:

- **DeepSWE v1.1:** autonomous repository work using a common mini-swe-agent
  harness. The value is the best published effort configuration's pass@1.
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
found; it does not mean zero.

### Engineering-agent evidence

| Model | DeepSWE pass@1 | Effort | 95% CI | Mean $/task | Terminal-Bench 4 |
| --- | ---: | --- | ---: | ---: | ---: |
| GPT-6 Astra | 74.12% | xhigh | 71.25-76.98 | $6.52 | 58.18% |
| Gemini 3.8 Flash | 73.83% | high | 72.41-75.24 | $2.36 | 19.09% |
| Claude Opus 5 | 73.65% | max | 69.78-77.52 | $11.84 | 51.82% |
| GPT-5.6 Sol | 72.67% | max | 69.84-75.50 | $8.39 | 37.27% |
| GPT-5.6 Terra | 69.62% | max | 67.07-72.18 | $4.95 | 21.52% |
| Grok 4.6 | 67.48% | medium | 65.20-69.76 | $3.45 | 20.30% |
| GPT-5.6 Luna | 67.19% | max | 63.20-71.18 | $3.03 | 17.27% |
| GPT-5.5 | 67.04% | xhigh | 60.57-73.50 | $7.23 | - |
| Gemini 3.7 Flash | 65.49% | medium | 62.40-68.57 | $2.03 | 11.21% |
| Claude Opus 4.8 | 58.97% | max | 57.21-60.74 | $13.22 | 23.64% |
| Claude Sonnet 5 | 53.85% | max | 49.61-58.08 | $26.40 | 12.42% |
| Grok 4.5 | 53.76% | high | 51.48-56.04 | $2.42 | 12.42% |
| GPT-5.4 | 51.77% | xhigh | 50.27-53.27 | $5.65 | - |
| Gemini 3.6 Flash | 46.68% | high | 42.98-50.39 | $4.42 | - |
| Gemini 3.5 Flash | 36.06% | high | 32.10-40.03 | $3.45 | - |

The DeepSWE mean-cost column is workload-specific. For example, Sonnet 5 used
far more steps and cached input than Gemini 3.8 in this harness. It does not
contradict the token price table; it shows why price per token is not cost per
successful task.

Most headline DeepSWE rows use `max` or `xhigh`, while the candidate factory
configuration above intentionally starts several roles at `medium` or `high`.
Do not expect the headline score at the cheaper setting. Compare the exact
effort intended for production during the local bakeoff.

### General reasoning and instruction evidence

| Model | AA Index | IFBench | AA-LCR |
| --- | ---: | ---: | ---: |
| GPT-6 Astra | 54.7 | - | 80.7% |
| Claude Opus 5 | 54.1 | - | - |
| GPT-5.6 Sol | 51.3 | 72.7% | 84.0% |
| Grok 4.6 | 50.6 | - | 80.3% |
| Claude Opus 4.8 | 47.8 | 62.2% | - |
| Gemini 3.8 Flash | 47.1 | - | 84.0% medium |
| GPT-5.6 Terra | 46.8 | 71.2% | 83.0% |
| GPT-5.5 | 45.6 | 75.9% | 84.3% |
| Grok 4.5 | 45.5 | - | - |
| Gemini 3.7 Flash | 45.2 | - | - |
| Claude Sonnet 5 | 45.1 | - | - |
| Claude Opus 4.7 | 44.3 | 58.6% | - |
| GPT-5.6 Luna | 43.4 | - | 83.7% |
| GPT-5.4 | 42.8 | 73.9% | - |
| Gemini 3.6 Flash | 40.3 | - | - |
| Gemini 3.5 Flash | 39.7 | 76.3% | - |
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

1. DeepSWE's top four are a leading cluster, not a proven strict ordering.
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
factory's own Copilot CLI harness.

Limitations include only a few whole-benchmark repeats, best-effort selection
bias, language skew toward TypeScript/Go/Python, public-task contamination over
time and a generic agent scaffold.

### BenchLM

[BenchLM](https://benchlm.ai/) is a useful meta-leaderboard. Its overall score
weights Agentic 22%, Coding 20%, Reasoning 17%, Multimodal/Grounded 12%,
Knowledge 12%, Multilingual 7%, Instruction Following 5% and Math 5%.

Use its Coding and Agentic category evidence to triangulate a shortlist. Do not
use a one-point overall difference as a procurement decision: source coverage
varies greatly, some ranks are estimated, and the full normalization,
missing-data prior and external-consensus calibration are not publicly
reproducible.

### OpenRouter

[OpenRouter rankings](https://openrouter.ai/rankings) measure token traffic and
adoption, not model quality. They are useful for ecosystem maturity and
operational popularity only.

For example, GPT-5.6 Luna ranked second by weekly OpenRouter tokens in the
research snapshot while ranking tenth among DeepSWE-tested models and
37th on BenchLM. Popularity can reflect price, free tiers, prompt length,
availability and integrations rather than successful work.

## Capability-specific evidence

### Coding and repository work

- Claude Opus 5 has the strongest broad vendor-published SWE-bench set:
  96.0% Verified, 79.2% Pro and 89.5% Multilingual under Anthropic's
  five-trial maximum-effort setup.
- DeepSWE's directly comparable leading cluster is Astra, Gemini 3.8, Opus 5
  and Sol at 72.7-74.1%.
- Gemini 3.8 is the standout cost/performance candidate: near-frontier
  DeepSWE at Flash pricing.
- Gemini 3.8's Terminal-Bench 4 result is weak under mini-swe-agent. Using it as
  a Tester is a price-driven hypothesis, not an evidence-backed conclusion
  about native-harness terminal performance.
- MAI-Code-1.1-Flash reports 72.6% SWE-bench Verified and 62.9%
  Terminal-Bench 2.1 in Microsoft's production Copilot harness, but no
  directly comparable DeepSWE or current independent composite result was
  found.
- GPT-5.3-Codex remains a plausible coding specialist, but its exact current
  public coverage is thinner than the newer general models.

Primary reports:

- [Claude Opus 5 system card](https://www.anthropic.com/claude-opus-5-system-card)
- [Claude Sonnet 5 system card](https://www.anthropic.com/claude-sonnet-5-system-card)
- [GPT-5.6 announcement](https://openai.com/index/gpt-5-6/)
- [GPT-6 Astra announcement](https://openai.com/index/gpt-6-astra/)
- [Gemini 3.8 Flash evaluation](https://storage.googleapis.com/deepmind-media/gemini/gemini_3-8_flash_model_evaluation.pdf)
- [Grok 4.6 model card](https://media.x.ai/v1/website/card-4p6-4cd2dc57.pdf)
- [MAI-Code-1.1-Flash model card](https://microsoft.ai/pdf/MAI-Code-1.1-Flash-Model-Card.PDF)

### Reasoning and instruction following

- GPT-6 Astra and Claude Opus 5 have the strongest current evidence for hard
  planning and novel interactive reasoning.
- Astra scored 62.7% on ARC-AGI-3's standard interface and 98.6% using the
  provider adapter. That enormous difference shows that harness design can be
  as important as model choice.
- GPT-5.5, GPT-5.3-Codex, GPT-5 mini, GPT-5.4 and Sol/Terra have strong exact
  IFBench results. Gemini 3.5 Flash leads the available exact rows, despite not
  being the strongest general reasoning model.
- Higher effort is not monotonically better. Gemini 3.8 medium beat high on
  AA-LCR in the observed snapshot.

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
- GPT-5.6 Sol is close and remains a strong current Researcher.
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

The strongest exact public evidence is available for Claude Opus 5 and
GPT-5.6 Sol. Opus 5 shows stronger results than Sonnet 5 on vulnerability and
exploit research and had the best external indirect-prompt-injection result in
the reviewed exact-model data. Sol has extensive CTF and multi-stage
vulnerability-research evidence, but its prompt-injection results vary sharply
between OpenAI's and Anthropic's harnesses.

MAI-Code-1.1-Flash's model card says CyberBench, CyberSecEval and SecRepo were
used, but publishes no versions, scores or task breakdown. Keep it on low-risk,
deterministically verifiable work until stronger evidence is available.

Do not infer secure coding from a SWE-bench score, or reviewer precision from a
CTF score. For security-sensitive changes, models supplement SAST, dependency
scanning, tests, fuzzing and human review; they do not replace them.

Sources:

- [Claude Opus 5 system card](https://www.anthropic.com/claude-opus-5-system-card)
- [Claude Sonnet 5 system card](https://www.anthropic.com/claude-sonnet-5-system-card)
- [GPT-5.6 system card](https://deploymentsafety.openai.com/gpt-5-6/gpt-5-6.pdf)
- [CyberSecEval](https://github.com/meta-llama/PurpleLlama/tree/main/CybersecurityBenchmarks)
- [SecCodePLT](https://github.com/SecCodePLT/SecCodePLT)
- [AutoPatchBench](https://engineering.fb.com/2025/04/29/ai-research/autopatchbench-benchmark-ai-powered-security-fixes/)

### Long context

Independent long-context evidence reinforces that capacity is not
comprehension:

- AA-LCR places GPT-5.5, Sol, Gemini 3.8, Luna and Terra in a narrow
  83.0-84.3% group.
- On Context Arena's harder eight-needle MRCR, several nominal 1M models fall
  sharply at the largest bins.
- Gemini 3.7 Flash retained the strongest reported full-1M result in that
  snapshot, even though Copilot does not expose its provider-level 1M window
  through the selectable long-context tier.
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
| Refiner | 30% instruction following, 25% reasoning, 20% ambiguity handling, 15% context, 10% cost |
| Researcher | 30% source discovery, 25% source-to-claim support, 20% synthesis, 15% uncertainty calibration, 10% cost |
| Planner | 30% decomposition/reasoning, 25% agentic reliability, 20% repository coding, 15% instruction following, 10% cost |
| L0 worker | 35% cost/latency, 30% deterministic success, 20% instruction following, 15% low false-change rate |
| L1 worker | 35% coding success, 25% tool use, 20% instruction following, 10% cost, 10% security |
| L2/L3 worker | 30% repository coding, 25% reasoning, 20% tool reliability, 15% security, 10% cost |
| Tester | 25% failure discovery, 25% terminal/tool use, 20% adversarial thinking, 15% instruction following, 15% cost |
| Reviewer | 30% defect/security recall, 25% reasoning, 20% false-positive control, 15% instruction following, 10% cost |

Do not fill missing public evidence with zero. Track an evidence-coverage score
separately, and penalize uncertainty only after the capability score has been
computed from observed dimensions.

## Required local bakeoff

Public leaderboards are priors. The factory should make final routing decisions
from its own persisted outcomes using the exact Copilot CLI, prompts, tool
permissions and retry policy it will deploy.

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
- input, cached, cache-write, reasoning and output tokens when available,
- total AI Credits and dollars,
- retries and failure category.

Use identical task order, prompts, permissions, timeouts and attempt budgets.
Run multiple trials for nondeterministic roles. Select Pareto-efficient models
by role instead of optimizing one blended score.

## Current factory limitations that affect selection

1. Model and reasoning are configurable, but context tier is not.
2. The factory does not query the live Copilot model catalog before a run.
   Unsupported model or reasoning combinations fail only when the CLI executes.
3. Reasoning is validated only as a non-empty string, not against each model's
   supported levels.
4. One reviewer is configured for all risk levels; the router cannot use Astra
   only for high-risk review.
5. The reviewer-family check uses the string before the first hyphen as the
   family. It is a useful guard, not a provider ontology.
6. There is no implemented Failure Investigator role despite its appearance in
   architecture documentation.
7. Actual token usage and cost are not persisted because the runtime does not
   currently report them.
8. A custom YAML is not deep-merged with the packaged defaults. Copy the full
   example before changing the `models` block.

These limitations mean model evaluation and model routing should remain
separate tasks: first establish a measured role-specific policy, then change
the typed configuration/runtime surfaces required to express it.

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
