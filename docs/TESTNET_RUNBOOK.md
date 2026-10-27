# Sepolia diagnostic deployment preparation

The selected application testnet is **Sepolia**, chain ID **11155111**. The checked-in
[profile](../contracts/networks/sepolia-v2.json), unsigned transaction planner,
read-only RPC preflight, and receipt auditor are implemented. No public contract
has been deployed or verified. A funded testnet transaction sender, administrator,
RPC endpoint, approved wallet workflow, and public execution of the timed replay
are still needed before rollout. The [local demonstration and fresh evidence
generator](TESTNET_DEMO.md) are implemented. There is no automatic publisher here.

Ethereum recommends Sepolia for application development; Hoodi targets protocol
and validator testing. The profile pins the chain ID and genesis hash published
by the network maintainers, rather than trusting a provider's network label.
Sources: [Ethereum networks](https://ethereum.org/developers/docs/networks/) and
[Sepolia network configuration](https://github.com/eth-clients/sepolia).

## Profile and publication policy

| Setting | Initial value |
| --- | --- |
| Profile version | `sepolia-diagnostic-v2` |
| Dataset | `replay:testnet-demo-v1` |
| Feed / signed output | ETH-USD / unavailable only |
| Publication interval | 30 seconds, on Unix cutoffs divisible by 30 |
| Maximum signed TTL / observation age | 120 seconds / 120 seconds |
| Minimum time remaining for submission | More than 45 seconds |
| Consumer age / minimum confidence | 60 seconds / 7,500 bps |
| Operational confirmations | 2, counting the inclusion block |
| Plan review lifetime | Up to 300 seconds; shorter for report submissions |
| Maximum provider head lag | 60 seconds; at most 5 seconds ahead |
| Initial registry | At most 8 retained signer entries |
| Transaction gas ceiling | 16,700,000 |
| Total fee / priority fee ceilings | 2 gwei / 0.1 gwei |
| Worst-case transaction cost ceiling | 0.2 test ETH |

Fee ceilings are operator limits, not current gas-price quotes or predicted costs.
The revised profile sets 16,700,000 gas for the oracle, 3,000,000 for the
consumer, 8,000,000 for initialization, and 3,000,000 for submission. Preflight
requires the network estimate plus a rounded-up 10% margin to fit the exact
reviewed limit. If it does not fit, create and review a new plan within the profile
ceilings. It never silently increases fees, gas, or nonces.

Sepolia has activated upgrades after the compiler's Cancun target. In particular,
the network's gas rules must be measured through its RPC; the local EVM is not
evidence of public gas costs. EIP-8037 increases code-deposit/state costs and
changes EIP-7825's 16,777,216-gas limit to apply to execution gas, with a separate
total-gas ceiling. This profile conservatively caps total transaction gas at
16,700,000. The earlier 128-key local test used a 25-million-gas budget and is
**not** a supported public deployment configuration. This profile deliberately
uses at most eight initial entries. Sources:
[Sepolia upgrade announcement](https://blog.ethereum.org/en/2026/09/17/glamsterdam-testnet-announcement)
and [EIP-8037 gas accounting](https://eips.ethereum.org/EIPS/eip-8037).

On 27 October 2026 local time, the live Sepolia RPC rejected oracle creation at
both 3,000,000 and 8,000,000 gas with code-storage out-of-gas. It estimated
14,970,098 gas with a sufficiently high simulation limit; the required 10% margin
is 16,467,108. V2 records the revised limits and lower fee ceilings. The original
V1 file and its checksum remain unchanged for archived runs. Initialization,
consumer deployment, and submission defaults still require their own live
preflights; their public execution costs have not been measured.

The revised 16,700,000-gas oracle plan subsequently passed live Sepolia preflight:
creation `eth_call`, the 14,970,098-gas estimate, the 10% margin, sender balance,
latest/pending nonce equality, predicted-address vacancy, chain/genesis, and
canonical head checks. It used a 1-gwei fee cap and 0.1-gwei priority cap, for a
maximum 0.0167 test ETH gas cost. This was simulation only; no public transaction
was signed or broadcast. The operator must rerun preflight before wallet signing.

The 120-second limits concern signed diagnostic transport. They do not relax
provider freshness, scoring, independence gates, or the evidence rules. V1 still
signs unavailable results, with null usable value/confidence off chain and zero
placeholders on chain. The consumer's `canAct()` remains false and `readPrice()`
reverts. A quality score or synthetic candidate cannot become a usable price.

The scheduling helper skips old rounds, waits for the 30-second boundary, refuses
overlap with a pending sender transaction, and skips evidence with insufficient
remaining lifetime. Submission preflight also checks the on-chain last round and
the chain clock. Missed rounds are skipped; timestamps must never be rewritten to
make retained evidence appear current. This is a scheduling policy and a set of
commands, not a running service.

## Local preparation

Run commands from a source checkout, because the tool pins repository contracts,
sources, dependency lockfile, and compiler artifacts. The Python wheel alone does
not include the contract workspace.

```powershell
uv sync --frozen --cache-dir .cache/uv
npm.cmd ci --prefix contracts
npm.cmd --prefix contracts run check
.venv/Scripts/python.exe -m pytest tests/test_contracts.py tests/test_testnet.py -q --basetemp .cache/pytest-testnet-check
.venv/Scripts/verdict.exe testnet profile
```

Review and retain the printed `profile_id`. Artifact IDs bind the ABI, creation
bytecode, runtime template, compiler settings, immutable reference locations, and
source/lockfile fingerprints. Any compiler or dependency change requires a rebuilt,
reviewed artifact and a new profile. The example profile is not a production
deployment approval.

Use separate administrator and report-signing keys. The administrator controls
registry updates and emergency stops; the report key signs EIP-712 diagnostics.
The planner rejects an initial registry containing the administrator's address.
Transaction senders use their own wallet custody. Public-network commands never
ask for a transaction private key, unlock an RPC account, sign a transaction, or
broadcast. `demo-local` executes fake accounts only inside its new in-memory EVM.
Existing [encrypted report-signing keystores](SIGNING.md) remain separate.

The read-only commands load an HTTPS endpoint from `VERDICT_SEPOLIA_RPC_URL` in the
local process environment. Set it through local secret management; do not put
credential-bearing URLs into artifacts or commit them. This variable is separate
from the mainnet ingestion RPC. Read-only RPC output and retained records omit the
URL, and provider errors are sanitized. Redirects are refused. There is no RPC
clock override in the CLI.

## Plan, review, preflight, and wallet handoff

`DEPLOYER_ADDRESS`, `ADMIN_ADDRESS`, `ORACLE_ADDRESS`, `RELAYER_ADDRESS`, reviewed
IDs, transaction hashes, and nonces below are placeholders. Obtain nonces from
the intended wallet/provider and review them; preflight requires latest and
pending sender nonces to agree exactly. Do not use the example test keys from the
test suite. All output files use exclusive creation and refuse overwrites.

Prepare an oracle deployment plan, choosing the actual wallet and owner:

```powershell
.venv/Scripts/verdict.exe testnet plan-deploy --operation deploy-oracle --sender DEPLOYER_ADDRESS --administrator ADMIN_ADDRESS --nonce 0 --output .cache/sepolia/oracle-plan.json
```

The JSON contains a type-2, zero-value unsigned transaction, exact calldata and
arguments, sender/nonce, gas and fee caps, profile/artifact IDs, review deadline,
and the predicted CREATE address. It also contains a checksum `plan_id`. A checksum
is an identity, not authorization. Independently review the arguments and retain
that ID outside the supplied plan file. Then simulate that reviewed artifact:

```powershell
.venv/Scripts/verdict.exe testnet preflight --plan .cache/sepolia/oracle-plan.json --plan-id REVIEWED_ORACLE_PLAN_ID --output .cache/sepolia/oracle-preflight-1.json
```

Preflight verifies the chain/genesis, recent canonical head, sender's latest and
pending nonces, pending balance against the worst-case cost, current base fee,
block gas limit, vacant predicted deployment address, `eth_call`, gas estimate,
and a second nonce/head consistency check. Contract calls additionally check the
pinned runtime, dataset, EIP-712 domain, administrator where relevant, and current
policy/registry context. RPC semantics follow the
[Ethereum JSON-RPC API](https://ethereum.org/developers/docs/apis/json-rpc/).

A successful preflight is a point-in-time simulation. It cannot reserve a nonce,
guarantee inclusion, or authorize spending. Before any public wallet action,
approve the concrete plan and its fresh preflight. The selected wallet must
support contract creation and exact type-2 fields. Review its network, sender,
nonce, destination/creation mode, zero value, calldata, gas, and fee limits against
`transaction` in the plan. The plan uses JSON integers; JSON-RPC uses hexadecimal
quantities. `verdict.testnet_rpc.rpc_transaction` supplies that conversion for
integrators. This milestone does not supply a wallet connection or broadcaster.

After an explicitly approved wallet sends the exact transaction, retain its hash
and audit it:

```powershell
.venv/Scripts/verdict.exe testnet receipt --plan .cache/sepolia/oracle-plan.json --plan-id REVIEWED_ORACLE_PLAN_ID --transaction-hash WALLET_TRANSACTION_HASH --output .cache/sepolia/oracle-receipt-1.json
```

Receipt auditing compares actual transaction fields with the reviewed plan and
checks execution status, canonical inclusion, gas used, the predicted deployment
address, runtime template, and constructor getters. Immutable bytecode locations
are masked only for template comparison; domain/dataset/owner or consumer getters
are checked separately. A known report digest is also reproduced independently
to check cached EIP-712 immutables. Initialization and publication require their exact
`RegistryApplied` or `DiagnosticAccepted` event, including report commitments.

Statuses are `not-found`, `pending`, `reverted`, `included`, `confirmed`, or
`finalized`. Two confirmations mean the inclusion block and one successor; they
are an operational threshold, not consensus finality. `finalized` requires a
canonical provider `finalized` block at or beyond inclusion. Unsupported finality
tags never imply finality. The CLI writes the audit but returns exit code 1 until
a successful transaction is confirmed or finalized. Invalid/mismatched checks
return an error without producing a success artifact. Previously reviewed plans
remain auditable after their preflight window expires.

RPC checks rely on the selected provider's chain/state responses; they are not
receipt proofs or independent consensus verification. Retain block hashes and
repeat checks with a separately operated provider before public acceptance.

## Initialize, deploy the consumer, and publish a diagnostic

Wait for verified oracle deployment before binding the signing domain to its
actual address. Create the public policy with the selected profile's exact values:

```powershell
.venv/Scripts/verdict.exe signing policy --version sepolia-diagnostic-v2 --dataset replay:testnet-demo-v1 --chain-id 11155111 --verifying-contract ORACLE_ADDRESS --maximum-ttl 120 --maximum-age 120 --output .cache/sepolia/policy.json
```

Create a fresh encrypted report keystore and genesis signer registry using the
[signing workflow](SIGNING.md). Set explicit activation/retirement times suitable
for the planned session, approve the public registry hash, and retain it
independently. Initialization verifies a self-contained evidence bundle and pins
its configuration without needing an already expiring signed report:

```powershell
.venv/Scripts/verdict.exe testnet plan-initialize --sender ADMIN_ADDRESS --nonce ADMIN_NONCE --oracle ORACLE_ADDRESS --bundle .cache/sepolia/round.json --policy .cache/sepolia/policy.json --registry .cache/sepolia/registry-v1.json --registry-id TRUSTED_REGISTRY_ID --output .cache/sepolia/initialize-plan.json
.venv/Scripts/verdict.exe testnet plan-deploy --operation deploy-consumer --sender DEPLOYER_ADDRESS --nonce NEXT_DEPLOYER_NONCE --oracle ORACLE_ADDRESS --output .cache/sepolia/consumer-plan.json
```

Both plans need the same review/preflight/approved-wallet/receipt sequence as the
oracle. Initialization is owner-only and one-time. Verify the consumer's source,
age/confidence settings, and unavailable behavior. Registry initialization is
limited to eight entries by this profile, although the contract supports a larger
local boundary. Ongoing rotation still requires separately reviewed full snapshots
and owner wallet actions through the ABI; these plan commands do not implement
administrative registry transactions.

For publication, use a freshly retained, verified assessment in the profile's
dataset and at a cutoff divisible by 30. `testnet demo-evidence` generates and
retains those rounds against the actual session clock; see the
[continuous session workflow](TESTNET_DEMO.md). Existing
historical benchmark bundles have different datasets/times and cannot simply be
relabelled or sent to this profile. Local execution is complete; repeating the
session through an approved public wallet remains pending.

Sign the fresh bundle with the approved report key using current time (omit
`--issued-at`), then build the unsigned submission:

```powershell
.venv/Scripts/verdict.exe signing sign --bundle .cache/sepolia/round.json --policy .cache/sepolia/policy.json --registry .cache/sepolia/registry-v1.json --key-id operator-v1 --keystore .cache/sepolia/operator-v1.keystore.json --output .cache/sepolia/report.json
.venv/Scripts/verdict.exe testnet plan-submit --sender RELAYER_ADDRESS --nonce RELAYER_NONCE --oracle ORACLE_ADDRESS --bundle .cache/sepolia/round.json --policy .cache/sepolia/policy.json --registry .cache/sepolia/registry-v1.json --registry-id TRUSTED_REGISTRY_ID --report .cache/sepolia/report.json --output .cache/sepolia/submit-plan.json
```

The planner reexecutes evidence/signature verification and checks exact policy,
domain, registry, configuration, root, and unavailable-only fields. Submission
preflight also requires the signed round to be newer on chain and leaves more
than 45 seconds before expiry by both local and chain clocks. Repeat the
review/preflight/wallet/receipt sequence promptly. If the window closes, skip that
round and retain a new round; extending an old timestamp or deadline is forbidden.

## Failures, custody, and retained evidence

- Keep at most one in-flight transaction per sender. Changed or pending nonces
  require reconciliation before creating another plan. A fee/nonce adjustment is
  a new plan and a new review, never an automatic retry.
- An unknown transaction hash does not prove it was never sent. Inspect wallet,
  pending state, and both providers. A reorganization or inconsistent receipt
  requires another read-only audit. Do not blindly redeploy at a new nonce.
- A reverted transaction consumes a nonce and gas but cannot establish a working
  deployment/update. Audit the failure and create a corrected plan only after
  reconciling its actual effects.
- On signer compromise, stop signing and relaying, use the owner's persistent
  `revokeSigner` kill switch, approve a monotonic registry successor, distribute
  its trusted hash, and audit those wallet actions. Never reactivate an old
  identity. Ownership transfer requires the new owner's explicit acceptance.
- Retain the profile and artifact IDs, public policy and all registry revisions,
  complete evidence bundles/proofs, signed reports, plans, preflight/audit records,
  transaction hashes, and canonical block references. Store encrypted keystores
  and passphrases separately from that public archive. No deletion policy or
  public artifact hosting service is introduced here.

## Validation and remaining gate

On 26 October 2026, **64 preparation/RPC checks** and the full **651-test backend
suite** pass locally; six optional PostgreSQL/Redis checks are skipped. One existing
FastAPI test-client deprecation warning remains. Lint, formatting, frozen offline
dependency installation/lock validation, and reproducible compiler checks pass.
An additional eight-key initialization probe used 1,257,487 gas locally, with an
estimate of 1,260,155 (1,386,171 including the required margin), below the default
two-million-gas initialization plan. These are local measurements, not Sepolia costs.

Tests exercise real compiled bytecode on a local Py-EVM chain with the Sepolia
chain ID, an explicitly substituted local genesis, fake funded test accounts,
and synthetic retained evidence. Production defaults still reject that genesis.
HTTP mocks verify the RPC allowlist and sanitized error handling. They cover
deployment, initialization, signed publication, unavailable consumer behavior,
receipt/event linkage, confirmations/finality, nonce races, budget/freshness
failures, reorganizations, altered transactions, and reverted/pending receipts.

This proves local tooling behavior, not public Sepolia deployment, live provider
compatibility, public gas costs, wallet handoff, or an end-to-end public recovery
demonstration. The local [five-phase demo](TESTNET_DEMO.md) adds archived end-to-end
execution and fresh evidence generation. Public results must still be recorded
with actual transaction hashes. Usable feed publication remains a separate
evidence/release gate.
