"""A continuous full-pipeline/EVM demonstration and adversarial offline archives."""

import builtins
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from verdict.demo_archive import run_local_demo, verify_archive
from verdict.demo_replay import DEFAULT_START, export_evidence, observations, session_spec
from verdict.dependencies import checksum
from verdict.evidence import EvidenceBundle, verify_bundle
from verdict.models import Observation
from verdict.testnet import NetworkProfile
from verdict.testnet_cli import DEFAULT_PROFILE

START = int(DEFAULT_START.timestamp())


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def repin(directory):
    from verdict.demo_archive import file_hash

    data = load(directory / "archive.json")
    data["files"] = {name: file_hash(directory / name) for name in data["files"]}
    data["archive_id"] = checksum({k: v for k, v in data.items() if k != "archive_id"})
    save(directory / "archive.json", data)
    return data["archive_id"]


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    directory = tmp_path_factory.mktemp("local-e2e") / "archive"
    profile = NetworkProfile.model_validate_json(DEFAULT_PROFILE.read_text(encoding="utf-8"))

    def forbidden(*args, **kwargs):
        raise AssertionError("local demonstration accessed global settings or an HTTP transport")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("VERDICT_DATABASE_URL", "must-not-use-this-global-database")
        patch.setenv("VERDICT_SEPOLIA_RPC_URL", "https://must-not-access.example/secret")
        patch.setattr("verdict.config.get_settings", forbidden)
        patch.setattr(httpx.Client, "send", forbidden)
        patch.setattr(httpx.AsyncClient, "send", forbidden)
        manifest = run_local_demo(directory, profile)
    return directory, manifest


def test_continuous_demo_executes_manipulation_outage_recovery(archive):
    directory, manifest = archive
    assert manifest["mode"] == "local-pyevm"
    assert manifest["synthetic"] and not manifest["public_deployment"]
    assert len(manifest["transactions"]) == 8
    baseline, attack, recovered_attack, outage, recovered = manifest["stages"]
    assert baseline["candidate_value"] == recovered["candidate_value"] == "100"
    assert attack["sources"]["coinbase"]["state"] == "quarantined"
    assert attack["sources"]["coinbase"]["weight_bps"] == 0
    assert recovered_attack["sources"]["coinbase"]["state"] == "active"
    assert outage["candidate_value"] is None and outage["total_weight_bps"] == 0
    assert all(row["weight_bps"] > 0 for row in recovered["sources"].values())
    assert all(
        row["status"] == "unavailable"
        and row["value"] is None
        and row["confidence"] is None
        and not row["consumer_can_act"]
        for row in manifest["stages"]
    )
    assert all(load(directory / "checks.json").values())
    assert (directory / "replay.db").is_file()
    assert (directory / "report.md").is_file()
    with sqlite3.connect(
        f"file:{(directory / 'replay.db').as_posix()}?mode=ro", uri=True
    ) as database:
        assert database.execute("select count(*) from observations").fetchone()[0] == 258
        assert database.execute("select count(*) from confidence_rounds").fetchone()[0] == 46
        assert (
            database.execute("select version_num from alembic_version").fetchone()[0]
            == "0011_round_events"
        )


def test_offline_archive_reproduces_evidence_without_evm_or_network(archive, monkeypatch):
    directory, manifest = archive
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("eth_tester"):
            raise AssertionError("offline verification imported the EVM")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    result = verify_archive(directory, expected_archive_id=manifest["archive_id"])
    assert result["verified"] and result["phases"] == 5 and result["transactions"] == 8
    assert not result["receipt_proof_verified"] and not result["evm_reexecuted"]
    assert not result["public_deployment"] and not result["publication_ready"]


def test_repeat_run_preserves_evidence_report_and_transaction_identities(archive, tmp_path):
    directory, first = archive
    source = NetworkProfile.model_validate(load(directory / "source-profile.json"))
    second = run_local_demo(tmp_path / "repeat", source)
    assert second["profile_id"] == first["profile_id"]
    assert second["stages"] == first["stages"]
    assert second["transactions"] == first["transactions"]


def test_reviewed_archive_id_and_no_overwrite(archive):
    directory, manifest = archive
    with pytest.raises(ValueError, match="archive ID"):
        verify_archive(directory, expected_archive_id="0" * 64)
    original = (directory / "archive.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_local_demo(
            directory, NetworkProfile.model_validate(load(directory / "source-profile.json"))
        )
    assert (directory / "archive.json").read_bytes() == original
    assert manifest["archive_id"] == load(directory / "archive.json")["archive_id"]


@pytest.mark.parametrize(
    "fault", ["signature", "receipt-event", "raw-transaction", "summary", "checks"]
)
def test_rehashed_archives_cannot_hide_semantic_corruption(archive, tmp_path, fault):
    directory, _ = archive
    damaged = tmp_path / fault
    shutil.copytree(directory, damaged)
    if fault == "signature":
        path = damaged / "rounds/manipulation/signed-report.json"
        value = load(path)
        value["message"]["evidence_root"] = "a" * 64
    elif fault == "receipt-event":
        path = damaged / "rounds/manipulation/receipt.json"
        value = load(path)
        value["logs"] = []
    elif fault == "raw-transaction":
        path = damaged / "rounds/manipulation/raw-transaction.json"
        value = {"hex": "0x02"}
    elif fault == "summary":
        path = damaged / "archive.json"
        value = load(path)
        value["stages"][1]["candidate_value"] = "100000"
    else:
        path = damaged / "checks.json"
        value = load(path)
        value["expired_report_rejected"] = False
    save(path, value)
    identity = repin(damaged)
    with pytest.raises(ValueError):
        verify_archive(damaged, expected_archive_id=identity)


