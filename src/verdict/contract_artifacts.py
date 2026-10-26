"""Pinned local artifacts, canonical ABI arguments, and immutable-aware runtime checks."""

import hashlib
import json
from pathlib import Path

from eth_abi import decode, encode
from eth_utils import keccak

from verdict.dependencies import checksum

ROOT = Path(__file__).resolve().parents[2] / "contracts"
CONTRACT_NAMES = ("DiagnosticOracle", "FailClosedConsumer")


def load_artifact(name: str, expected_id: str | None = None) -> dict:
    if name not in CONTRACT_NAMES:
        raise ValueError("unknown contract artifact")
    data = json.loads((ROOT / "artifacts" / f"{name}.json").read_text(encoding="utf-8"))
    if data["contractName"] != name or (expected_id is not None and checksum(data) != expected_id):
        raise ValueError("contract artifact ID mismatch")
    for source, expected in data["sourceSha256"].items():
        target = (ROOT / source).resolve()
        if not target.is_relative_to((ROOT / "src").resolve()):
            raise ValueError("artifact source escapes the contract workspace")
        if hashlib.sha256(target.read_text(encoding="utf-8").encode()).hexdigest() != expected:
            raise ValueError("contract source changed; rebuild and review artifacts")
    lock = (ROOT / "package-lock.json").read_text(encoding="utf-8")
    if hashlib.sha256(lock.encode()).hexdigest() != data["packageLockSha256"]:
        raise ValueError("contract dependency lock changed")
    return data


def abi_type(field: dict) -> str:
    if field["type"].startswith("tuple"):
        return (
            "(" + ",".join(abi_type(part) for part in field["components"]) + ")" + field["type"][5:]
        )
    return field["type"]


def abi_value(field: dict, value):
    if field["type"] == "tuple":
        if not isinstance(value, dict) or set(value) != {
            part["name"] for part in field["components"]
        }:
            raise ValueError("contract tuple fields do not match the ABI")
        return tuple(abi_value(part, value[part["name"]]) for part in field["components"])
    if field["type"] == "tuple[]":
        return [abi_value({**field, "type": "tuple"}, item) for item in value]
    if field["type"].startswith("bytes"):
        if not isinstance(value, str) or not value.startswith("0x"):
            raise ValueError("ABI byte values require 0x-prefixed hex")
        return bytes.fromhex(value[2:])
    return value


def method(artifact: dict, name: str) -> dict:
    if name == "constructor":
        return next(item for item in artifact["abi"] if item["type"] == "constructor")
    return next(
        item for item in artifact["abi"] if item["type"] == "function" and item["name"] == name
    )


def encode_arguments(artifact: dict, name: str, arguments: dict) -> str:
    entry = method(artifact, name)
    fields = entry["inputs"]
    if set(arguments) != {part["name"] for part in fields}:
        raise ValueError("contract arguments do not match the ABI")
    body = encode(
        [abi_type(part) for part in fields],
        [abi_value(part, arguments[part["name"]]) for part in fields],
    )
    if name == "constructor":
        return artifact["bytecode"] + body.hex()
    signature = name + "(" + ",".join(abi_type(part) for part in fields) + ")"
    return "0x" + (keccak(text=signature)[:4] + body).hex()


def decode_result(artifact: dict, name: str, result: str) -> tuple:
    if not isinstance(result, str) or not result.startswith("0x") or len(result) > 65538:
        raise ValueError("invalid contract result")
    raw = bytes.fromhex(result[2:])
    types = [abi_type(part) for part in method(artifact, name)["outputs"]]
    value = decode(types, raw, strict=True)
    if encode(types, value) != raw:
        raise ValueError("noncanonical contract result")
    return value


def runtime_matches(artifact: dict, code: str) -> bool:
    try:
        if not isinstance(code, str) or not code.startswith("0x") or len(code) > 49154:
            return False
        actual, expected = (
            bytearray.fromhex(code[2:]),
            bytearray.fromhex(artifact["deployedBytecode"][2:]),
        )
        if len(actual) != len(expected) or not actual:
            return False
        for references in artifact["immutableReferences"].values():
            for item in references:
                start, length = item["start"], item["length"]
                if start < 0 or length < 1 or start + length > len(expected):
                    return False
                actual[start : start + length] = b"\x00" * length
                expected[start : start + length] = b"\x00" * length
        return actual == expected
    except (ValueError, KeyError, TypeError):
        return False
