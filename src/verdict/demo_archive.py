"""Repeatable local execution and independently pinned, offline demo archive verification."""

import ast
import hashlib
import json
from fractions import Fraction
from pathlib import Path, PurePosixPath

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes
from pydantic import TypeAdapter

from verdict.contract_artifacts import load_artifact, runtime_matches
from verdict.demo_replay import CHECKPOINTS, DEFAULT_START, retain_replay, session_spec, summarize
from verdict.dependencies import Sha256, checksum
from verdict.evidence import (
    EvidenceBundle,
    InclusionProof,
    inclusion_proof,
    verify_inclusion,
)
from verdict.signing import (
    ReportDomain,
    ReportPolicy,
    SignedDiagnosticReport,
    SignerAuthorization,
    SignerRegistry,
    sign_report,
    verify_report,
)
from verdict.signing_keys import write_new_json
from verdict.testnet import (
    NetworkProfile,
    TransactionPlan,
    deployment_plan,
    initialization_plan,
    publication_plan,
    validate_plan,
)
from verdict.testnet_rpc import (
    audit_receipt,
    call,
    matching_event,
    matching_transaction,
    preflight,
    quantity,
)


def file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(65536), b""):
            result.update(part)
    return result.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def phase_checks(rows):
    require(
        [row["phase"] for row in rows] == [name for _, name in CHECKPOINTS],
        "demo phase order mismatch",
    )
    for row in rows:
        require(
            (row["status"], row["value"], row["confidence"], row["publication_ready"])
            == ("unavailable", None, None, False),
            "demo promoted diagnostic output",
        )
    baseline, attack, recovered_attack, outage, recovered = rows
    require(baseline["sources"]["coinbase"]["weight_bps"] > 0, "baseline is not mature")
    require(
        attack["sources"]["coinbase"]
        == {
            "state": "quarantined",
            "weight_bps": 0,
            "availability": "fresh",
            "comparison": "outlier",
        },
        "manipulation was not quarantined",
    )
    require(
        recovered_attack["sources"]["coinbase"]["weight_bps"] > 0, "manipulation recovery missing"
    )
    require(
        abs(Fraction(attack["candidate_value"]) / Fraction(baseline["candidate_value"]) - 1) * 10000
        <= 5,
        "isolated manipulation exceeded the diagnostic candidate shift target",
    )
    require(
        outage["candidate_value"] is None and outage["total_weight_bps"] == 0,
        "outage did not remove the candidate",
    )
    require(
        all(
            row["availability"] == "stale" and row["weight_bps"] == 0
            for row in outage["sources"].values()
        ),
        "outage freshness was bypassed",
    )
    require(
        all(
            row["state"] == "active" and row["weight_bps"] > 0
            for row in recovered["sources"].values()
        ),
        "provider recovery missing",
    )


