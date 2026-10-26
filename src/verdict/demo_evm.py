"""Development-only in-memory EVM. No network transport or public transaction sender."""

from datetime import UTC, datetime

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak

from verdict.testnet import validate_plan
from verdict.testnet_rpc import READ_METHODS, rpc_transaction


class LocalEVM:
    def __init__(self, *, timestamp):
        if type(timestamp) is not int or timestamp <= int(datetime.now(UTC).timestamp()):
            raise ValueError(
                "local EVM needs a future fixture clock to avoid host-time mining jumps"
            )
        try:
            from eth_tester import EthereumTester, PyEVMBackend
        except ImportError:
            raise ValueError(
                "the local demonstration requires the development dependencies"
            ) from None
        self._tester = EthereumTester(
            PyEVMBackend(
                genesis_parameters={
                    "timestamp": timestamp,
                    "gas_limit": 30_000_000,
                }
            )
        )
        self._tester.backend.chain.chain_id = 11155111
        self.deployer, self.administrator, self.relayer = self._tester.get_accounts()[:3]

    @property
    def now(self):
        return self._tester.get_block_by_number("latest")["timestamp"]

    @property
    def genesis_hash(self):
        return self._tester.get_block_by_number(0)["hash"]

    def advance(self, timestamp):
        if timestamp <= self.now:
            raise ValueError("local demo clock must advance")
        self._tester.time_travel(timestamp)
        self._tester.mine_blocks()

    def confirm(self):
        self._tester.mine_blocks()

    @staticmethod
    def tag(value):
        if value == "finalized":
            raise ValueError("an in-memory EVM has no consensus finality")
        return int(value, 16) if value.startswith("0x") else value

    @staticmethod
    def _transaction(value):
        names = {
            "maxFeePerGas": "max_fee_per_gas",
            "maxPriorityFeePerGas": "max_priority_fee_per_gas",
        }
        return {
            names.get(key, key): int(item, 16)
            if key in {"nonce", "gas", "value", "maxFeePerGas", "maxPriorityFeePerGas"}
            else item
            for key, item in value.items()
            if key not in {"type", "chainId"}
        }

    def send_local(self, plan, profile):
        if profile.genesis_hash != self.genesis_hash:
            raise ValueError("local execution requires the freshly created local genesis")
        plan = validate_plan(plan, profile, now=self.now)
        return self._tester.send_transaction(self._transaction(rpc_transaction(plan.transaction)))

    def raw_transaction(self, tx_hash):
        tx = self._tester.get_transaction_by_hash(tx_hash)
        value = {
            "type": 2,
            "chainId": tx["chain_id"],
            "nonce": tx["nonce"],
            "to": tx["to"] or b"",
            "value": tx["value"],
            "data": tx["data"],
            "gas": tx["gas"],
            "maxFeePerGas": tx["max_fee_per_gas"],
            "maxPriorityFeePerGas": tx["max_priority_fee_per_gas"],
            "accessList": [],
            "v": tx["v"],
            "r": tx["r"],
            "s": tx["s"],
        }
        raw = TypedTransaction.from_dict(value).encode()
        if "0x" + keccak(raw).hex() != tx_hash or Account.recover_transaction(raw) != tx["from"]:
            raise ValueError("local serialized transaction does not match its hash/sender")
        return "0x" + raw.hex()

    def request(self, method, params):
        from eth_tester.exceptions import TransactionNotFound

        if method not in READ_METHODS:
            raise ValueError("local RPC adapter permits read methods only")
        tester = self._tester
        if method == "eth_chainId":
            return hex(tester.backend.chain.chain_id)
        if method == "eth_getBlockByNumber":
            item = tester.get_block_by_number(self.tag(params[0]))
            return {
                "number": hex(item["number"]),
                "hash": item["hash"],
                "timestamp": hex(item["timestamp"]),
                "gasLimit": hex(item["gas_limit"]),
                "baseFeePerGas": hex(item["base_fee_per_gas"]),
            }
        if method in {"eth_getBalance", "eth_getTransactionCount", "eth_getCode"}:
            function = {
                "eth_getBalance": tester.get_balance,
                "eth_getTransactionCount": tester.get_nonce,
                "eth_getCode": tester.get_code,
            }[method]
            value = function(params[0], self.tag(params[1]))
            return value if method == "eth_getCode" else hex(value)
        if method in {"eth_call", "eth_estimateGas"}:
            tx = self._transaction(params[0])
            tx.setdefault("from", self.deployer)
            at = self.tag(params[1])
            return tester.call(tx, at) if method == "eth_call" else hex(tester.estimate_gas(tx, at))
        try:
            if method == "eth_getTransactionByHash":
                tx = tester.get_transaction_by_hash(params[0])
                return {
                    "hash": tx["hash"],
                    "from": tx["from"],
                    "to": tx["to"] or None,
                    "input": tx["data"],
                    "type": tx["type"],
                    "blockHash": tx["block_hash"],
                    "blockNumber": hex(tx["block_number"]),
                    "accessList": [],
                    **{
                        new: hex(tx[old])
                        for old, new in {
                            "nonce": "nonce",
                            "value": "value",
                            "gas": "gas",
                            "chain_id": "chainId",
                            "max_fee_per_gas": "maxFeePerGas",
                            "max_priority_fee_per_gas": "maxPriorityFeePerGas",
                        }.items()
                    },
                }
            receipt = tester.get_transaction_receipt(params[0])
            return {
                "transactionHash": receipt["transaction_hash"],
                "blockHash": receipt["block_hash"],
                "blockNumber": hex(receipt["block_number"]),
                "from": receipt["from"],
                "to": receipt["to"] or None,
                "status": hex(receipt["status"]),
                "gasUsed": hex(receipt["gas_used"]),
                "contractAddress": receipt["contract_address"],
                "logs": [
                    {"address": log["address"], "topics": log["topics"], "data": log["data"]}
                    for log in receipt["logs"]
                ],
            }
        except TransactionNotFound:
            return None
