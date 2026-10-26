"""File-based signing commands, deliberately independent of the database and network."""

import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, TypeAdapter


def add_commands(commands):
    parser = commands.add_parser(
        "signing", help="Offline diagnostic signatures and signer rotation"
    )
    actions = parser.add_subparsers(dest="signing_action", required=True)
    policy = actions.add_parser("policy", help="Create a public signing policy")
    policy.add_argument("--version", required=True)
    policy.add_argument("--dataset", required=True)
    policy.add_argument("--chain-id", type=int, required=True)
    policy.add_argument("--verifying-contract", required=True)
    policy.add_argument("--maximum-ttl", type=int, default=30)
    policy.add_argument("--maximum-age", type=int, default=30)
    key = actions.add_parser("key-create", help="Create a new encrypted development keystore")
    key.add_argument(
        "--password-env", help="Environment variable name; never the passphrase itself"
    )
    initial = actions.add_parser("registry-init", help="Create the first public signer registry")
    initial.add_argument("--policy", type=Path, required=True)
    initial.add_argument("--key-id", required=True)
    initial.add_argument("--signer", required=True)
    initial.add_argument("--valid-from", required=True)
    initial.add_argument("--valid-until", required=True)
    rotate = actions.add_parser(
        "registry-rotate", help="Create a successor with bounded key overlap"
    )
    rotate.add_argument("--registry", type=Path, required=True)
    rotate.add_argument("--old-key-id", required=True)
    rotate.add_argument("--new-key-id", required=True)
    rotate.add_argument("--new-signer", required=True)
    rotate.add_argument("--activate-at", required=True)
    rotate.add_argument("--retire-at", required=True)
    rotate.add_argument("--valid-until", required=True)
    revoke = actions.add_parser("registry-revoke", help="Revoke a signer in a successor registry")
    revoke.add_argument("--registry", type=Path, required=True)
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--at", help="UTC revocation time; defaults to now")
    sign = actions.add_parser("sign", help="Sign a verified evidence bundle")
    verify = actions.add_parser(
        "verify", help="Verify evidence, signature, authorization and expiry"
    )
    contract = actions.add_parser("contract-input", help="Export verified oracle ABI arguments")
    for command in (sign, verify, contract):
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--policy", type=Path, required=True)
        command.add_argument("--registry", type=Path, required=True)
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--keystore", type=Path, required=True)
    sign.add_argument(
        "--password-env", help="Environment variable name; never the passphrase itself"
    )
    sign.add_argument("--issued-at", help="Historical clock override for replay datasets only")
    for command in (verify, contract):
        command.add_argument("--report", type=Path, required=True)
        command.add_argument(
            "--registry-id", required=True, help="Independently trusted CURRENT registry hash"
        )
        command.add_argument("--at", help="Verification time; defaults to now")
    typed = actions.add_parser("typed-data", help="Export the EIP-712 representation for tooling")
    typed.add_argument("--report", type=Path, required=True)
    for command in (policy, key, initial, rotate, revoke, sign, typed, contract):
        command.add_argument("--output", type=Path, required=True)


