# Manipulation, outage, and recovery demonstration

The complete diagnostic pipeline now runs through an isolated EVM demonstration:
synthetic observations → scoring → persistent quarantine/weights → aggregation →
availability assessment → self-contained evidence/proof → EIP-712 signature →
reviewed transaction plan → preflight → contract execution → receipt/event audit.

There are two commands for generating evidence: `testnet demo-local` executes and
archives the whole local demonstration, while `testnet demo-evidence` retains a
current-clock snapshot for a separately operated public session. Neither connects
to a public wallet or sends a public transaction. Public Sepolia execution and its
independent verification remain pending.

## Continuous scenario

All stages share one dataset, `replay:testnet-demo-v1`, one session start, and one
weighting predecessor chain. Observations arrive every ten seconds. The local run
retains 258 observations and 46 scoring/weighting/aggregation/assessment rounds,
then publishes five checkpoints aligned to the 30-second publication grid.

| Session seconds | Intervention / checkpoint | Measured diagnostic behavior |
| --- | --- | --- |
| 0–119 / 90 | Warmup and baseline | Candidate 100; Coinbase active, weight 1,666 bps |
| 120–179 / 150 | Coinbase synthetic price multiplied by 1.35 | Coinbase quarantined at zero weight; candidate 100.01 |
| 180–299 / 270 | Normal observations resume | Coinbase active again; candidate 100 |
| 300–389 / 360 | All six providers stop producing observations | All samples stale, zero allocated weight, no candidate |
| 390 onward / 510 | Observations resume and recovery completes | All providers active with positive weight; candidate 100 |

These prices are arbitrary synthetic inputs, not market quotes. A shortened
60-second scoring window, lower sample warmup requirements, and faster recovery
parameters make the demonstration bounded. They are retained in `session.json`
and each evidence bundle, not substituted into live policy. The synthetic graph
removes unknown nodes solely to exercise nonzero weights; it supplies no verified
source independence. Oracle/DEX rows explicitly use `synthetic_fixture`
provenance with no asserted RPC block or contract. This provenance is rejected
when relabelled into the live dataset.

Every assessment and signed message remains unavailable, with null usable
value/confidence and `publication_ready=false`. On-chain zero placeholders remain
unusable. The consumer's `canAct()` is false and `readPrice()` reverts at every
checkpoint, including baseline and recovery. Recovery demonstrates diagnostic
weight restoration, not usable feed publication or coordinated-majority resistance.

## Run and inspect locally

From a source checkout with development dependencies and checked compiler artifacts:

```powershell
uv sync --frozen --cache-dir .cache/uv
npm.cmd --prefix contracts run check
.venv/Scripts/verdict.exe testnet demo-local --directory .cache/testnet-demo/my-run
```

The directory must not already exist. The command creates its own migrated SQLite
database and in-memory Py-EVM chain; it does not read the configured app database,
RPC endpoint, or wallet. The default clock is frozen at 1 January 2030, keeping
mining ahead of the host clock. An explicit future, 30-second-aligned `--start`
can replace it. This clock override exists only in local mode. Py-EVM is a
development dependency; offline archive verification does not import it.

The local profile preserves the public profile's dataset, chain ID, policy, gas
and fee ceilings, artifact IDs, and 30-second cadence, while explicitly replacing
the genesis hash with the newly created local genesis. The original profile is
retained alongside it. Public preflight therefore rejects the local profile against
the real network. Local transactions use public, unsafe development keys and fake
balances; no user key is read. Never fund those fixture addresses.

Eight local transactions pass the production planner/preflight/auditor: oracle
deployment, owner initialization, consumer deployment, and five submissions.
Receipts get two local block confirmations, never a consensus-finality label.
The runner also executes rejected calls for duplicate publication, an altered
evidence root with the original signature, exact report expiry, and unusable price
reads. At expiry `latestDiagnostic().fresh` becomes false.

Open `report.md` for the phase table and evidence links. `archive.json` inventories:

