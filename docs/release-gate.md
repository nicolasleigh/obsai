# V1 release gate — 2026-09-14

| Gate | Evidence | Status |
| --- | --- | --- |
| All tests pass | `uv run pytest -q` | Pass: 183 tests |
| No known data-loss bug | Safe-write, rollback, rebuild, and failure-injection tests; see security review | Pass within documented local-process threat model |
| No write without approval | CLI/Agent write previews and explicit approval; rejected-write tests | Pass |
| OCC working | Hash conflict and disappeared-file tests | Pass |
| Rollback working | Second-operation failure and SIGINT rollback tests | Pass |
| Index rebuild recoverable | Shadow validation, interruption, disk failure, corrupt live DB tests | Pass |
| Agent bounded | Step, retrieval, repeated-call, and no-progress tests | Pass |
| Citation working | Context/citation validation tests | Pass |
| Cost preview working | Embedding plan/budget/approval tests | Pass |
| README complete | Required installation, architecture, privacy, safety, rebuild, config, development, and test sections | Pass |

Local performance gate: [10k/100k benchmark](benchmark-2026-09-14.md)
measured FTS/vector/hybrid P95 below 100 ms. Remote-provider latency and
representative user Vault content remain outside this local benchmark.