def run(args, parser):
    from verdict.evidence import EvidenceBundle
    from verdict.signing import (
        ReportDomain,
        ReportPolicy,
        SignedDiagnosticReport,
        SignerAuthorization,
        SignerRegistry,
        revoke_registry,
        rotate_registry,
        sign_report,
        typed_data,
        unix_seconds,
        verify_report,
    )
    from verdict.signing_keys import create_keystore, unlock_keystore, write_new_json

    def load(model, path):
        return model.model_validate_json(path.read_text(encoding="utf-8"))

    def clock(value):
        value = (
            TypeAdapter(AwareDatetime).validate_python(value)
            if value
            else datetime.now(UTC).replace(microsecond=0)
        )
        return unix_seconds(value)

    def password(*, confirm=False):
        if args.password_env:
            value = os.environ.get(args.password_env)
            if not value:
                raise ValueError("passphrase environment variable is missing or empty")
            return value
        if not sys.stdin.isatty():
            raise ValueError("a terminal prompt or --password-env is required for the passphrase")
        value = getpass.getpass("Keystore passphrase: ")
        if confirm and value != getpass.getpass("Confirm passphrase: "):
            raise ValueError("passphrases do not match")
        return value

    try:
        action = args.signing_action
        if hasattr(args, "output") and args.output.exists():
            raise ValueError("output file already exists; artifacts cannot be overwritten")
        if action == "policy":
            result = ReportPolicy(
                version=args.version,
                dataset=args.dataset,
                domain=ReportDomain(
                    chain_id=args.chain_id, verifying_contract=args.verifying_contract
                ),
                maximum_ttl_seconds=args.maximum_ttl,
                maximum_observation_age_seconds=args.maximum_age,
            )
            summary = {"policy_id": result.policy_id}
        elif action == "key-create":
            result, signer = create_keystore(password(confirm=True))
            summary = {"signer": signer, "encrypted": True}
        elif action == "registry-init":
            policy = load(ReportPolicy, args.policy)
            result = SignerRegistry(
                domain=policy.domain,
                dataset=policy.dataset,
                policy_id=policy.policy_id,
                revision=1,
                keys=(
                    SignerAuthorization(
                        key_id=args.key_id,
                        signer=args.signer,
                        valid_from=clock(args.valid_from),
                        valid_until=clock(args.valid_until),
                    ),
                ),
            )
            summary = {"registry_id": result.registry_id, "revision": result.revision}
        elif action in ("registry-rotate", "registry-revoke"):
            previous = load(SignerRegistry, args.registry)
            if action == "registry-rotate":
                result = rotate_registry(
                    previous,
                    old_key_id=args.old_key_id,
                    new_key_id=args.new_key_id,
                    new_signer=args.new_signer,
                    activate_at=clock(args.activate_at),
                    retire_at=clock(args.retire_at),
                    valid_until=clock(args.valid_until),
                )
            else:
                result = revoke_registry(previous, key_id=args.key_id, at=clock(args.at))
            summary = {
                "registry_id": result.registry_id,
                "revision": result.revision,
                "previous_registry_id": previous.registry_id,
            }
        elif action == "typed-data":
            report = load(SignedDiagnosticReport, args.report)
            result = typed_data(report.domain, report.message)
            summary = {"output": str(args.output)}
        else:
            bundle, policy, registry = (
                load(EvidenceBundle, args.bundle),
                load(ReportPolicy, args.policy),
                load(SignerRegistry, args.registry),
            )
            if action == "sign":
                if args.issued_at and bundle.dataset == "live":
                    raise ValueError("explicit issuance time is only supported for replay datasets")
                account = unlock_keystore(args.keystore, password())
                result = sign_report(
                    bundle,
                    policy=policy,
                    registry=registry,
                    key_id=args.key_id,
                    account=account,
                    issued_at=clock(args.issued_at),
                )
                summary = {
                    "digest": result.digest,
                    "signer": result.signer,
                    "status": result.message.status,
                    "publication_ready": False,
                }
            else:
                report = load(SignedDiagnosticReport, args.report)
                at = clock(args.at)
                if action == "contract-input":
                    from verdict.contract_inputs import contract_inputs

                    result = contract_inputs(
                        report,
                        bundle=bundle,
                        policy=policy,
                        registry=registry,
                        expected_registry_id=args.registry_id,
                        now=at,
                    )
                    write_new_json(args.output, result)
                    print(
                        json.dumps(
                            {
                                "output": str(args.output),
                                "digest": result["digest"],
                                "publication_ready": False,
                            }
                        )
                    )
                    return
                verified = verify_report(
                    report,
                    bundle=bundle,
                    policy=policy,
                    registry=registry,
                    expected_registry_id=args.registry_id,
                    now=at,
                )
                print(
                    json.dumps(
                        {
                            "verified": True,
                            "verified_at": at,
                            "digest": verified.digest,
                            "registry_id": registry.registry_id,
                            "signer": verified.signer,
                            "status": verified.message.status,
                            "value": None,
                            "confidence": None,
                            "publication_ready": False,
                        }
                    )
                )
                return
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        write_new_json(args.output, payload)
        print(json.dumps(summary))
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
