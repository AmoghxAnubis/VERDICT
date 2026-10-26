# Local diagnostic oracle contracts

This workspace compiles `DiagnosticOracle` and `FailClosedConsumer` using pinned
Solidity 0.8.30 and OpenZeppelin Contracts 5.4.0. Tests execute the bytecode on a
local Py-EVM chain, using evidence and signatures produced by the Python pipeline.
No public RPC, funded wallet, or native Forge/Solidity installation is required.

From the repository root:

```powershell
uv sync --frozen --cache-dir .cache/uv
npm.cmd ci --prefix contracts
npm.cmd --prefix contracts run check
.venv/Scripts/python.exe -m pytest tests/test_contracts.py -q --basetemp .cache/pytest-contract-check
```

After changing Solidity sources, run `npm.cmd --prefix contracts run compile`,
review the source and generated artifacts together, then rerun the checks.
`check` compiles again and rejects any artifact drift. The committed artifacts let
Python tests run offline without downloading a compiler. Tests also check the
source and lockfile fingerprints before using them. Build settings use via-IR,
optimizer runs 200, and the Cancun EVM target. Source line endings are normalized
for Windows/Linux reproducibility.

Validated on 26 October 2026: **59 contract tests pass**; the full backend suite
passes **587 tests**, with six optional PostgreSQL/Redis checks skipped. Ruff lint,
formatting, dependency lock validation, and reproducible Solidity compilation pass.
Runtime bytecode sizes are 9,383 bytes for the oracle and 1,457 for the consumer.
The 128-key initialization boundary fits the local test's 25-million-gas budget.
Hosted CI is configured to rebuild/check artifacts before running the Python suite;
its execution has not been observed during this local validation.

See [contract behavior, input exports, and operational limits](../docs/CONTRACTS.md).
See [Sepolia plans, read-only verification, and deployment runbook](../docs/TESTNET_RUNBOOK.md).
The [continuous local demonstration](../docs/TESTNET_DEMO.md) executes eight audited
transactions across manipulation, outage, and recovery. Public deployment and
the public testnet demonstration remain pending. The public profile
limits initial snapshots to eight entries and transactions to eight million gas;
the 128-key local gas test does not establish a public-chain budget.
