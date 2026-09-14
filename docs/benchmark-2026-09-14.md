# V1 local release benchmark — 2026-09-14

Command:

```bash
uv run python scripts/benchmark.py --notes 10000 --chunks-per-note 10 --vector-chunks 100000 --queries 30
```

Environment: macOS Darwin 25.2.0, arm64, local temporary filesystem. The
workload uses 10,000 generated Markdown notes, 100,000 parsed chunks, and
100,000 deterministic 3-dimensional vectors. It makes no network calls.

| Measure | Result |
| --- | ---: |
| CLI `--help` startup P50 / P95 | 135.67 / 181.00 ms |
| FTS P50 / P95 | 0.19 / 3.89 ms |
| sqlite-vec P50 / P95 | 1.25 / 1.74 ms |
| Hybrid P50 / P95 | 2.26 / 6.65 ms |
| Initial index | 7.92 s |
| Unchanged incremental update | 1.27 s |

The local FTS, vector, and hybrid paths met the 100 ms P95 target in this
synthetic workload. CLI startup has no 100 ms gate in the PRD and was measured
separately. Vector/hybrid timings exclude remote query embedding, provider
latency, and network time; results are not a production end-to-end SLA. Queries
share similar terms and do not exercise expensive metadata filters. Repeat on
target hardware and representative Vault content before making a release
performance claim.
