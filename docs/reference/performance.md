# Performance

The factory reduces local delays without removing required quality gates.

The standard mode remains the default. The fast mode is optional and applies
only to eligible low-risk work.

## Main improvements

- A worker completion wakes the scheduler.
- Each scheduler claim reloads current run state.
- The dashboard shares one bounded run scan.
- The CLI defers imports until a command needs them.
- Git evidence uses fewer processes and batches review location checks.
- Repository profiles reuse parsed facts only when their evidence is unchanged.
- Successful command output stores counts and a content hash instead of full logs.
- ChangeSet prose can receive one tool-free correction.
- The runtime records stage, process, prompt, gate, and rework measurements.

The fast mode uses a configured Refiner and Planner profile. It also skips the
optional polish pass.

The fast mode does not skip deterministic verification, the Tester, the
Reviewer, scope checks, or delivery controls.

See [Configuration](configuration.md#performance) and
[CLI](cli.md) for setup and command options.

## Measured results

These results compare commit `5ad8942` with the performance changes in pull
request 35.

The benchmark ran on Apple silicon with Python 3.14.7. Each local benchmark
used 15 measured runs after three warmup runs.

The p50 value is the median result. The p95 value shows the slower end of the
measured results.

| Operation | Baseline p50 | Optimized p50 | Baseline p95 | Optimized p95 |
|---|---:|---:|---:|---:|
| CLI help | 344.249 ms | 329.225 ms | 352.653 ms | 356.608 ms |
| Dashboard cold shared scan | 11.217 ms | 9.464 ms | 11.748 ms | 10.620 ms |
| Dashboard warm shared scan | 11.226 ms | 5.811 ms | 12.320 ms | 6.921 ms |
| Git evidence collection | 84.134 ms | 78.656 ms | 98.319 ms | 83.743 ms |
| Run-store scan with 1,000 runs | 131.535 ms | 130.992 ms | 143.496 ms | 138.709 ms |

The fake-runtime controller benchmark measured these median times:

| Mode | Median | Attempts | Agent calls | Optional polish |
|---|---:|---:|---:|---:|
| Standard | 346.03 ms | 2 | 8 | yes |
| Fast | 226.09 ms | 1 | 6 | no |

The fast mode reduced controller time by 34.66 percent in this synthetic test.
Both modes ran deterministic verification, the Tester, and the Reviewer.

These local measurements do not predict remote model latency. Machine load,
repository size, Git history, and model response time can change the results.

## Run the benchmark

Run the offline benchmark from the repository root:

```bash
uv run --no-sync python scripts/performance/benchmark.py \
  --iterations 15 \
  --warmup 3 \
  --output benchmark.json
```

Compare the result with an earlier report:

```bash
uv run --no-sync python scripts/performance/benchmark.py \
  --iterations 15 \
  --warmup 3 \
  --output optimized.json \
  --baseline baseline.json
```

Add `--controller-comparison` to compare standard mode with fast mode.

The benchmark uses temporary local repositories and the fake runtime. It does
not call a paid model or use the network.
