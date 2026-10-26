"""Encrypted local development keystores; private material is never serialized raw."""

import json
import os
from pathlib import Path

from eth_account import Account

SCRYPT_N = 262144


def write_new_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(json.dumps(value, sort_keys=True, indent=2) + "\n")


def create_keystore(password: str) -> tuple[dict, str]:
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("keystore passphrase must contain at least 12 characters")
    try:
        account = Account.create()
        encrypted = Account.encrypt(account.key, password, kdf="scrypt", iterations=SCRYPT_N)
        return encrypted, account.address
    except Exception:
        raise ValueError("unable to create the encrypted signing keystore") from None


def unlock_keystore(path: Path, password: str):
    try:
        if path.stat().st_size > 65536:
            raise ValueError("oversized keystore")
        data = json.loads(path.read_text(encoding="utf-8"))
        crypto = data["crypto"]
        parameters = crypto["kdfparams"]
        # Bound the KDF before decrypting a supplied file. Only our v3 profile is supported.
        expected = {"n": SCRYPT_N, "r": 8, "p": 1, "dklen": 32}
        if data["version"] != 3 or crypto["kdf"] != "scrypt" or crypto["cipher"] != "aes-128-ctr":
            raise ValueError("unsupported keystore")
        if any(
            type(parameters[name]) is not int or parameters[name] != value
            for name, value in expected.items()
        ):
            raise ValueError("unsupported KDF parameters")
        account = Account.from_key(Account.decrypt(data, password))
        if account.address.lower().removeprefix("0x") != data["address"].lower().removeprefix("0x"):
            raise ValueError("keystore address mismatch")
        return account
    except Exception:
        # Decryption/library errors must not echo passwords, key bytes, or file content.
        raise ValueError("unable to unlock the signing keystore") from None
