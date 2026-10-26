# Diagnostic oracle and consumer v1

`contracts/src/DiagnosticOracle.sol` accepts the exact EIP-712 report format from
the [Python signing workflow](SIGNING.md). It retains authenticated diagnostic
metadata and rejects unauthorized, expired, altered, and replayed reports.
`FailClosedConsumer.sol` demonstrates an application's guarded price-read policy.
Both are deployed and exercised only in a private, in-process EVM by the tests.

V1 accepts **unavailable diagnostics only**. Reports must have schema 1, 18
decimals, status 0, zero value/confidence placeholders, and `publicationReady=false`.
The oracle rejects a usable-looking report even when signed by an authorized key.
A fresh accepted diagnostic is not a usable feed. The consumer therefore keeps
`canAct()` false and `readPrice()` reverting with `FeedUnavailable` throughout v1.
No synthetic candidate or quality score becomes an on-chain trading price.

## Domain, configuration, and submission

The oracle uses OpenZeppelin's
[EIP712 and ECDSA implementations](https://docs.openzeppelin.com/contracts/5.x/api/utils/cryptography)
with domain name `VERDICT Diagnostic Reports`, version `1`, the actual chain ID,
and its actual deployed address. The report's 17 fields and primary type match
Python exactly. `hashReport(report)` exposes the digest for independent comparison.
ECDSA verification requires the canonical 65-byte low-s signature form with
`v=27/28`; compact 64-byte signatures are not accepted.

The constructor pins the dataset ID and initial administrator. The ETH-USD feed ID
is fixed. An administrator calls `initialize` once, after the deployed address is
known and a matching signing policy can be constructed. Initialization pins:

- Signing policy ID and retained configuration hash.
- Maximum report lifetime and observation age, each from 1 to 3,600 seconds.
- The first approved signer registry hash and complete authorization snapshot.

Initialization is restricted to the owner and is atomic. These policy/configuration
values cannot subsequently be changed on this deployment. A new policy or dataset
requires a new deployment and a new signing domain. Calling `initialize` again
cannot reset replay state.

Anyone can relay `submit(report, signature)`. Caller identity does not confer
publication authority: the recovered signer must match the current grant for the
signed key ID. Submission requires the expected feed, dataset, policy, configuration,
and nonzero assessment/evidence IDs. It also enforces:

- `roundId == observedAt`, with a nonzero round.
- `observedAt <= issuedAt < validUntil`.
- `issuedAt <= block.timestamp < validUntil`.
- Expiry no later than issuance plus the configured lifetime or observation plus
  the configured maximum age.
- Issuance within the key's activation/retirement interval and report expiry no
  later than key retirement.
- No registry revocation effective at the current block, and no emergency stop.

`submit` rejects rounds no newer than `lastRound`, previously accepted assessment
IDs, and previously accepted digests. Assessment-ID replay protection also rejects
an attempt to refresh old evidence by signing it with a larger timestamp. Registry
updates and ownership transfers do not clear this history. Failed transactions
roll back all report, replay, and registry state.

The `DiagnosticAccepted` event contains indexed assessment ID, evidence root, and
digest, plus round, recovered signer, and expiry. Local tests check these exact
links against the retained evidence bundle. `latestDiagnostic()` returns the
report, digest, signer, and a `fresh` flag. That flag is recalculated using the
current block time and current authorization, including later revocation or
retirement restrictions. Its name describes freshness of metadata, not usability.

## Registry administration and emergency revocation

Administration uses OpenZeppelin
[Ownable2Step](https://docs.openzeppelin.com/contracts/5.x/access-control): the
proposed administrator must accept ownership. Report-signing keys and administrator
accounts serve different roles. Renouncing ownership is disabled so expiry and
incident response cannot be left without an administrator.

`applyRegistry(previousId, nextId, revision, grants)` installs a complete public
snapshot. It requires the current predecessor hash, exactly the next revision, a
nonzero never-used registry hash, and 1–128 grants sorted by **hashed key ID**.
Existing IDs and signer addresses cannot be removed or reassigned. Activation
times cannot change, and retirement/revocation limits can only become stricter.
Signer addresses cannot be reused under other key IDs. The complete update is
atomic, including new entries provisionally written before a later validation fails.

Each grant has `keyId`, `signer`, `validFrom`, `validUntil`, `revokedAt`, and a
separate `revoked` boolean. The boolean distinguishes no revocation from an explicit
epoch-zero revocation. Revocation becomes effective at the specified block time,
including for reports signed before it. Planned overlap and retirement semantics
match the Python registry helpers.

`revokeSigner(keyId)` is an owner-only immediate stop. It records the block time
in a separate persistent emergency-revocation map, leaving the public registry
snapshot identifiable. Later registry updates cannot clear the stop or make that
key usable again. Repeating the stop does not postpone its original time. After
an incident, also publish an approved Python registry successor so off-chain
verifiers agree with the chain. Restore service using a fresh key ID/address.

The administrator attests that the registry hash matches the approved public JSON
and its ABI snapshot. Solidity does not reconstruct canonical JSON, recompute a
Python policy checksum, or fetch the file. The verified export helpers below reduce
translation errors; they do not remove administrator trust. The same applies to
the configured time limits and configuration hash. The contract authenticates a
publisher's commitment; it does not execute statistical scoring, prove provider
truth, or prove completeness of retained observations.

## Consumer policy

The example consumer pins an oracle address containing code, a maximum observation
age, and a minimum confidence threshold. A usable read requires the oracle's fresh
flag, available status, publication readiness, a positive value, bounded sufficient
confidence, 18 decimals, and acceptable observation age. Its oracle cannot produce
that available status in v1, so no read returns a zero placeholder as a price.

The consumer is an application-policy example, not a lending protocol, swap, token
transfer, universal circuit breaker, or production integration. A usable-price
version needs independently supported availability and a separately reviewed
schema/deployment. No such gate is relaxed here.

## Verified ABI arguments

To export submission arguments from retained artifacts:

```powershell
.venv/Scripts/verdict.exe signing contract-input --report .cache/signing/report.json --bundle .cache/evidence/baseline.json --policy .cache/signing/policy.json --registry .cache/signing/registry-v1.json --registry-id INDEPENDENTLY_TRUSTED_REGISTRY_ID --at 2026-10-20T00:05:02Z --output .cache/contracts/submit-input.json
```

As with signature verification, omit `--at` to use the current clock. An old
historical replay report will then be expired. The export checks the complete
bundle, signature, policy, trusted registry ID, authorization, and clock. Its
`report` object follows the contract tuple field names; its `signature` and
`digest` are hex strings. Configuration and registry initialization arguments are
also included. Preserve large integer types when reading the JSON in other tools.

Registry updates must remain possible when all report signers are revoked. Use
the public registry-only export, which verifies its independent hash and policy
without requiring a currently acceptable report:

```powershell
.venv/Scripts/verdict.exe signing registry-input --policy .cache/signing/policy.json --registry .cache/signing/registry-v3.json --registry-id INDEPENDENTLY_TRUSTED_CURRENT_REGISTRY_ID --output .cache/contracts/registry-input.json
```

Both commands refuse overwrites and export only public arguments. Neither loads a
private key, creates a signed transaction, accesses SQL, selects an RPC, nor sends
anything to a chain. The submission export's verification time is not an extension
of the report's expiry: the EVM always uses its own current block time.

## Deployment order and remaining gates

The tested local sequence is: retain evidence; deploy the oracle with the intended
administrator and dataset; create the domain/policy for its actual address and
chain; authorize a signer; sign and verify a fresh bundle; approve/initialize the
configuration and complete signer snapshot; relay the report; and inspect the
event, latest diagnostic, and consumer rejection. No production key or real funds
are used. Test timestamps are frozen in 2030 to avoid host-clock-dependent mining;
the data is explicitly synthetic replay evidence.

Public testnet selection, an RPC and administrator/signer arrangement, fresh-round
scheduling, publication cadence, gas budgets, retention hosting, and a deployment
runbook are the next roadmap step. There has been no public deployment. A testnet
demonstration of manipulation, outage, and recovery is still pending. Production
release additionally requires independent evidence, usable confidence, operational
key custody, registry distribution, security review, and validation on another
EVM client. The local test chain uses the experimental
[Py-EVM backend](https://github.com/ethereum/eth-tester), not a production consensus
network.

## Measured local validation

On 26 October 2026, **59 contract tests pass**. They execute compiled Solidity and
Python-produced signatures/evidence, covering accepted diagnostics, event linkage,
foreign domains and keys, altered fields, unavailable-only payloads, signature
canonicality, exact expiry, newer/duplicate/old rounds, reused assessments, signer
rotation, scheduled and emergency revocation, two-step ownership, invalid registry
successors, zero-time revocation, constructor/configuration guards, atomic failures,
consumer rejection, offline exports, and the 128-key gas boundary.

The full backend regression run passes **587 tests**, with six optional
PostgreSQL/Redis checks skipped and one existing test-client deprecation warning.
Lint, formatting, dependency lock checks, and a reproducible compiler check pass.
The compiler is pinned to 0.8.30 with OpenZeppelin 5.4.0, via-IR, optimizer runs 200,
and the Cancun target. Source line endings are normalized. Committed artifacts
carry source/lockfile fingerprints; CI recompiles and rejects any drift. Runtime
bytecode is 9,383 bytes for the oracle and 1,457 for the consumer. The maximum
registry snapshot fits the tested 25-million-gas initialization budget on a
30-million-gas local block; public-chain costs remain unverified.
