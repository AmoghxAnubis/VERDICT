# Diagnostic signatures and signer rotation v1

VERDICT can sign a verified evidence bundle and verify the report offline against
an independently trusted signer registry. The workflow reproduces the evidence
pipeline before signing or accepting a report. `eth-account` supplies EIP-712
encoding, secp256k1 signing, and address recovery. Commands need no database or RPC.

V1 preserves the existing unavailable result: `status="unavailable"`, `value=null`,
`confidence=null`, and `publication_ready=false`. Its Solidity representation uses
status `0` and zero value/confidence placeholders. Those placeholders are never
usable prices or confidence estimates. Signing does not promote a synthetic
candidate or diagnostic quality score into usable feed output.

## Authenticated fields

The [EIP-712 standard](https://eips.ethereum.org/EIPS/eip-712) supplies typed-data
hashing and domain separation. The domain has name `VERDICT Diagnostic Reports`,
version `1`, an explicit positive uint256 `chainId`, and an explicit nonzero
`verifyingContract` address. An offline domain does not establish that a contract
exists there. Deployment and checking that address belong to the contract milestone.

The exact primary type and field order are:

```text
DiagnosticReport(uint16 schemaVersion,bytes32 feedId,bytes32 datasetId,bytes32 assessmentId,bytes32 evidenceRoot,bytes32 configurationHash,bytes32 policyId,bytes32 keyId,uint64 roundId,uint64 observedAt,uint64 issuedAt,uint64 validUntil,uint256 value,uint16 confidenceBps,uint8 status,uint8 decimals,bool publicationReady)
```

`feedId`, `datasetId`, and `keyId` are Keccak-256 hashes of their UTF-8 identifiers.
Assessment and evidence IDs are raw bytes of their existing SHA-256 hex identifiers.
`policyId` is the canonical-JSON SHA-256 checksum of the public signing policy.
`configurationHash` uses that checksum over the signing policy ID, dependency
registry ID, scoring/weighting/confidence policy checksums, evidence encoding/tree
versions, and assessment/aggregation/weighting/scoring schema versions. These fields
are reconstructed from the verified bundle. The evidence root also binds leaf count.
`schemaVersion` is 1 and `decimals` is 18. `signing typed-data` exports the standard
JSON projection; preserve large integer types when consuming it in other languages.

The digest is `keccak256(0x1901 || domainSeparator || hashStruct(message))`.
Signatures are 65 bytes (`r || s || v`), with `v=27/28` and canonical low `s`.
Verification explicitly rejects the malleable high-s form described by
[EIP-2](https://eips.ethereum.org/EIPS/eip-2). The dataset is signed as well as the
feed, so replay evidence cannot be relabelled as live without changing the digest.

## Time and round rules

Times use unsigned 64-bit Unix seconds. CLI timestamps require a timezone and
whole-second precision. `observedAt` is the retained assessment cutoff, and
`roundId` equals that cutoff in Unix seconds. Equal-cutoff policy variants share
the same round number. The signer cannot substitute a newer observation time.

Require `observedAt <= issuedAt < validUntil`. Expiry cannot exceed any of:

- `issuedAt + maximum_ttl_seconds`.
- `observedAt + maximum_observation_age_seconds`.
- The signing key's authorization end time.

Both time limits default to 30 seconds and accept 1 to 3,600 seconds. Verification
accepts a report only in `[issuedAt, validUntil)`. Key activation is inclusive;
retirement and revocation are exclusive. Revocation is evaluated at verification
time, including for reports issued before compromise. Explicit historical `--at`
verification prints the clock used in its result.

EIP-712 itself does not prevent replay. This offline verifier is stateless and can
verify an unexpired report repeatedly. Stateful duplicate/round rejection,
block-time freshness, and feed publication are the next contract milestone.

## Registry trust and transitions

The public signing policy pins domain, dataset, asset, decimals, and time limits.
Each public signer registry pins that policy checksum, domain, and dataset, and
contains key IDs, addresses, validity/revocation times, a revision, and its previous
registry checksum. Neither artifact contains private material.

Verification requires `--registry-id` obtained independently of the supplied files.
It checks that hash and the pinned policy. Attaching an attacker's own registry
does not authorize their signature. The current signer-registry ID is intentionally
not signed: consumers can apply new revocations or rotation windows to old reports.

Hashes and predecessor links do not grant administrative authority. Operators must
approve and distribute each new registry hash through a trusted channel. Consumers
must retain the latest approved revision/hash and reject rollback. The local
verifier checks the supplied independent hash; it does not discover the latest
registry or update remote consumers. Registry administration and report signing
are separate responsibilities.

Transition helpers require consecutive revisions, a matching predecessor, and the
same domain/dataset/policy. They preserve old IDs, addresses, and activation times,
and allow only shorter retirement/revocation boundaries. Old entries cannot be
removed, reassigned, extended, or unrevoked. New IDs and addresses must be unique.
The maximum is 128 retained keys; compaction requires a separate specification.

## Local workflow

This example assumes a replay bundle with cutoff `2026-10-20T00:05:00Z`, such as
the latest healthy-agreement demo. Replace the evidence filename and public address
with your local artifacts. Chain 31337 and the address below are local test
configuration; no deployment occurs.

```powershell
.venv/Scripts/verdict.exe signing policy --version local-diagnostic-v1 --dataset replay:adv-synthetic-baseline --chain-id 31337 --verifying-contract 0x1212121212121212121212121212121212121212 --output .cache/signing/policy.json
.venv/Scripts/verdict.exe signing key-create --output .cache/signing/operator-v1.keystore.json
```

Key creation prompts twice for a passphrase of at least 12 characters and prints
only the public address. `--password-env` can name an environment variable for
unattended local tests. There is no raw-private-key or plaintext-passphrase argument.
Use the public address to create the registry and retain its printed `registry_id`
independently:

```powershell
.venv/Scripts/verdict.exe signing registry-init --policy .cache/signing/policy.json --key-id operator-v1 --signer PUBLIC_ADDRESS --valid-from 2026-10-20T00:00:00Z --valid-until 2026-10-21T00:00:00Z --output .cache/signing/registry-v1.json
.venv/Scripts/verdict.exe signing sign --bundle .cache/evidence/baseline.json --policy .cache/signing/policy.json --registry .cache/signing/registry-v1.json --key-id operator-v1 --keystore .cache/signing/operator-v1.keystore.json --issued-at 2026-10-20T00:05:01Z --output .cache/signing/report.json
.venv/Scripts/verdict.exe signing verify --report .cache/signing/report.json --bundle .cache/evidence/baseline.json --policy .cache/signing/policy.json --registry .cache/signing/registry-v1.json --registry-id INDEPENDENTLY_TRUSTED_REGISTRY_ID --at 2026-10-20T00:05:02Z
.venv/Scripts/verdict.exe signing typed-data --report .cache/signing/report.json --output .cache/signing/typed-data.json
```

`--issued-at` is a replay-only clock override. Live signing uses the current clock.
Verification defaults to the current clock, so this old example report is expired
without its explicit historical `--at`. Typed-data export is an encoding operation,
not a verification result.

All artifacts use exclusive creation. Files cannot overwrite existing artifacts;
creation requests POSIX mode 0600, while Windows inherits its directory's ACL.
Keep keystores in a protected directory and check Windows ACLs. Back up encrypted
keys and passphrases separately. `*.keystore.json` and `.cache/` are ignored by Git.
Reports, policies, and registries contain public data and can be retained with the
bundle. No automatic deletion or key destruction is introduced.

## Rotation and revocation procedure

Create a new keystore, use its printed address in a registry successor, approve
the new registry hash, and distribute it before cutover:

```powershell
.venv/Scripts/verdict.exe signing registry-rotate --registry .cache/signing/registry-v1.json --old-key-id operator-v1 --new-key-id operator-v2 --new-signer NEW_PUBLIC_ADDRESS --activate-at 2026-10-20T00:10:00Z --retire-at 2026-10-20T00:11:00Z --valid-until 2026-10-21T00:00:00Z --output .cache/signing/registry-v2.json
```

The new key starts at activation, the old key stops at retirement, and both are
authorized during overlap. Old-key reports remain valid under the successor if
their expiry fits its shortened old-key window. Install the successor at issuers
and verifiers at least one maximum report lifetime before retirement. Tightening
retirement to before an existing report's expiry invalidates that report immediately
under the new registry; this is deliberately conservative. Confirm the new key's
operation before retiring the old one. Keep public snapshots and reports for audit.

On suspected compromise, stop the affected signer, revoke it immediately, approve
the successor hash, and ensure consumers advance their trusted hash:

```powershell
.venv/Scripts/verdict.exe signing registry-revoke --registry .cache/signing/registry-v2.json --key-id operator-v1 --output .cache/signing/registry-v3.json
```

Revocation defaults to now. Old signatures are rejected at/after that time even
when issued earlier. Restore signing with a fresh key ID/address; helpers cannot
resurrect a revoked identity. Keep compromised material offline for incident
procedures instead of reusing it. Contract state will need its own emergency
administration procedure once contracts exist.

## Key custody and validation

Development keystores use Ethereum Secret Storage v3 with scrypt `N=262144`, `r=8`,
`p=1`, `dklen=32`, and AES-128-CTR through
[`eth-account`](https://eth-account.readthedocs.io/en/v0.13.5/eth_account.html).
Unlocking accepts only this bounded profile, checks the recovered address, and
suppresses decryption error details. Creation/signing print no private material.
Python does not guarantee memory zeroization. Production custody requires an
isolated HSM/KMS or approved external signer, protected backups, restore drills,
access controls, and an approved registry distribution mechanism before deployment.

Focused tests cover an independently ABI-encoded Solidity digest, deterministic
signatures, JSON typed-data recovery, altered fields/domains, malformed and high-s
signatures, expiry/activation boundaries, wrong keys, trusted-hash rollback
rejection, rotation overlap, emergency revocation, encrypted-keystore round trips,
bounded KDF handling, and all offline CLI commands. No testnet transaction,
contract deployment, or production key is created by these checks.