def test_unreviewed_file_changes_are_detected(archive, tmp_path):
    directory, manifest = archive
    damaged = tmp_path / "damaged"
    shutil.copytree(directory, damaged)
    with (damaged / "timeline.json").open("a", encoding="utf-8") as output:
        output.write(" ")
    with pytest.raises(ValueError, match="checksum"):
        verify_archive(damaged, expected_archive_id=manifest["archive_id"])


def test_archive_cannot_claim_public_execution_or_read_outside_inventory(archive, tmp_path):
    _, manifest = archive
    value = {**manifest, "public_deployment": True}
    value["archive_id"] = checksum({k: v for k, v in value.items() if k != "archive_id"})
    save(tmp_path / "archive.json", value)
    with pytest.raises(ValueError, match="local synthetic"):
        verify_archive(tmp_path, expected_archive_id=value["archive_id"])
    value = {**manifest, "files": {"../outside.json": "0" * 64}}
    value["archive_id"] = checksum({k: v for k, v in value.items() if k != "archive_id"})
    save(tmp_path / "archive.json", value)
    with pytest.raises(ValueError, match="escapes"):
        verify_archive(tmp_path, expected_archive_id=value["archive_id"])


def test_fresh_evidence_reproduces_the_same_session_without_future_observations(archive, tmp_path):
    directory, manifest = archive
    profile = NetworkProfile.model_validate(load(directory / "profile.json"))
    summary = export_evidence(tmp_path / "fresh", profile, start=START, now=START + 95)
    assert summary["cutoff"] == START + 90
    assert summary["evidence_root"] == manifest["stages"][0]["evidence_root"]
    bundle = verify_bundle(EvidenceBundle.model_validate(load(tmp_path / "fresh/evidence.json")))
    for leaf in bundle.leaves:
        if leaf.kind == "observation":
            assert (
                datetime.fromisoformat(
                    leaf.payload["received_at"].replace("Z", "+00:00")
                ).timestamp()
                <= summary["cutoff"]
            )
            if leaf.payload["source_type"] != "exchange":
                assert leaf.payload["provenance"]["verification"] == "synthetic_fixture"
                assert leaf.payload["provenance"]["block"] is None


@pytest.mark.parametrize(
    "start,now",
    [(START + 1, START + 95), (START, START + 59), (START, START + 631), (START, START - 1)],
)
def test_fresh_session_rejects_bad_grid_future_or_finished_sessions(tmp_path, start, now):
    profile = NetworkProfile.model_validate_json(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        export_evidence(tmp_path / "invalid", profile, start=start, now=now)
    assert not (tmp_path / "invalid").exists()


def test_synthetic_provenance_cannot_be_relabelled_live():
    profile = NetworkProfile.model_validate_json(DEFAULT_PROFILE.read_text(encoding="utf-8"))
    row = next(
        row for row in observations(session_spec(profile, START), 90) if row.source == "chainlink"
    )
    with pytest.raises(ValueError, match="replay"):
        Observation.model_validate({**row.model_dump(), "dataset": "live"})
    with pytest.raises(ValueError, match="replay"):
        Observation.model_validate({**row.model_dump(), "raw_payload": {"synthetic": False}})


def test_cli_fresh_snapshot_uses_current_clock_and_preserves_existing_output(
    tmp_path, monkeypatch, capsys
):
    from verdict.cli import main

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return DEFAULT_START + timedelta(seconds=365)

    monkeypatch.setattr("verdict.testnet_cli.datetime", FrozenClock)
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "testnet",
            "demo-evidence",
            "--session-start",
            DEFAULT_START.isoformat(),
            "--directory",
            str(tmp_path / "round"),
        ],
    )
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["cutoff"] == START + 360
    assert result["synthetic"] and not result["public_deployment"]
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert load(tmp_path / "round/session.json")["start"] == START


def test_cli_archive_verification(archive, monkeypatch, capsys):
    from verdict.cli import main

    directory, manifest = archive
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "testnet",
            "demo-verify",
            "--directory",
            str(directory),
            "--archive-id",
            manifest["archive_id"],
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["verified"]


def test_local_evm_rejects_public_profile_and_write_rpc_methods(archive):
    from verdict.demo_evm import LocalEVM
    from verdict.testnet import deployment_plan

    directory, _ = archive
    evm = LocalEVM(timestamp=START)
    public = NetworkProfile.model_validate(load(directory / "source-profile.json"))
    plan = deployment_plan(
        public,
        operation="deploy-oracle",
        sender=evm.deployer,
        administrator=evm.administrator,
        nonce=0,
        now=evm.now,
    )
    with pytest.raises(ValueError, match="local genesis"):
        evm.send_local(plan, public)
    with pytest.raises(ValueError, match="read methods"):
        evm.request("eth_sendRawTransaction", ["0x"])
    with pytest.raises(ValueError, match="future fixture"):
        LocalEVM(timestamp=int(datetime.now(UTC).timestamp()) - 1)
