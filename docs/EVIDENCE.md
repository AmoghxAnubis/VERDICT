# Evidence bundles and inclusion proofs v1

An evidence bundle commits to one retained diagnostic assessment and the inputs
needed to reproduce it. The export contains the assessment, its aggregation,
the entire weighting predecessor chain back to genesis, every corresponding
scoring report, full dependency registries including retained evidence content,
and the union of their referenced observations, including raw provider payloads.
These include observations selected for scoring and subsequently rejected as
outliers or excluded from allocation. Policies, algorithm versions, cutoff times,
decisions, and explanations travel with the reports.

This is a commitment to retained evidence. It does not authenticate the collector,
prove provider truth, or prove that the collector retained every possible input.
The bundled synthetic registries still do not establish independent upstreams.
Exports reproduce existing diagnostic decisions; usable feed output stays null.

## Operation and retention

Apply the existing migrations and retain an assessment first. For example, seeding
the dashboard demos creates isolated assessments that can be listed through
`GET /v1/confidence?dataset=replay:adv-synthetic-single_spike`.

```powershell
.venv/Scripts/verdict.exe evidence export ASSESSMENT_SHA256 --dataset replay:adv-synthetic-single_spike --output .cache/evidence/round.json
.venv/Scripts/verdict.exe evidence verify .cache/evidence/round.json
.venv/Scripts/verdict.exe evidence proof .cache/evidence/round.json --index 0 --output .cache/evidence/proof.json
.venv/Scripts/verdict.exe evidence verify-proof .cache/evidence/proof.json --root INDEPENDENTLY_OBTAINED_ROOT_SHA256
```

`export` reads retained SQL evidence and creates the requested file. It fails on
missing or inconsistent records. `verify`, `proof`, and `verify-proof` work without
a database or network access. Output files use exclusive creation and cannot
overwrite existing files. Exported bundles retain complete copies of their inputs
and can be verified after closing or removing access to the source database.
No automatic deletion, retention expiry, or compaction is introduced. `.cache/`
is ignored by Git; use a backed-up directory when a bundle must survive workspace
cleanup. SQL evidence retains its existing immutable identities and schemas.

Bundle verification checks canonical ordering and representations, identities,
exact dependency closure, checksums, and the Merkle root. It reproduces every
included score and weighting transition, then the selected aggregation and
availability assessment, using decimal precision 60 and round-half-even.
Missing predecessor state, changed raw observations, unrelated records, and
cross-dataset substitution fail verification, even if an attacker recomputes the
bundle's root. A proof alone verifies inclusion; it does not reproduce the pipeline.
Use a root obtained independently of the proof when verifying inclusion. Signed
roots and authorization are available through the [offline signing workflow](SIGNING.md).

V1 limits bundles to 32,768 leaves, 512 weighting predecessors, and 128 levels of
JSON nesting. Bounds fail explicitly. Long-running datasets exceeding these limits
need a separately specified checkpoint format; exports never truncate history.
This implementation adds no HTTP proof endpoint or signature/publication path.

## Canonical byte encoding: verdict-tlv-v1

The JSON export is a transport container. Its whitespace and object-key order do
not determine the committed bytes. Each leaf is encoded with the following rules.
Lengths/counts are unsigned 32-bit integers in big-endian order. Concatenation
below appends raw bytes, without delimiters beyond the specified lengths.

| JSON type | Tag (hex) | Following bytes |
| --- | --- | --- |
| null | `00` | none |
| false | `01` | none |
| true | `02` | none |
| integer | `03` | byte length, then ASCII base-10 integer; no leading zeros or plus sign |
| floating number | `04` | exactly 8 IEEE-754 binary64 bytes, big-endian |
| string | `05` | UTF-8 byte length, then strict UTF-8 bytes |
| array | `06` | element count, then recursively encoded values in array order |
| object | `07` | entry count, then encoded key/value pairs sorted by the key's UTF-8 bytes |

Object keys must be strings. Nonfinite numbers, invalid Unicode surrogates, and
non-JSON types fail encoding. Unicode is not normalized. Negative floating zero
differs from positive floating zero. Integer `1`, floating `1.0`, and string `"1"`
have distinct encodings. Prices remain decimal strings in normalized records;
binary64 encoding preserves floating telemetry and raw JSON numbers without
depending on a language's JSON float printer. A consumer must preserve JSON number
types when parsing the transport, including integers beyond JavaScript's exact
number range. Existing report IDs continue to use their original checksum rules;
this codec does not change them.

The following input has this exact hexadecimal encoding:

```text
{"b":[true,null,"é"],"a":1}
0700000002050000000161030000000131050000000162060000000302000500000002c3a9
```

Each leaf encodes all six fields: `schema_version`, `dataset`, `assessment_id`,
`kind`, `record_id`, and `payload`. Kinds are `assessment`, `aggregation`, `weighting`,
`scoring`, `registry`, and `observation`. Leaves are unique and sorted ascending by
`(kind, record_id)` using these ASCII identifiers. Payloads use their model's
canonical JSON representation, including normalized timestamps and decimal text.
The outer bundle's dataset and assessment ID must match every leaf's context.

## Merkle construction: verdict-merkle-v1

Let `H` be SHA-256 and `D` the ASCII bytes `VERDICT-EVIDENCE-V1` followed by a zero
byte. Hashes below are raw 32-byte values internally; exports use lowercase hex.

```text
leaf_hash(x) = H(D || 0x00 || canonical_bytes(x))
node(L, R)   = H(D || 0x01 || L || R)
root         = H(D || 0x02 || uint32_be(leaf_count) || tree_hash)
```

A one-leaf tree's `tree_hash` is that leaf hash. For `n > 1`, split at the largest
power of two strictly smaller than `n`; hash the left and right subtrees with
`node`. Odd leaves are not duplicated, and child hashes are not sorted. Empty
trees are invalid. The root seals the leaf count as well as the tree hash.

A proof contains the complete contextual leaf, its zero-based `leaf_index`,
`leaf_count`, and sibling hashes in bottom-up order. Left/right orientation follows
the index and recursive split rule. At most 15 siblings are needed. Verification
requires the exact path: extra or missing siblings, a changed index/count/context,
or a different expected root fail. The proof does not carry a root to trust.

## Validation scope

Validated on 25 October 2026: 19 focused evidence tests pass. The full backend
regression run passes **495 tests**, with six optional PostgreSQL/Redis integration
checks skipped in this run. Ruff lint/formatting and frontend TypeScript checks
pass. The production dashboard browser suite separately passes all ten checks.

Tests pin exact encoding bytes and an independent three-leaf hash construction,
exercise every leaf of 1-, 2-, 3-, 5-, 8-, 9-, 17-, and 32-leaf trees, and reject
modified paths, counts, contexts, payloads, and roots. A real retained 25-round
synthetic spike/quarantine/recovery pipeline exports and reproduces offline across
reopened database connections and changed caller decimal precision. CLI checks
cover exports, offline verification, proof generation, independent root checking,
and refusal to overwrite existing artifacts.

Offline signing and local development key rotation are implemented separately.
Production key custody, cross-language implementations, public contract deployment,
hosting and backup guarantees, and testnet deployment remain separate release gates.
