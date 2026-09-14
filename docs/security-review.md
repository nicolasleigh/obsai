# V1 security review

Review scope: Vault parsing, indexing, retrieval, Agent planning, and Vault
writes. Vault Markdown and metadata are untrusted input; the user's direct
request and explicit approval are the only authority for a write.

| Boundary | V1 control | Verification |
| --- | --- | --- |
| Path traversal | SafeWriteService accepts only Vault-relative `.md` paths, rejects `..`, absolute paths, backslashes, and reserved internal directories | `tests/unit/test_safe_write.py` |
| Symlink escape | Scanner excludes symlinked files/directories; safe write rejects symlinked path components; shadow index rejects symlink targets | Vault scan and safe-write tests; rebuild tests |
| Ignored folders | Scanner prunes `.obsaiignore` matches before parsing and indexing | Vault scan and incremental-index tests |
| Arbitrary code | Markdown parser reads UTF-8/YAML safely; code fences, HTML, Dataview, and JS are never executed | Parser tests, including fenced fake links |
| Dangerous overwrite | Preflight checks destination collision, original hash, permissions, and free space; writes use same-directory atomic replacement; multi-file changes snapshot and roll back | Safe-write, transaction, and hardening tests |
| Prompt injection | Ask system instructions mark note evidence untrusted; Agent planner instructions mark tool summaries untrusted; workflow rejects write tools for read-only intent; every write pauses for approval | Answering and Agent workflow tests |
| Secret handling | API key is read from the environment only at provider call time; structured metrics contain timings and aggregate counts, never note bodies, queries, paths, or keys | Metric hardening test and source review |
| Recovery | Shadow build is validated before atomic swap; failed builds preserve the live index; unfinished Vault transaction journals are detected on next CLI invocation | Rebuild and transaction failure-injection tests |

Residual limitations: a malicious process with write access to the same Vault
can race path checks and file operations; this CLI does not defend against a
hostile local actor with equivalent filesystem permissions. Durability of
directory `fsync` is best-effort on platforms where it is unavailable. Shadow
rebuild replaces metadata and discards cached vectors, requiring an approved
embedding run afterward. The release benchmark measures local sqlite-vec
lookup and does not include remote embedding or LLM network latency.
