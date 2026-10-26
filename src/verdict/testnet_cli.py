"""Unsigned deployment preparation and read-only public-network verification."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from verdict.contract_artifacts import ROOT
from verdict.signing_keys import write_new_json
from verdict.testnet import (
    NetworkProfile,
    TransactionPlan,
    deployment_plan,
    initialization_plan,
    publication_plan,
)
from verdict.testnet_rpc import CheckFailed, ReadRPC, audit_receipt, preflight

DEFAULT_PROFILE = ROOT / "networks" / "sepolia-v1.json"


def add_commands(commands):
    root = commands.add_parser("testnet", help="Unsigned Sepolia plans and read-only RPC checks")
    actions = root.add_subparsers(dest="testnet_action", required=True)
    profile = actions.add_parser("profile", help="Inspect the pinned public network profile")
    deploy = actions.add_parser("plan-deploy", help="Prepare unsigned constructor calldata")
    deploy.add_argument("--operation", choices=["deploy-oracle", "deploy-consumer"], required=True)
    deploy.add_argument("--administrator", help="Oracle owner, separate from the report signer")
    deploy.add_argument("--oracle", help="Verified oracle address for the consumer")
    initialize = actions.add_parser(
        "plan-initialize", help="Verify evidence and prepare initialization"
    )
    submit = actions.add_parser(
        "plan-submit", help="Verify a signed diagnostic and prepare submission"
    )
    for item in (initialize, submit):
        for field in ("bundle", "policy", "registry"):
            item.add_argument(f"--{field}", type=Path, required=True)
        item.add_argument(
            "--registry-id", required=True, help="Independently trusted registry hash"
        )
        item.add_argument("--oracle", required=True)
    submit.add_argument("--report", type=Path, required=True)
    for item in (deploy, initialize, submit):
        item.add_argument("--sender", required=True)
        item.add_argument("--nonce", type=int, required=True)
        item.add_argument("--gas", type=int)
        item.add_argument("--max-fee-wei", type=int)
        item.add_argument("--priority-fee-wei", type=int)
    check = actions.add_parser("preflight", help="Simulate a reviewed plan using read-only RPC")
    receipt = actions.add_parser(
        "receipt", help="Audit a wallet transaction against a reviewed plan"
    )
    receipt.add_argument("--transaction-hash", required=True)
    for item in (check, receipt):
        item.add_argument("--plan", type=Path, required=True)
        item.add_argument("--plan-id", required=True, help="Independently reviewed plan checksum")
    for item in (profile, deploy, initialize, submit, check, receipt):
        item.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
        if item is not profile:
            item.add_argument("--output", type=Path, required=True)


def run(args, parser):
    from verdict.evidence import EvidenceBundle
    from verdict.signing import ReportPolicy, SignedDiagnosticReport, SignerRegistry

    def load(model, path):
        if path.stat().st_size > 32_000_000:
            raise ValueError("input artifact exceeds the size limit")
        return model.model_validate_json(path.read_text(encoding="utf-8"))

    try:
        profile = load(NetworkProfile, args.profile)
        if args.testnet_action == "profile":
            print(
                json.dumps(
                    {"profile_id": profile.profile_id, **profile.model_dump(mode="json")}, indent=2
                )
            )
            return
        if args.output.exists():
            raise ValueError("output already exists; review artifacts cannot be overwritten")
        now = int(datetime.now(UTC).timestamp())
        action = args.testnet_action
        if action.startswith("plan-"):
            common = dict(
                sender=args.sender,
                nonce=args.nonce,
                now=now,
                gas=args.gas,
                max_fee=args.max_fee_wei,
                priority_fee=args.priority_fee_wei,
            )
            if action == "plan-deploy":
                if args.operation == "deploy-oracle" and (not args.administrator or args.oracle):
                    raise ValueError("oracle deployment requires --administrator only")
                if args.operation == "deploy-consumer" and (not args.oracle or args.administrator):
                    raise ValueError("consumer deployment requires --oracle only")
                result = deployment_plan(
                    profile,
                    operation=args.operation,
                    administrator=args.administrator,
                    oracle=args.oracle,
                    **common,
                )
            else:
                common.update(
                    oracle=args.oracle,
                    bundle=load(EvidenceBundle, args.bundle),
                    policy=load(ReportPolicy, args.policy),
                    registry=load(SignerRegistry, args.registry),
                    expected_registry_id=args.registry_id,
                )
                result = (
                    initialization_plan(profile, **common)
                    if action == "plan-initialize"
                    else publication_plan(
                        profile, signed=load(SignedDiagnosticReport, args.report), **common
                    )
                )
            payload = result.model_dump(mode="json", by_alias=True)
            summary = {
                "plan_id": result.plan_id,
                "operation": result.operation,
                "expires_at": result.expires_at,
                "broadcast": False,
            }
        else:
            plan = load(TransactionPlan, args.plan)
            url = os.environ.get(profile.rpc_env)
            if not url:
                raise ValueError(f"RPC environment variable is missing: {profile.rpc_env}")
            with httpx.Client(timeout=20, trust_env=False) as client:
                rpc = ReadRPC(client, url)
                payload = (
                    preflight(plan, profile, rpc, expected_plan_id=args.plan_id, now=now)
                    if action == "preflight"
                    else audit_receipt(
                        plan,
                        profile,
                        rpc,
                        expected_plan_id=args.plan_id,
                        transaction_hash=args.transaction_hash,
                        now=now,
                    )
                )
            summary = {key: payload[key] for key in ("record_id", "plan_id", "kind")}
            summary.update({key: payload[key] for key in ("ready", "status") if key in payload})
        write_new_json(args.output, payload)
        print(json.dumps(summary, sort_keys=True))
        if action == "receipt" and payload["status"] not in {"confirmed", "finalized"}:
            parser.exit(
                1,
                "Receipt is not a confirmed successful transaction; inspect the retained audit.\n",
            )
    except CheckFailed as error:
        parser.error(str(error))
    except (ValueError, OSError, KeyError, TypeError, StopIteration):
        # Model validation can echo artifact values. Keep CLI errors free of file/RPC secrets.
        parser.error(
            "testnet preparation/check failed; inspect input paths, reviewed IDs, "
            "network, and transaction state"
        )