- The closed `replay.db`, session definition, complete round timeline, and profiles.
- Public policy/registry and deployed runtime bytecode.
- Complete evidence bundles, assessment inclusion proofs, and signed reports.
- Exact unsigned plans, signed raw transactions, transaction hashes, preflights,
  raw receipts, and receipt audits for every local transaction.
- Recorded contract rejection checks and the human-readable report.

The command prints `archive_id`. Retain that value independently of the archive.
No output file or previous run is overwritten. Failed runs retain partial artifacts
for inspection; they do not produce a successfully verified archive.

## Verify the archive offline

```powershell
.venv/Scripts/verdict.exe testnet demo-verify --directory .cache/testnet-demo/my-run --archive-id INDEPENDENTLY_RETAINED_ARCHIVE_ID
```

Verification checks the independently supplied archive ID, bounded file inventory,
path containment, and file hashes. It reexecutes the evidence pipeline, verifies
assessment inclusion and report signatures at the recorded inclusion time,
reconstructs initialization/submission plans, checks serialized transaction hashes
and recovered senders, and checks exact receipt/event linkage and phase summaries.
Rehashing a changed signature, removed event, changed raw transaction, or fabricated
phase result does not make it valid.

This is verification of retained evidence, signatures, transactions, and archive
linkage. It does not rerun the EVM or establish a consensus receipt proof. The
output states `evm_reexecuted=false` and `receipt_proof_verified=false`; recorded
consumer/rejection outcomes are integrity-checked local execution results. Run
`demo-local` into another directory to repeat actual execution. Repeated runs
preserve evidence roots, signed digests, and transaction identities. Archive IDs
can differ with local block data and database bytes; pin each run independently.

## Generate current evidence for public Sepolia

First complete the [public deployment, policy, signer, and wallet setup](TESTNET_RUNBOOK.md).
Use the real Sepolia profile and separate report-signing key. Local archive reports,
fixture keys, substituted genesis, and frozen timestamps are not public credentials
or current reports.

Choose one session start aligned to 30 seconds and retain it for all snapshots.
This PowerShell example starts a session 90 seconds before the current grid point,
backfilling its explicitly synthetic warmup:

```powershell
$verdictDemoEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$verdictDemoEpoch = $verdictDemoEpoch - ($verdictDemoEpoch % 30) - 90
$verdictDemoStart = [DateTimeOffset]::FromUnixTimeSeconds($verdictDemoEpoch).ToString('yyyy-MM-ddTHH:mm:ssZ')
.venv/Scripts/verdict.exe testnet demo-evidence --session-start $verdictDemoStart --directory .cache/testnet-demo/public-baseline
```

The command floors the real current clock to the latest 30-second cutoff and
retains only observations at or before that cutoff. It writes `evidence.json`,
`assessment-proof.json`, `session.json`, `timeline.json`, and a dedicated database.
The same session start reproduces the same past evidence as later rounds are
added. It does not relabel historical benchmark evidence or import old RPC claims.

Invoke it again into new directories near session offsets 150, 270, 360, and 510
for manipulation, attack recovery, outage, and recovery. Keep the same start and
profile. Check the printed cutoff; a delayed invocation snapshots the current round
rather than forging a missed timestamp. Snapshots are supported from offset 60
through 600 seconds. Invalid/future starts, unaligned starts, and finished sessions
are refused before creating output. There is no public-mode `--at` override.

Use each fresh evidence bundle with the runbook's report signing, `plan-submit`,
preflight, reviewed wallet send, and receipt audit. Publication needs more than
45 seconds of signed lifetime remaining and a newer on-chain round. Skip a missed
window and export a new current snapshot. These commands do not supply an automatic
publisher or wallet connection. Public completion needs actual deployment/update
transaction hashes, canonical receipts, and independent provider checks.

## Recorded local run

On 26 October 2026, `.cache/testnet-demo/local-20261026-1` completed five phases and
eight audited transactions. Its independently printed archive ID is
`eb0fa37a6a798b7a375769527a26aa48840b9defa9856e682d8b68a34f8b8915`.
The ignored archive remains in the local workspace; generated databases and large
evidence bundles are not committed. Public Sepolia RPC and wallet execution have
not been performed.