def run_local_demo(directory, source_profile, *, start=None):
    from verdict.demo_evm import LocalEVM

    source_profile = NetworkProfile.model_validate(source_profile)
    start = int(DEFAULT_START.timestamp()) if start is None else start
    # Validate before creating an output directory or an EVM.
    session_spec(source_profile, start)
    evm = LocalEVM(timestamp=start + 30)
    from eth_tester.exceptions import TransactionFailed

    profile = NetworkProfile.model_validate(
        {
            **source_profile.model_dump(mode="json"),
            "genesis_hash": evm.genesis_hash,
        }
    )
    directory = Path(directory)
    bundles = retain_replay(
        directory, profile, start=start, until=510, checkpoints=[at for at, _ in CHECKPOINTS]
    )

    def save(name, value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", by_alias=True)
        write_new_json(directory / name, value)

    save("source-profile.json", source_profile)
    save("profile.json", profile)
    transactions = []

    def execute(prefix, plan):
        checked = preflight(plan, profile, evm, expected_plan_id=plan.plan_id, now=evm.now)
        tx_hash = evm.send_local(plan, profile)
        evm.confirm()
        audit = audit_receipt(
            plan, profile, evm, expected_plan_id=plan.plan_id, transaction_hash=tx_hash, now=evm.now
        )
        require(
            audit["status"] == "confirmed" and audit["verified"], "local transaction failed audit"
        )
        save(f"{prefix}/plan.json", plan)
        save(f"{prefix}/preflight.json", checked)
        save(f"{prefix}/audit.json", audit)
        save(f"{prefix}/transaction.json", evm.request("eth_getTransactionByHash", [tx_hash]))
        save(f"{prefix}/raw-transaction.json", {"hex": evm.raw_transaction(tx_hash)})
        save(f"{prefix}/receipt.json", evm.request("eth_getTransactionReceipt", [tx_hash]))
        transactions.append(
            {"prefix": prefix, "plan_id": plan.plan_id, "transaction_hash": tx_hash}
        )
        return tx_hash

    oracle_plan = deployment_plan(
        profile,
        operation="deploy-oracle",
        sender=evm.deployer,
        administrator=evm.administrator,
        nonce=0,
        now=evm.now,
    )
    execute("deploy-oracle", oracle_plan)
    oracle = oracle_plan.expected_contract_address
    policy = ReportPolicy(
        version=profile.version,
        dataset=profile.dataset,
        domain=ReportDomain(chain_id=profile.chain_id, verifying_contract=oracle),
        maximum_ttl_seconds=profile.maximum_ttl_seconds,
        maximum_observation_age_seconds=profile.maximum_observation_age_seconds,
    )
    # Public development fixture only. Never accepted from a user or used by public RPC tools.
    signer = Account.from_key(bytes.fromhex("77" * 32))
    registry = SignerRegistry(
        domain=policy.domain,
        dataset=profile.dataset,
        policy_id=policy.policy_id,
        revision=1,
        keys=(
            SignerAuthorization(
                key_id="local-demo-v1",
                signer=signer.address,
                valid_from=start,
                valid_until=start + 3600,
            ),
        ),
    )
    save("policy.json", policy)
    save("registry.json", registry)
    execute(
        "initialize",
        initialization_plan(
            profile,
            sender=evm.administrator,
            nonce=0,
            oracle=oracle,
            bundle=bundles[90],
            policy=policy,
            registry=registry,
            expected_registry_id=registry.registry_id,
            now=evm.now,
        ),
    )
    consumer_plan = deployment_plan(
        profile,
        operation="deploy-consumer",
        sender=evm.deployer,
        nonce=1,
        oracle=oracle,
        now=evm.now,
    )
    execute("deploy-consumer", consumer_plan)
    consumer = consumer_plan.expected_contract_address
    oracle_artifact = load_artifact("DiagnosticOracle", profile.oracle_artifact_id)
    consumer_artifact = load_artifact("FailClosedConsumer", profile.consumer_artifact_id)
    save("oracle-runtime.json", {"code": evm.request("eth_getCode", [oracle, "latest"])})
    save("consumer-runtime.json", {"code": evm.request("eth_getCode", [consumer, "latest"])})

    def rejects(artifact, target, name, args, error):
        try:
            call(evm, artifact, target, name, args, "latest")
        except TransactionFailed as caught:
            raw = ast.literal_eval(str(caught))
            require(raw[:4] == keccak(text=error)[:4], "unexpected local contract rejection")
        else:
            raise ValueError("local contract unexpectedly accepted an adversarial call")

    stages = []
    for nonce, (at, phase) in enumerate(CHECKPOINTS):
        bundle = bundles[at]
        prefix = f"rounds/{phase}"
        evm.advance(start + at + 1)
        signed = sign_report(
            bundle,
            policy=policy,
            registry=registry,
            key_id="local-demo-v1",
            account=signer,
            issued_at=evm.now,
        )
        plan = publication_plan(
            profile,
            sender=evm.relayer,
            nonce=nonce,
            oracle=oracle,
            signed=signed,
            bundle=bundle,
            policy=policy,
            registry=registry,
            expected_registry_id=registry.registry_id,
            now=evm.now,
        )
        tx_hash = execute(prefix, plan)
        save(f"{prefix}/evidence.json", bundle)
        save(f"{prefix}/signed-report.json", signed)
        index = next(i for i, leaf in enumerate(bundle.leaves) if leaf.kind == "assessment")
        save(f"{prefix}/assessment-proof.json", inclusion_proof(bundle, index))
        report, digest, latest_signer, fresh = call(
            evm, oracle_artifact, oracle, "latestDiagnostic", {}, "latest"
        )
        require(
            report[4].hex() == bundle.root
            and report[8] == start + at
            and digest.hex() == signed.digest
            and latest_signer == signer.address.lower()
            and fresh,
            "latest on-chain diagnostic lost its evidence linkage",
        )
        require(report[12:15] == (0, 0, 0) and report[16] is False, "on-chain output became usable")
        require(
            call(evm, consumer_artifact, consumer, "canAct", {}, "latest") == (False,),
            "consumer accepted an unavailable diagnostic",
        )
        rejects(consumer_artifact, consumer, "readPrice", {}, "FeedUnavailable()")
        stages.append(
            {
                "phase": phase,
                "seconds": at,
                **summarize(bundle),
                "prefix": prefix,
                "signed_digest": signed.digest,
                "transaction_hash": tx_hash,
                "consumer_can_act": False,
                "diagnostic_fresh_at_receipt": fresh,
            }
        )
    phase_checks(stages)
    latest_args = plan.arguments
    rejects(oracle_artifact, oracle, "submit", latest_args, "ReplayedReport()")
    altered = {**latest_args, "report": {**latest_args["report"], "evidenceRoot": "0x" + "aa" * 32}}
    rejects(oracle_artifact, oracle, "submit", altered, "UnauthorizedSigner()")
    evm.advance(signed.message.valid_until)
    rejects(oracle_artifact, oracle, "submit", latest_args, "ReportExpired()")
    require(
        call(evm, oracle_artifact, oracle, "latestDiagnostic", {}, "latest")[-1] is False,
        "diagnostic stayed fresh after expiry",
    )
    save(
        "checks.json",
        {
            "duplicate_rejected": True,
            "changed_root_signature_rejected": True,
            "expired_report_rejected": True,
            "expired_latest_not_fresh": True,
            "unavailable_consumer_rejected_every_round": True,
        },
    )
    report = [
        "# Local manipulation, outage, and recovery demonstration",
        "",
        "Synthetic replay executed on an isolated Py-EVM chain using the Sepolia chain ID",
        "and a substituted local genesis. No public deployment or consensus finality is claimed.",
        "",
        "| Seconds | Phase | Diagnostic candidate | Allocated weight (bps) | Consumer can act |",
        "| --- | --- | --- | --- | --- |",
        *[
            f"| {row['seconds']} | [{row['phase']}]({row['prefix']}/evidence.json) | "
            f"{row['candidate_value'] or 'unavailable'} | {row['total_weight_bps']} | false |"
            for row in stages
        ],
        "",
        "All signed usable values/confidence remain unavailable/null. "
        "Candidates above are diagnostic only.",
        "The shortened policies and synthetic dependency graph are test assumptions, "
        "not live calibration.",
        "",
        "Eight transactions passed preflight and receipt audits. "
        "Duplicate, changed-root, and expired",
        "submissions were rejected, and usable consumer reads reverted at every checkpoint.",
        "",
        "`archive.json` inventories the database, complete evidence bundles, "
        "assessment proofs, signed",
        "reports, unsigned plans, signed raw transactions, preflights, "
        "receipts, and audit records.",
        "Retain its independently printed archive ID for `testnet demo-verify`.",
        "",
    ]
    with (directory / "report.md").open("x", encoding="utf-8") as output:
        output.write("\n".join(report))
    body = {
        "schema_version": 1,
        "kind": "verdict-local-e2e-v1",
        "mode": "local-pyevm",
        "synthetic": True,
        "public_deployment": False,
        "start": start,
        "source_profile_id": source_profile.profile_id,
        "profile_id": profile.profile_id,
        "dataset": profile.dataset,
        "oracle": oracle,
        "consumer": consumer,
        "policy_id": policy.policy_id,
        "registry_id": registry.registry_id,
        "transactions": transactions,
        "stages": stages,
        "files": {
            path.relative_to(directory).as_posix(): file_hash(path)
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        },
    }
    manifest = {**body, "archive_id": checksum(body)}
    save("archive.json", manifest)
    return manifest


def verify_archive(directory, *, expected_archive_id):
    """Reproduce evidence/signatures and verify archive linkage; not a chain receipt proof."""
    directory = Path(directory).resolve()
    expected_archive_id = TypeAdapter(Sha256).validate_python(expected_archive_id)

    def load(name):
        path = directory / name
        require(path.resolve().is_relative_to(directory), "demo file escapes archive")
        require(path.stat().st_size <= 32_000_000, "oversized demo artifact")
        return json.loads(path.read_text(encoding="utf-8"))

    manifest = load("archive.json")
    require(
        manifest.get("archive_id")
        == expected_archive_id
        == checksum({k: v for k, v in manifest.items() if k != "archive_id"}),
        "demo archive ID mismatch",
    )
    require(
        (
            manifest["schema_version"],
            manifest["kind"],
            manifest["mode"],
            manifest["synthetic"],
            manifest["public_deployment"],
        )
        == (1, "verdict-local-e2e-v1", "local-pyevm", True, False),
        "archive is not a local synthetic demo",
    )
    files = manifest["files"]
    require(isinstance(files, dict) and 1 <= len(files) <= 128, "invalid demo file inventory")
    for name, digest in files.items():
        path = directory / name
        require(
            isinstance(name, str)
            and "\\" not in name
            and ":" not in name
            and not PurePosixPath(name).is_absolute()
            and ".." not in PurePosixPath(name).parts
            and path.resolve().is_relative_to(directory)
            and path.is_file(),
            "demo file escapes archive",
        )
        require(
            path.stat().st_size <= 32_000_000 and file_hash(path) == digest,
            "demo artifact checksum mismatch",
        )

    def artifact(name):
        require(name in files, "required demo artifact is not inventoried")
        return load(name)

    profile = NetworkProfile.model_validate(artifact("profile.json"))
    source = NetworkProfile.model_validate(artifact("source-profile.json"))
    require(
        profile.profile_id == manifest["profile_id"]
        and source.profile_id == manifest["source_profile_id"]
        and profile.genesis_hash != source.genesis_hash
        and profile.model_dump(exclude={"genesis_hash"})
        == source.model_dump(exclude={"genesis_hash"}),
        "local profile changed more than the genesis substitution",
    )
    require(
        artifact("session.json") == session_spec(profile, manifest["start"]),
        "demo session mismatch",
    )
    policy = ReportPolicy.model_validate(artifact("policy.json"))
    registry = SignerRegistry.model_validate(artifact("registry.json"))
    require(
        policy.policy_id == manifest["policy_id"]
        and registry.registry_id == manifest["registry_id"],
        "demo policy/registry mismatch",
    )
    for name, identity, file in (
        ("DiagnosticOracle", profile.oracle_artifact_id, "oracle-runtime.json"),
        ("FailClosedConsumer", profile.consumer_artifact_id, "consumer-runtime.json"),
    ):
        require(
            runtime_matches(load_artifact(name, identity), artifact(file)["code"]),
            "demo runtime mismatch",
        )
    transactions = manifest["transactions"]
    require(
        [row["prefix"] for row in transactions]
        == [
            "deploy-oracle",
            "initialize",
            "deploy-consumer",
            *[f"rounds/{name}" for _, name in CHECKPOINTS],
        ],
        "demo transaction sequence mismatch",
    )
    receipts, plans = {}, {}
    for item in transactions:
        prefix = item["prefix"]
        plan = validate_plan(
            TransactionPlan.model_validate(artifact(f"{prefix}/plan.json")),
            profile,
            expected_plan_id=item["plan_id"],
        )
        tx_hash = item["transaction_hash"]
        actual = artifact(f"{prefix}/transaction.json")
        matching_transaction(actual, plan, tx_hash)
        raw = HexBytes(artifact(f"{prefix}/raw-transaction.json")["hex"])
        require(
            "0x" + keccak(raw).hex() == tx_hash
            and Account.recover_transaction(raw) == plan.transaction.sender,
            "signed transaction hash/sender mismatch",
        )
        decoded = TypedTransaction.from_bytes(raw).as_dict()
        expected = plan.transaction.model_dump(mode="json", by_alias=True, exclude_none=True)
        expected.pop("from")
        expected["to"] = bytes.fromhex(expected.get("to", "0x")[2:])
        expected["data"] = bytes.fromhex(expected["data"][2:])
        require(
            {k: v for k, v in decoded.items() if k not in {"v", "r", "s", "accessList"}} == expected
            and not decoded["accessList"],
            "serialized transaction differs from plan",
        )
        receipt = artifact(f"{prefix}/receipt.json")
        checked, audit = artifact(f"{prefix}/preflight.json"), artifact(f"{prefix}/audit.json")
        for record in (checked, audit):
            require(
                record["record_id"]
                == checksum({k: v for k, v in record.items() if k != "record_id"})
                and record["plan_id"] == plan.plan_id
                and record["profile_id"] == profile.profile_id,
                "demo RPC record mismatch",
            )
        validate_plan(plan, profile, now=checked["checked_at"])
        require(
            checked["kind"] == "preflight"
            and checked["ready"] is True
            and checked["broadcast"] is False
            and audit["kind"] == "receipt"
            and audit["status"] == "confirmed"
            and audit["verified"] is True
            and audit["transaction_hash"] == receipt["transactionHash"] == tx_hash
            and audit["block"]["hash"] == receipt["blockHash"] == actual["blockHash"]
            and quantity(receipt["status"]) == 1
            and audit["confirmations"] >= profile.confirmation_blocks,
            "demo receipt/preflight linkage mismatch",
        )
        if plan.operation.startswith("deploy-"):
            require(
                receipt["contractAddress"] == plan.expected_contract_address,
                "demo deployment address mismatch",
            )
        elif plan.operation == "initialize":
            matching_event(
                receipt,
                plan.oracle,
                "RegistryApplied(bytes32,bytes32,uint64)",
                ["0x" + "00" * 32, "0x" + registry.registry_id],
                ["uint64"],
                (1,),
            )
        receipts[prefix], plans[prefix] = receipt, plan
    require(
        plans["deploy-oracle"].expected_contract_address
        == manifest["oracle"]
        == policy.domain.verifying_contract
        and plans["deploy-consumer"].expected_contract_address == manifest["consumer"]
        and plans["deploy-consumer"].oracle == manifest["oracle"]
        and plans["initialize"].oracle == manifest["oracle"]
        and plans["initialize"].administrator == plans["deploy-oracle"].administrator,
        "demo contract/domain mismatch",
    )
    reconstructed = []
    for at, name in CHECKPOINTS:
        prefix = f"rounds/{name}"
        bundle = EvidenceBundle.model_validate(artifact(f"{prefix}/evidence.json"))
        if at == 90:
            initial = plans["initialize"]
            reconstructed_initial = initialization_plan(
                profile,
                sender=initial.transaction.sender,
                nonce=initial.transaction.nonce,
                oracle=manifest["oracle"],
                bundle=bundle,
                policy=policy,
                registry=registry,
                expected_registry_id=registry.registry_id,
                now=initial.created_at,
                gas=initial.transaction.gas,
                max_fee=initial.transaction.maxFeePerGas,
                priority_fee=initial.transaction.maxPriorityFeePerGas,
            )
            require(
                reconstructed_initial == initial,
                "initial configuration does not match baseline evidence",
            )
        signed = verify_report(
            SignedDiagnosticReport.model_validate(artifact(f"{prefix}/signed-report.json")),
            bundle=bundle,
            policy=policy,
            registry=registry,
            expected_registry_id=registry.registry_id,
            now=artifact(f"{prefix}/audit.json")["block"]["timestamp"],
        )
        proof = InclusionProof.model_validate(artifact(f"{prefix}/assessment-proof.json"))
        require(
            verify_inclusion(proof, expected_root=bundle.root)
            and proof.leaf.kind == "assessment"
            and proof.leaf.record_id == bundle.assessment_id,
            "assessment proof mismatch",
        )
        plan = publication_plan(
            profile,
            sender=plans[prefix].transaction.sender,
            nonce=plans[prefix].transaction.nonce,
            oracle=manifest["oracle"],
            signed=signed,
            bundle=bundle,
            policy=policy,
            registry=registry,
            expected_registry_id=registry.registry_id,
            now=plans[prefix].created_at,
            gas=plans[prefix].transaction.gas,
            max_fee=plans[prefix].transaction.maxFeePerGas,
            priority_fee=plans[prefix].transaction.maxPriorityFeePerGas,
        )
        require(
            plan == plans[prefix] and signed.message.round_id == manifest["start"] + at,
            "signed publication plan mismatch",
        )
        matching_event(
            receipts[prefix],
            manifest["oracle"],
            "DiagnosticAccepted(bytes32,bytes32,bytes32,uint64,address,uint64)",
            ["0x" + bundle.assessment_id, "0x" + bundle.root, "0x" + signed.digest],
            ["uint64", "address", "uint64"],
            (signed.message.round_id, signed.signer.lower(), signed.message.valid_until),
        )
        reconstructed.append(
            {
                "phase": name,
                "seconds": at,
                **summarize(bundle),
                "prefix": prefix,
                "signed_digest": signed.digest,
                "transaction_hash": receipts[prefix]["transactionHash"],
                "consumer_can_act": False,
                "diagnostic_fresh_at_receipt": True,
            }
        )
    require(
        reconstructed == manifest["stages"], "demo phase summary differs from retained evidence"
    )
    phase_checks(reconstructed)
    checks = artifact("checks.json")
    require(
        set(checks)
        == {
            "duplicate_rejected",
            "changed_root_signature_rejected",
            "expired_report_rejected",
            "expired_latest_not_fresh",
            "unavailable_consumer_rejected_every_round",
        }
        and all(value is True for value in checks.values()),
        "local rejection checks incomplete",
    )
    return {
        "verified": True,
        "archive_id": expected_archive_id,
        "mode": "local-pyevm",
        "public_deployment": False,
        "synthetic": True,
        "phases": len(reconstructed),
        "transactions": len(transactions),
        "publication_ready": False,
        "verification_scope": "evidence-signatures-transactions-and-archive-linkage",
        "evm_reexecuted": False,
        "receipt_proof_verified": False,
    }
