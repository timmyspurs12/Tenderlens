"""Offline tests for the pinned SDK driver; every RPC/account object is a fake."""
from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import socket
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from backend.app import TenderLensAPI
from backend.evaluation_adapter import EvaluationAdapter, _make_verified_submission
from backend.genlayer_sdk_driver import DriverConfig, DriverError, GenLayerSDKDriver

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/genlayer_direct/contracts/sdk_driver_test_fixture.py"
TX_DEPLOY = "0x" + "1" * 64
TX_EVAL = "0x" + "2" * 64
CONTRACT = "0x" + "a" * 40
KEY = "1" * 64
_TEST_PROFILE_TEMP_DIRS = []


class FakeChain:
    id = 4221
    rpc_urls = {"default": {"http": ["https://rpc-asimov.genlayer.com"]}}
    consensus_main_contract = {"address": "0x" + "c" * 40, "abi": ["fake-abi"]}


class FakeEvent:
    def __init__(self, provider, name):
        self.provider = provider
        self.name = name

    def process_receipt(self, receipt, errors=None):
        if receipt.get("event") != self.name:
            return []
        return [{"args": {"txId": bytes.fromhex(receipt["genlayer_tx"][2:])}}]


class FakeEventContract:
    def __init__(self, provider):
        self.provider = provider

    def get_event_by_name(self, name):
        return FakeEvent(self.provider, name)


class FakeWeb3:
    def __init__(self, client, chain_id):
        self.client = client
        self.provider = None
        self.eth = SimpleNamespace(chain_id=chain_id, get_transaction_receipt=self.get_transaction_receipt,
                                   contract=self.contract, get_block=self.get_block,
                                   get_balance=self.get_balance, get_code=self.get_code)

    def get_block(self, block):
        assert block == "latest"
        return {"baseFeePerGas": self.client.base_fee_per_gas}

    def get_balance(self, address):
        assert address == "0x" + "b" * 40
        if self.client.balance_query_error:
            raise OSError("simulated balance RPC failure")
        return self.client.wallet_balance_wei

    def get_code(self, address):
        return self.client.contract_code_by_address.get(address.lower(), b"fake-code")

    @staticmethod
    def to_wei(value, unit):
        assert value == 2 and unit == "gwei"
        return 2_000_000_000

    @staticmethod
    def keccak(*, hexstr):
        # This is a deterministic fake; production uses Web3's Ethereum Keccak-256.
        return hashlib.sha3_256(bytes.fromhex(hexstr[2:])).digest()

    @staticmethod
    def to_hex(value):
        if isinstance(value, str):
            return value if value.startswith("0x") else "0x" + value
        return "0x" + bytes(value).hex()

    def get_transaction_receipt(self, tx_hash):
        try:
            return self.client.provider.receipts[tx_hash.lower()]
        except KeyError:
            raise LookupError("receipt not found")

    def contract(self, *, address=None, abi=None):
        assert address == FakeChain.consensus_main_contract["address"]
        assert abi == FakeChain.consensus_main_contract["abi"]
        return FakeEventContract(self.client.provider)


class FakeProvider:
    def __init__(self, client):
        self.client = client
        self.sent = []
        self.receipts = {}
        self.lose_response_at_send_number = None
        self.accept_lost_send = True
        self.contract_code = None
        self.next_genlayer_hash = TX_DEPLOY
        self.rpc_calls = []

    def make_request(self, method, params):
        self.rpc_calls.append((method, params))
        if method == "gen_call":
            return {"result": {"status": {"code": self.client.runtime_probe_status}}}
        if method == "eth_sendRawTransaction":
            raw = params[0]
            evm_hash = self.client.w3.to_hex(self.client.w3.keccak(hexstr=raw)).lower()
            self.sent.append((raw, evm_hash))
            if self.lose_response_at_send_number == len(self.sent):
                if self.accept_lost_send:
                    self.receipts[evm_hash] = {
                        "transactionHash": evm_hash, "status": 1,
                        "event": "NewTransaction", "genlayer_tx": self.next_genlayer_hash,
                    }
                raise TimeoutError("simulated lost RPC response after acceptance")
            return {"result": evm_hash}
        if method == "gen_getContractCode":
            return {"result": self.contract_code}
        raise AssertionError(f"unexpected RPC method in fake: {method}")


class FakeClient:
    def __init__(self, *, chain_id=4221, execution="FINISHED_WITH_RETURN", pending=False):
        self.w3 = FakeWeb3(self, chain_id)
        self.provider = FakeProvider(self)
        self.w3.provider = self.provider
        self.execution = execution
        self.pending = pending
        self.receipt_hash = None
        self.receipt_status = "finalized"
        self.receipt_execution = "FINISHED_WITH_RETURN"
        self.result_json = '{"fake_result":true}'
        self.calls = []
        self.read_accounts = []
        self.wallet_balance_wei = 2_000_000_000_000_000_000
        self.base_fee_per_gas = 100
        self.balance_query_error = False
        self.contract_code_by_address = {}
        self.fee_value = 1_000
        self.fee_distribution = {"leaderExecutionFee": 1_000}
        self.fee_quote_error = False
        self.fee_options = []
        self.submission_fees = []
        self.runtime_probe_status = 0
        self.prepare_before_broadcast = False
        self.preparing_stage = None
        self.prepared_transactions = {}

    def _prepare_sdk_transaction(self, stage, fees):
        if not self.prepare_before_broadcast:
            return
        self.preparing_stage = stage
        actions = importlib.import_module("genlayer_py.contracts.actions")
        actions._prepare_transaction(fees=fees)

    def estimate_transaction_fees(self, options):
        self.fee_options.append(json.loads(json.dumps(options)))
        if self.fee_quote_error:
            raise OSError("simulated fee quote failure")
        return {"distribution": dict(self.fee_distribution), "feeValue": self.fee_value}

    def deploy_contract(self, *, code, account, args, fees=None):
        self.calls.append(("deploy", code, args))
        self.submission_fees.append(("deploy", fees))
        self._prepare_sdk_transaction("deploy", fees)
        self.provider.next_genlayer_hash = TX_DEPLOY
        self.provider.make_request("eth_sendRawTransaction", [self.w3.to_hex(b"deploy")])
        return TX_DEPLOY

    def get_transaction(self, *, transaction_hash):
        self.calls.append(("poll", transaction_hash))
        if self.pending:
            return {"tx_id": transaction_hash, "lifecycle": {"state": "processing"},
                    "tx_execution_result_name": "NOT_VOTED"}
        return {"tx_id": transaction_hash, "lifecycle": {"state": "finalized"},
                "tx_execution_result_name": self.execution}

    def wait_for_transaction_receipt(self, *, transaction_hash, wait_until, interval, retries):
        self.calls.append(("receipt", transaction_hash, wait_until, interval, retries))
        return {"tx_id": self.receipt_hash or transaction_hash,
                "lifecycle": {"state": self.receipt_status},
                "tx_execution_result_name": self.receipt_execution,
                "tx_data_decoded": {"contract_address": CONTRACT}}

    def write_contract(self, *, address, function_name, account, args, fees=None):
        self.calls.append(("write", address, function_name, args))
        self.submission_fees.append(("evaluate", fees))
        self._prepare_sdk_transaction("evaluate", fees)
        self.provider.next_genlayer_hash = TX_EVAL
        self.provider.make_request("eth_sendRawTransaction", [self.w3.to_hex(b"evaluate")])
        return TX_EVAL

    def read_contract(self, *, address, function_name, args, account):
        self.calls.append(("read", address, function_name, args))
        self.read_accounts.append(account)
        return self.result_json


class FakeSDK:
    version = "0.19.0rc2"
    chains = SimpleNamespace(testnet_asimov=FakeChain())
    DISCARD = "fake-discard-sentinel"
    ExecutionResult = SimpleNamespace(FINISHED_WITH_RETURN="FINISHED_WITH_RETURN",
                                      FINISHED_WITH_ERROR="FINISHED_WITH_ERROR")

    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.create_calls = 0
        self.account_calls = 0

    def create_account(self, *, account_private_key):
        self.account_calls += 1
        assert account_private_key == KEY
        return SimpleNamespace(address="0x" + "b" * 40)

    def create_client(self, *, chain, endpoint, account):
        self.create_calls += 1
        assert endpoint == "https://rpc-asimov.genlayer.com"
        return self.client


class Lifecycle:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            event = (name, *args, kwargs) if kwargs else (name, *args)
            self.events.append(event)
        return record


def configured_driver(sdk=None, *, funding_verifier=None, contract_path=FIXTURE,
                     authorized_snapshot_id="10000000-0000-4000-8000-000000000001",
                     authorized_snapshot_sha256="a" * 64):
    sdk = sdk or FakeSDK()
    sdk.client.provider.contract_code = base64.b64encode(
        Path(contract_path).read_text(encoding="utf-8").encode("utf-8")
    ).decode("ascii")
    profile_tempdir = tempfile.TemporaryDirectory()
    _TEST_PROFILE_TEMP_DIRS.append(profile_tempdir)
    profile_path, profile_hash, _profile = write_fee_profile(
        Path(profile_tempdir.name), contract_path=contract_path
    )
    now_utc = datetime.now(timezone.utc)
    driver = GenLayerSDKDriver(
        DriverConfig(
            network="testnet_asimov", private_key=KEY, broadcast_authorized=True,
            signer_address="0x" + "b" * 40, fee_profile_path=profile_path,
            fee_profile_sha256=profile_hash, max_total_cost_wei=10**18,
            authorized_snapshot_id=authorized_snapshot_id,
            authorized_snapshot_sha256=authorized_snapshot_sha256,
            authorization_expires_at=now_utc + timedelta(hours=1),
            authorized_network="testnet_asimov",
            authorized_rpc_endpoint_sha256=hashlib.sha256(b"https://rpc-asimov.genlayer.com").hexdigest(),
            authorized_signer_address="0x" + "b" * 40,
            authorized_contract_sha256=hashlib.sha256(Path(contract_path).read_bytes()).hexdigest(),
            authorized_fee_profile_sha256=profile_hash, authorized_max_total_cost_wei=10**18,
            contract_path=contract_path, poll_interval_seconds=0.001, max_polls=2,
        ),
        runtime_qualifier=lambda dependency, network: dependency == "qualified-fixture" and network == "testnet_asimov",
        network_identity_verifier=lambda client, chain, network: network == "testnet_asimov" and client.w3.eth.chain_id == chain.id,
        funding_verifier=funding_verifier or (lambda client, account, network: True),
        sdk=sdk, sleep=lambda _seconds: None,
    )
    driver._test_profile_tempdir = profile_tempdir
    return driver


def write_fee_profile(root: Path, *, contract_path=FIXTURE, profile_overrides=None, metadata_overrides=None,
                      raw: bytes | None = None):
    measured_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    valid_until = datetime.now(timezone.utc) + timedelta(days=1)
    source = Path(contract_path).read_text(encoding="utf-8")
    dependency = source.split("py-genlayer:", 1)[1].split('"', 1)[0]
    metadata = {
        "sdkVersion": "0.19.0rc2",
        "contractSha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "runtime": dependency,
        "maxSubmissionBytes": 250_000,
        "appealRounds": 1,
        "maxEvmGas": {"deploy": "300000", "evaluate": "200000"},
        "validUntil": valid_until.isoformat().replace("+00:00", "Z"),
    }
    metadata.update(metadata_overrides or {})
    profile = {
        "version": 1,
        "network": "testnet_asimov",
        "measuredAt": measured_at.isoformat().replace("+00:00", "Z"),
        "deploy": {
            "leaderTimeunitsAllocation": "100",
            "validatorTimeunitsAllocation": "200",
            "executionBudgetPerRound": "300000",
            "totalMessageFees": "0",
            "rotationsPerRound": "1",
        },
        "methods": {"evaluate": {
            "leaderTimeunitsAllocation": "150",
            "validatorTimeunitsAllocation": "250",
            "executionBudgetPerRound": "400000",
            "totalMessageFees": "0",
            "rotationsPerRound": "2",
        }},
        "tenderlens": metadata,
    }
    profile.update(profile_overrides or {})
    profile_path = root / "fee-profile.json"
    payload = raw if raw is not None else json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    profile_path.write_bytes(payload)
    return profile_path, hashlib.sha256(payload).hexdigest(), profile


def production_test_driver(root: Path, *, sdk=None, profile_overrides=None, metadata_overrides=None,
                           cost_cap=10**18, network="testnet_asimov", signer_address="0x" + "b" * 40,
                           balance=2 * 10**18, broadcast_authorized=True, private_key=KEY,
                           auth_overrides=None, funding_verifier=None):
    profile_path, profile_hash, profile = write_fee_profile(
        root, profile_overrides=profile_overrides, metadata_overrides=metadata_overrides
    )
    sdk = sdk or FakeSDK()
    sdk.client.wallet_balance_wei = balance
    sdk.client.provider.contract_code = base64.b64encode(FIXTURE.read_bytes()).decode("ascii")
    now_utc = datetime.now(timezone.utc)
    authorized_snapshot_id = "10000000-0000-4000-8000-000000000001"
    authorized_snapshot_sha256 = "a" * 64
    config_values = dict(
        network=network,
        private_key=private_key,
        broadcast_authorized=broadcast_authorized,
        signer_address=signer_address,
        fee_profile_path=profile_path,
        fee_profile_sha256=profile_hash,
        max_total_cost_wei=cost_cap,
        authorized_snapshot_id=authorized_snapshot_id,
        authorized_snapshot_sha256=authorized_snapshot_sha256,
        authorization_expires_at=now_utc + timedelta(hours=1),
        authorized_network=network,
        authorized_rpc_endpoint_sha256=hashlib.sha256(b"https://rpc-asimov.genlayer.com").hexdigest(),
        authorized_signer_address=signer_address,
        authorized_contract_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        authorized_fee_profile_sha256=profile_hash,
        authorized_max_total_cost_wei=cost_cap,
        contract_path=FIXTURE,
        poll_interval_seconds=0.001,
        max_polls=2,
    )
    config_values.update(auth_overrides or {})
    driver = GenLayerSDKDriver(
        DriverConfig(**config_values),
        runtime_qualifier=lambda dependency, target: dependency == "qualified-fixture" and target == network,
        network_identity_verifier=lambda client, chain, target: target == network and client.w3.eth.chain_id == chain.id,
        funding_verifier=funding_verifier,
        sdk=sdk,
        sleep=lambda _seconds: None,
    )
    return driver, sdk, profile, profile_hash


def wsgi_call(app, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json", "HTTP_AUTHORIZATION": "Bearer " + ("t" * 48),
        "wsgi.input": io.BytesIO(raw),
    }
    response = {}
    payload = b"".join(app(environ, lambda status, headers: response.update(status=status)))
    return int(response["status"].split()[0]), json.loads(payload) if payload else None


class DurableRequestHarness:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.api = TenderLensAPI(root / "api.sqlite", root / "blobs", "t" * 48, "sdk-driver-ws")
        status, tender = wsgi_call(self.api, "POST", "/api/tenders", {
            "title": "Synthetic driver test", "reference": "SDK-DRIVER-TEST",
        })
        assert status == 201
        status, _ = wsgi_call(self.api, "POST", f"/api/tenders/{tender['id']}/requirements", {
            "code": "SYNTHETIC-1", "text": "Hold a valid test licence.",
            "category": "eligibility", "mandatory": True, "kind": "judgment",
        })
        assert status == 201
        status, snapshot = wsgi_call(self.api, "POST", f"/api/tenders/{tender['id']}/evaluation-snapshots", {})
        assert status == 201
        self.adapter = EvaluationAdapter(self.api)
        self.prepared = self.adapter.prepare(snapshot["snapshot_id"], "lost-response-test")
        assert self.adapter.claim_submission(self.prepared["request_id"])
        self.lifecycle = self.adapter.lifecycle(self.prepared["request_id"])
        self.verified = _make_verified_submission(
            self.prepared["contract_input_json"], self.prepared["request_id"],
            self.prepared["snapshot_id"], self.prepared["snapshot_sha256"],
            self.prepared["policy_version"],
        )

    def result_json(self):
        submission = json.loads(self.prepared["contract_input_json"])
        decisions = [{
            "requirement_id": item["id"], "code": item["code"], "mandatory": item["mandatory"],
            "status": "insufficient_evidence",
            "reason": "No evidence was supplied for this requirement; absence is not compliance.",
            "evidence_refs": [],
        } for item in submission["requirements"]]
        unresolved = [{"requirement_id": item["requirement_id"], "code": item["code"], "status": item["status"]}
                      for item in decisions]
        return json.dumps({
            "schema_version": "tenderlens.evaluation-result.v1",
            "evaluation_id": submission["evaluation_id"], "tender_id": submission["tender_id"],
            "snapshot_id": submission["snapshot_id"], "snapshot_sha256": submission["snapshot_sha256"],
            "aggregation_policy": submission["aggregation_policy"], "overall_status": "inconclusive",
            "decisions": decisions, "unresolved_requirements": unresolved,
            "disclaimer": "Evidence interpretation only; not legal advice and not an automatic procurement award.",
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def close(self):
        self.temp.cleanup()


class GenLayerSDKDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._socket_connect_patch = patch.object(
            socket.socket, "connect", side_effect=AssertionError("offline driver tests must not open sockets")
        )
        cls._socket_create_patch = patch("socket.create_connection",
                                         side_effect=AssertionError("offline driver tests must not open sockets"))
        cls._socket_sendto_patch = patch.object(
            socket.socket, "sendto", side_effect=AssertionError("offline driver tests must not open sockets")
        )
        root = ModuleType("genlayer_py")
        root.__path__ = []
        contracts = ModuleType("genlayer_py.contracts")
        contracts.__path__ = []
        actions = ModuleType("genlayer_py.contracts.actions")
        actions.DISCARD = "fake-discard-sentinel"
        actions._prepare_transaction = lambda **_kwargs: {}
        cls._sdk_modules_patch = patch.dict(sys.modules, {
            "genlayer_py": root,
            "genlayer_py.contracts": contracts,
            "genlayer_py.contracts.actions": actions,
        })
        cls._socket_connect_patch.start()
        cls._socket_create_patch.start()
        cls._socket_sendto_patch.start()
        cls._sdk_modules_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._sdk_modules_patch.stop()
        cls._socket_sendto_patch.stop()
        cls._socket_create_patch.stop()
        cls._socket_connect_patch.stop()
        for profile_tempdir in _TEST_PROFILE_TEMP_DIRS:
            profile_tempdir.cleanup()
        _TEST_PROFILE_TEMP_DIRS.clear()
        super().tearDownClass()

    def submission(self):
        return ('{"snapshot_id":"10000000-0000-4000-8000-000000000001",'
                '"snapshot_sha256":"' + "a" * 64 + '",'
                '"aggregation_policy":"mandatory_gate_optional_advisory_v1"}')

    def test_uses_pinned_sdk_deploy_write_poll_and_decoded_read_interfaces(self):
        sdk = FakeSDK()
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        lifecycle = Lifecycle()
        exact_input = self.submission()
        verified = _make_verified_submission(exact_input, "10000000-0000-4000-8000-000000000001",
                                             "10000000-0000-4000-8000-000000000001", "a" * 64,
                                             "mandatory_gate_optional_advisory_v1")
        result = driver(verified, request_id="10000000-0000-4000-8000-000000000001",
                        snapshot_id="10000000-0000-4000-8000-000000000001",
                        snapshot_sha256="a" * 64,
                        policy_version="mandatory_gate_optional_advisory_v1",
                        lifecycle=lifecycle, runtime=runtime)
        self.assertEqual(sdk.client.calls[0], ("deploy", FIXTURE.read_text(), [exact_input]))
        self.assertIn(("write", CONTRACT, "evaluate", []), sdk.client.calls)
        self.assertIn(("read", CONTRACT, "get_result", []), sdk.client.calls)
        self.assertEqual(result["deployment_tx_hash"], TX_DEPLOY)
        self.assertEqual(result["evaluation_tx_hash"], TX_EVAL)
        self.assertRegex(result["deployment_evm_tx_hash"], r"^0x[0-9a-f]{64}$")
        self.assertRegex(result["evaluation_evm_tx_hash"], r"^0x[0-9a-f]{64}$")
        self.assertIn(("deployment_broadcast", result["deployment_evm_tx_hash"]), lifecycle.events)
        self.assertIn(("evaluation_broadcast", result["evaluation_evm_tx_hash"]), lifecycle.events)
        self.assertEqual(len(sdk.client.provider.sent), 2)
        self.assertEqual(result["result_json"], '{"fake_result":true}')
        self.assertIn(("deployment_submitted", TX_DEPLOY), lifecycle.events)
        self.assertIn(("evaluation_submitted", TX_EVAL), lifecycle.events)
        self.assertIn(("finalized",), lifecycle.events)

    def test_lost_deployment_rpc_response_is_recovered_from_durable_evm_hash_without_resend(self):
        harness = DurableRequestHarness()
        try:
            sdk = FakeSDK()
            sdk.client.provider.lose_response_at_send_number = 1
            driver = configured_driver(
                sdk, authorized_snapshot_id=harness.prepared["snapshot_id"],
                authorized_snapshot_sha256=harness.prepared["snapshot_sha256"],
            )
            runtime = driver.prepare()
            with self.assertRaisesRegex(DriverError, "deployment_submission_outcome_unknown"):
                driver(harness.verified, request_id=harness.prepared["request_id"],
                       snapshot_id=harness.prepared["snapshot_id"],
                       snapshot_sha256=harness.prepared["snapshot_sha256"],
                       policy_version=harness.prepared["policy_version"],
                       lifecycle=harness.lifecycle, runtime=runtime)

            request = harness.adapter.get_request(harness.prepared["request_id"])
            expected_evm_hash = sdk.client.provider.sent[0][1]
            self.assertEqual(request["lifecycle_state"], "outcome_unknown")
            self.assertEqual(request["deployment_evm_tx_hash"], expected_evm_hash)
            self.assertIsNone(request["deployment_tx_hash"])
            self.assertIsNone(request["evaluation_evm_tx_hash"])
            self.assertEqual(len(sdk.client.provider.sent), 1)

            recovered = driver.reconcile(harness.adapter, harness.prepared["request_id"])
            self.assertEqual(recovered["lifecycle_state"], "deployment_finalized")
            self.assertEqual(recovered["deployment_evm_tx_hash"], expected_evm_hash)
            self.assertEqual(recovered["deployment_tx_hash"], TX_DEPLOY)
            self.assertEqual(recovered["contract_address"], CONTRACT)
            self.assertIsNone(recovered["evaluation_tx_hash"])
            self.assertIsNone(recovered["evaluation_evm_tx_hash"])
            self.assertEqual(len(sdk.client.provider.sent), 1)
            self.assertFalse(any(call[0] == "write" for call in sdk.client.calls))
        finally:
            harness.close()

    def test_lost_deployment_response_without_receipt_stays_pending_and_never_resends(self):
        harness = DurableRequestHarness()
        try:
            sdk = FakeSDK()
            sdk.client.provider.lose_response_at_send_number = 1
            sdk.client.provider.accept_lost_send = False
            driver = configured_driver(
                sdk, authorized_snapshot_id=harness.prepared["snapshot_id"],
                authorized_snapshot_sha256=harness.prepared["snapshot_sha256"],
            )
            runtime = driver.prepare()
            with self.assertRaisesRegex(DriverError, "deployment_submission_outcome_unknown"):
                driver(harness.verified, request_id=harness.prepared["request_id"],
                       snapshot_id=harness.prepared["snapshot_id"],
                       snapshot_sha256=harness.prepared["snapshot_sha256"],
                       policy_version=harness.prepared["policy_version"],
                       lifecycle=harness.lifecycle, runtime=runtime)
            evm_hash = sdk.client.provider.sent[0][1]
            for _ in range(2):
                request = driver.reconcile(harness.adapter, harness.prepared["request_id"])
                self.assertEqual(request["lifecycle_state"], "pending")
                self.assertEqual(request["deployment_evm_tx_hash"], evm_hash)
                self.assertIsNone(request["deployment_tx_hash"])
                self.assertEqual(len(sdk.client.provider.sent), 1)
        finally:
            harness.close()

    def test_lost_evaluation_rpc_response_recovers_tx_and_actual_contract_result_without_resend(self):
        harness = DurableRequestHarness()
        try:
            sdk = FakeSDK()
            sdk.client.provider.lose_response_at_send_number = 2
            sdk.client.result_json = harness.result_json()
            driver = configured_driver(
                sdk, authorized_snapshot_id=harness.prepared["snapshot_id"],
                authorized_snapshot_sha256=harness.prepared["snapshot_sha256"],
            )
            runtime = driver.prepare()
            with self.assertRaisesRegex(DriverError, "evaluation_submission_outcome_unknown"):
                driver(harness.verified, request_id=harness.prepared["request_id"],
                       snapshot_id=harness.prepared["snapshot_id"],
                       snapshot_sha256=harness.prepared["snapshot_sha256"],
                       policy_version=harness.prepared["policy_version"],
                       lifecycle=harness.lifecycle, runtime=runtime)

            uncertain = harness.adapter.get_request(harness.prepared["request_id"])
            expected_evm_hash = sdk.client.provider.sent[1][1]
            self.assertEqual(uncertain["lifecycle_state"], "outcome_unknown")
            self.assertEqual(uncertain["evaluation_evm_tx_hash"], expected_evm_hash)
            self.assertIsNone(uncertain["evaluation_tx_hash"])
            self.assertEqual(len(sdk.client.provider.sent), 2)

            recovered = driver.reconcile(harness.adapter, harness.prepared["request_id"])
            durable = harness.adapter.get_request(harness.prepared["request_id"])
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(durable["lifecycle_state"], "completed")
            self.assertEqual(durable["evaluation_evm_tx_hash"], expected_evm_hash)
            self.assertEqual(durable["evaluation_tx_hash"], TX_EVAL)
            self.assertEqual(recovered["evaluation_tx_hash"], TX_EVAL)
            self.assertEqual(harness.adapter.get_result(harness.prepared["request_id"]),
                             json.loads(harness.result_json()))
            self.assertEqual(len(sdk.client.provider.sent), 2)
            self.assertIn(("read", CONTRACT, "get_result", []), sdk.client.calls)
        finally:
            harness.close()

    def test_unjournaled_send_methods_are_rejected_before_provider_can_broadcast(self):
        sdk = FakeSDK()
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        with self.assertRaisesRegex(DriverError, "unsupported_sdk_broadcast_method"):
            with driver._journal_broadcast(runtime, "deployment", Lifecycle()):
                runtime.client.provider.make_request("eth_sendTransaction", [{"to": CONTRACT}])
        self.assertFalse(sdk.client.provider.sent)

    def test_evm_hash_journal_database_failure_aborts_before_raw_transaction_send(self):
        sdk = FakeSDK()
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        verified = _make_verified_submission(
            self.submission(), "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000001", "a" * 64,
            "mandatory_gate_optional_advisory_v1")

        class JournalWriteFailure(Lifecycle):
            def deployment_broadcast(self, _evm_hash):
                raise OSError("simulated durable journal failure")

        lifecycle = JournalWriteFailure()
        with self.assertRaisesRegex(DriverError, "durable_broadcast_journal_failed"):
            driver(verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                   snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                   lifecycle=lifecycle, runtime=runtime)
        self.assertEqual(sdk.client.provider.sent, [])

    def test_driver_rejects_caller_supplied_raw_payload(self):
        sdk = FakeSDK()
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        with self.assertRaisesRegex(DriverError, "adapter_verified_snapshot_required"):
            driver(self.submission(), request_id="10000000-0000-4000-8000-000000000001",
                   snapshot_id="10000000-0000-4000-8000-000000000001", snapshot_sha256="a" * 64,
                   policy_version="mandatory_gate_optional_advisory_v1", lifecycle=Lifecycle(), runtime=runtime)
        self.assertFalse(any(call[0] in {"deploy", "write"} for call in sdk.client.calls))

    def test_reconcile_polls_deployment_but_does_not_implicitly_submit_evaluation(self):
        sdk = FakeSDK()
        driver = configured_driver(sdk)
        request_id = "10000000-0000-4000-8000-000000000001"
        state = {"request_id": request_id, "lifecycle_state": "deployment_submitted",
                 "deployment_tx_hash": TX_DEPLOY, "evaluation_tx_hash": None, "contract_address": None,
                 "last_network_status": None}
        class RecoveryAdapter:
            def get_request(self, _request_id):
                return dict(state)
            def lifecycle(self, _request_id):
                class DurableLifecycle:
                    def pending(self, status):
                        state["lifecycle_state"] = "pending"
                        state["last_network_status"] = status
                    def outcome_unknown(self, status=None):
                        state["lifecycle_state"] = "outcome_unknown"
                        state["last_network_status"] = status
                    def failed(self, *_args):
                        state["lifecycle_state"] = "failed"
                    def deployment_finalized(self, address):
                        state["lifecycle_state"] = "deployment_finalized"
                        state["contract_address"] = address
                return DurableLifecycle()
        recovered = driver.reconcile(RecoveryAdapter(), request_id)
        self.assertEqual(recovered["lifecycle_state"], "deployment_finalized")
        self.assertEqual(recovered["contract_address"], CONTRACT)
        self.assertIsNone(recovered["evaluation_tx_hash"])
        self.assertFalse(any(call[0] == "write" for call in sdk.client.calls))

    def test_readonly_recovery_context_needs_no_broadcast_or_funding_gate(self):
        sdk = FakeSDK()
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key="", contract_path=FIXTURE),
            network_identity_verifier=lambda client, chain, network: client.w3.eth.chain_id == chain.id,
            sdk=sdk)
        runtime = driver.prepare_readonly()
        self.assertFalse(runtime.broadcast_ready)
        self.assertIsNone(runtime.account)
        self.assertEqual(sdk.account_calls, 0)
        verified = _make_verified_submission(self.submission(), "10000000-0000-4000-8000-000000000001",
                                             "10000000-0000-4000-8000-000000000001", "a" * 64,
                                             "mandatory_gate_optional_advisory_v1")
        with self.assertRaisesRegex(DriverError, "broadcast_preflight_required"):
            driver(verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                   snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                   lifecycle=Lifecycle(), runtime=runtime)
        self.assertFalse(any(call[0] in {"deploy", "write"} for call in sdk.client.calls))

    def test_read_only_result_queries_receive_address_only_account_for_pinned_sdk(self):
        sdk = FakeSDK()
        address = "0x" + "b" * 40
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key="", signer_address=address,
                         contract_path=FIXTURE),
            network_identity_verifier=lambda client, chain, network: client.w3.eth.chain_id == chain.id,
            sdk=sdk)
        runtime = driver.prepare_readonly()
        account = driver._read_account(runtime)
        self.assertEqual(account.address, address)
        self.assertFalse(hasattr(account, "key"))
        result = runtime.client.read_contract(address=CONTRACT, function_name="get_result",
                                              args=[], account=account)
        self.assertEqual(result, runtime.client.result_json)
        self.assertIs(runtime.client.read_accounts[-1], account)
        self.assertEqual(sdk.account_calls, 0)

    def test_broadcast_requires_explicit_authorization_before_client_or_rpc(self):
        sdk = FakeSDK()
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key=KEY, contract_path=FIXTURE),
            runtime_qualifier=lambda *_: True, funding_verifier=lambda *_: True, sdk=sdk)
        with self.assertRaisesRegex(DriverError, "broadcast_not_authorized"):
            driver.prepare()
        self.assertEqual(sdk.create_calls, 0)

    def test_measured_profile_quotes_both_stages_and_checks_full_cost_against_balance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, profile, _ = production_test_driver(Path(temp_dir))
            runtime = driver.prepare()
            expected_cost = sum(
                quote["feeValue"] + quote["maxEvmGas"] * runtime.max_fee_per_gas
                for quote in runtime.fee_quotes.values()
            )
            self.assertEqual(runtime.estimated_total_cost_wei, expected_cost)
            self.assertEqual(runtime.max_total_cost_wei, 10**18)
            self.assertTrue(runtime.balance_sufficient)
            self.assertTrue(runtime.cost_within_cap)
            self.assertEqual([item["rotations"] for item in sdk.client.fee_options], [[1, 1], [2, 2]])
            self.assertEqual(sdk.client.fee_options[0]["appealRounds"], 1)
            self.assertEqual(sdk.client.fee_options[1]["validatorTimeunitsAllocation"], 250)
            self.assertEqual(runtime.fee_profile["_sha256"], driver.config.fee_profile_sha256)
            self.assertFalse(sdk.client.provider.sent)
            self.assertFalse(any(call[0] in {"deploy", "write"} for call in sdk.client.calls))

    def test_cost_ceiling_applies_to_genlayer_deposits_and_all_outer_evm_gas(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk = FakeSDK()
            max_fee = 100 + 2_000_000_000
            full_cost = (2 * sdk.client.fee_value) + ((300_000 + 200_000) * max_fee)
            driver, sdk, _profile, _hash = production_test_driver(
                Path(temp_dir), sdk=sdk, cost_cap=full_cost - 1
            )
            with self.assertRaisesRegex(DriverError, "approved_cost_cap_exceeded"):
                driver.prepare()
            self.assertEqual(len(sdk.client.fee_options), 2)
            self.assertFalse(sdk.client.provider.sent)
            self.assertFalse(any(call[0] in {"deploy", "write"} for call in sdk.client.calls))

    def test_insufficient_or_unavailable_balance_is_not_treated_as_zero_cost_or_funding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir), balance=1)
            with self.assertRaisesRegex(DriverError, "insufficient_wallet_balance"):
                driver.prepare()
            self.assertFalse(sdk.client.provider.sent)
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir))
            sdk.client.balance_query_error = True
            with self.assertRaisesRegex(DriverError, "wallet_balance_unavailable"):
                driver.prepare()
            self.assertFalse(sdk.client.provider.sent)

    def test_unknown_fee_or_evm_cost_fails_closed_instead_of_becoming_zero(self):
        for mutation, error in (
            (lambda client: setattr(client, "fee_value", None), "genlayer_fee_quote_invalid"),
            (lambda client: setattr(client, "fee_value", 0), "genlayer_fee_quote_invalid"),
            (lambda client: setattr(client, "fee_distribution", {}), "genlayer_fee_quote_invalid"),
            (lambda client: setattr(client, "fee_quote_error", True), "genlayer_fee_quote_unavailable"),
            (lambda client: setattr(client, "base_fee_per_gas", None), "evm_fee_quote_unavailable"),
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir))
                mutation(sdk.client)
                with self.subTest(error=error), self.assertRaisesRegex(DriverError, error):
                    driver.prepare()
                self.assertFalse(sdk.client.provider.sent)

    def test_fee_profile_quotes_are_passed_to_sdk_and_prepared_transaction_is_guarded_pre_send(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk = FakeSDK()
            sdk.client.prepare_before_broadcast = True
            driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir), sdk=sdk)
            runtime = driver.prepare()
            signer = runtime.signer_address
            sdk.client.prepared_transactions = {
                "deploy": {"gas": 300_000, "chainId": 4221, "from": signer,
                           "to": FakeChain.consensus_main_contract["address"], "data": "0x1234",
                           "value": runtime.fee_quotes["deploy"]["feeValue"],
                           "maxFeePerGas": runtime.max_fee_per_gas},
                "evaluate": {"gas": 200_000, "chainId": 4221, "from": signer,
                             "to": FakeChain.consensus_main_contract["address"], "data": "0x1234",
                             "value": runtime.fee_quotes["evaluate"]["feeValue"],
                             "maxFeePerGas": runtime.max_fee_per_gas},
            }
            verified = _make_verified_submission(
                self.submission(), "10000000-0000-4000-8000-000000000001",
                "10000000-0000-4000-8000-000000000001", "a" * 64,
                "mandatory_gate_optional_advisory_v1")
            root = ModuleType("genlayer_py")
            root.__path__ = []
            contracts = ModuleType("genlayer_py.contracts")
            contracts.__path__ = []
            actions = ModuleType("genlayer_py.contracts.actions")
            actions._prepare_transaction = lambda **_kwargs: dict(
                sdk.client.prepared_transactions[sdk.client.preparing_stage]
            )
            with patch.dict("sys.modules", {
                "genlayer_py": root,
                "genlayer_py.contracts": contracts,
                "genlayer_py.contracts.actions": actions,
            }):
                result = driver(
                    verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                    snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                    lifecycle=Lifecycle(), runtime=runtime,
                )
            self.assertEqual(result["deployment_tx_hash"], TX_DEPLOY)
            self.assertEqual(result["evaluation_tx_hash"], TX_EVAL)
            self.assertEqual(len(sdk.client.provider.sent), 2)
            self.assertEqual(sdk.client.submission_fees, [
                ("deploy", runtime.fee_quotes["deploy"]["fees"]),
                ("evaluate", runtime.fee_quotes["evaluate"]["fees"]),
            ])

        scenarios = ("gas_over_cap", "missing_evm_fee", "wrong_consensus_target")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                sdk = FakeSDK()
                sdk.client.prepare_before_broadcast = True
                driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir), sdk=sdk)
                runtime = driver.prepare()
                transaction = {
                    "gas": 300_000,
                    "chainId": 4221,
                    "from": runtime.signer_address,
                    "to": FakeChain.consensus_main_contract["address"],
                    "data": "0x1234",
                    "value": runtime.fee_quotes["deploy"]["feeValue"],
                    "maxFeePerGas": runtime.max_fee_per_gas,
                }
                if scenario == "gas_over_cap":
                    transaction["gas"] = 300_001
                    expected = "prepared_evm_gas_exceeds_profile"
                elif scenario == "missing_evm_fee":
                    transaction.pop("maxFeePerGas")
                    expected = "prepared_evm_transaction_invalid"
                else:
                    transaction["to"] = "0x" + "d" * 40
                    expected = "prepared_evm_transaction_target_mismatch"
                sdk.client.prepared_transactions = {"deploy": transaction}
                verified = _make_verified_submission(
                    self.submission(), "10000000-0000-4000-8000-000000000001",
                    "10000000-0000-4000-8000-000000000001", "a" * 64,
                    "mandatory_gate_optional_advisory_v1")
                root = ModuleType("genlayer_py")
                root.__path__ = []
                contracts = ModuleType("genlayer_py.contracts")
                contracts.__path__ = []
                actions = ModuleType("genlayer_py.contracts.actions")
                actions._prepare_transaction = lambda **_kwargs: dict(
                    sdk.client.prepared_transactions[sdk.client.preparing_stage]
                )
                with patch.dict("sys.modules", {
                    "genlayer_py": root,
                    "genlayer_py.contracts": contracts,
                    "genlayer_py.contracts.actions": actions,
                }):
                    with self.assertRaisesRegex(DriverError, expected):
                        driver(
                            verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                            snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                            lifecycle=Lifecycle(), runtime=runtime,
                        )
                self.assertFalse(sdk.client.provider.sent)
                self.assertFalse(any(call[0] == "write" for call in sdk.client.calls))

    def test_fee_profiles_require_digest_scope_and_nonexpired_measured_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, _sdk, profile, profile_hash = production_test_driver(Path(temp_dir))
            source = FIXTURE.read_text(encoding="utf-8")
            self.assertEqual(driver._load_fee_profile(source)["_sha256"], profile_hash)

            missing_profile = GenLayerSDKDriver(
                replace(driver.config, fee_profile_path=None),
                runtime_qualifier=driver.runtime_qualifier,
                network_identity_verifier=driver.network_identity_verifier,
                sdk=FakeSDK(),
            )
            with self.assertRaisesRegex(DriverError, "fee_profile_unavailable"):
                missing_profile._load_fee_profile(source)

            bad_hash = GenLayerSDKDriver(
                replace(driver.config, fee_profile_sha256="f" * 64), sdk=FakeSDK()
            )
            with self.assertRaisesRegex(DriverError, "fee_profile_digest_mismatch"):
                bad_hash._load_fee_profile(source)

        cases = (
            ({"network": "studionet"}, {}, "fee_profile_network_mismatch"),
            ({}, {"validUntil": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}, "fee_profile_expired"),
            ({}, {"validUntil": "not-a-timestamp"}, "fee_profile_expiry_missing_or_invalid"),
            ({"measuredAt": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()}, {}, "fee_profile_stale"),
            ({}, {"contractSha256": "f" * 64}, "fee_profile_qualification_mismatch"),
            ({}, {"sdkVersion": "0.18.0"}, "fee_profile_qualification_mismatch"),
            ({"deploy": {"leaderTimeunitsAllocation": "100", "validatorTimeunitsAllocation": "200", "executionBudgetPerRound": "300000", "rotationsPerRound": "1"}}, {}, "fee_profile_invalid"),
            ({"methods": {"evaluate": {"leaderTimeunitsAllocation": "150", "validatorTimeunitsAllocation": "250", "executionBudgetPerRound": "400000", "totalMessageFees": "0"}}}, {}, "fee_profile_invalid"),
            ({}, {"runtime": "unqualified"}, "fee_profile_qualification_mismatch"),
            ({"measuredAt": "not-a-timestamp"}, {}, "fee_profile_measurement_time_missing"),
        )
        for profile_override, metadata_override, expected in cases:
            with tempfile.TemporaryDirectory() as temp_dir:
                driver, _sdk, _profile, _hash = production_test_driver(
                    Path(temp_dir), profile_overrides=profile_override,
                    metadata_overrides=metadata_override,
                )
                with self.subTest(expected=expected), self.assertRaisesRegex(DriverError, expected):
                    driver._load_fee_profile(FIXTURE.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temp_dir:
            profile_path, digest, _ = write_fee_profile(Path(temp_dir), raw=b"{")
            malformed = GenLayerSDKDriver(DriverConfig(
                network="testnet_asimov", private_key=KEY, fee_profile_path=profile_path,
                fee_profile_sha256=digest, contract_path=FIXTURE), sdk=FakeSDK())
            with self.assertRaisesRegex(DriverError, "fee_profile_invalid"):
                malformed._load_fee_profile(FIXTURE.read_text(encoding="utf-8"))

    def test_missing_invalid_and_expired_authorization_never_initialize_sdk(self):
        scenarios = (
            ({"broadcast_authorized": False}, "broadcast_not_authorized"),
            ({"authorization_expires_at": None}, "authorization_expiry_missing"),
            ({"authorization_expires_at": None, "authorization_expires_at_raw": "not-a-date"}, "authorization_expiry_invalid"),
            ({"authorization_expires_at": datetime.now()}, "authorization_expiry_invalid"),
            ({"authorization_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}, "authorization_expired"),
            ({"authorized_network": "studionet"}, "authorization_scope_mismatch"),
            ({"authorized_rpc_endpoint_sha256": "f" * 64}, "authorization_scope_mismatch"),
            ({"authorized_signer_address": "0x" + "c" * 40}, "authorization_scope_mismatch"),
            ({"authorized_contract_sha256": "f" * 64}, "authorization_scope_mismatch"),
            ({"authorized_fee_profile_sha256": "f" * 64}, "authorization_scope_mismatch"),
            ({"authorized_max_total_cost_wei": 1}, "authorization_scope_mismatch"),
            ({"authorized_snapshot_id": None}, "authorized_snapshot_id_missing"),
            ({"authorized_snapshot_sha256": "not-a-digest"}, "authorized_snapshot_digest_missing"),
        )
        for override, expected in scenarios:
            with tempfile.TemporaryDirectory() as temp_dir:
                driver, sdk, _profile, _hash = production_test_driver(
                    Path(temp_dir), auth_overrides=override
                )
                with self.subTest(expected=expected), self.assertRaisesRegex(DriverError, expected):
                    driver.prepare()
                self.assertEqual(sdk.create_calls, 0)
                self.assertEqual(sdk.account_calls, 0)

    def test_exact_snapshot_authorization_is_checked_before_sdk_and_again_before_submission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir))
            verified = _make_verified_submission(
                self.submission(), "10000000-0000-4000-8000-000000000001",
                "10000000-0000-4000-8000-000000000001", "a" * 64,
                "mandatory_gate_optional_advisory_v1")
            driver.validate_submission(
                verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
            )
            altered_snapshot = _make_verified_submission(
                self.submission(), "10000000-0000-4000-8000-000000000002",
                "10000000-0000-4000-8000-000000000002", "b" * 64,
                "mandatory_gate_optional_advisory_v1")
            with self.assertRaisesRegex(DriverError, "approved_snapshot_mismatch"):
                driver.validate_submission(
                    altered_snapshot, request_id=altered_snapshot.request_id,
                    snapshot_id=altered_snapshot.snapshot_id,
                    snapshot_sha256=altered_snapshot.snapshot_sha256,
                    policy_version=altered_snapshot.policy_version,
                )
            self.assertEqual(sdk.create_calls, 0)

    def test_missing_signer_bad_private_key_and_missing_profile_block_before_sdk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, _profile, _hash = production_test_driver(
                Path(temp_dir), auth_overrides={"signer_address": None}
            )
            with self.assertRaisesRegex(DriverError, "authorized_signer_address_missing"):
                driver.prepare()
            self.assertEqual(sdk.create_calls, 0)
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, _profile, _hash = production_test_driver(
                Path(temp_dir), private_key="invalid"
            )
            with self.assertRaisesRegex(DriverError, "invalid_account_configuration"):
                driver.prepare()
            self.assertEqual(sdk.create_calls, 0)
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir))
            driver.config = replace(driver.config, fee_profile_path=None)
            with self.assertRaisesRegex(DriverError, "fee_profile_or_cost_cap_missing"):
                driver.prepare()
            self.assertEqual(sdk.create_calls, 0)
        with tempfile.TemporaryDirectory() as temp_dir:
            driver, sdk, _profile, _hash = production_test_driver(Path(temp_dir))
            driver.config = replace(driver.config, max_total_cost_wei=None)
            with self.assertRaisesRegex(DriverError, "fee_profile_or_cost_cap_missing"):
                driver.prepare()
            self.assertEqual(sdk.create_calls, 0)

    def test_missing_network_or_rpc_and_unpinned_endpoints_fail_before_sdk(self):
        scenarios = (
            (DriverConfig(network="", private_key="", contract_path=FIXTURE), "unsupported_network"),
            (DriverConfig(network="unknown", private_key="", contract_path=FIXTURE), "unsupported_network"),
            (DriverConfig(network="testnet_bradbury", private_key="", contract_path=FIXTURE), "bradbury_genlayer_node_rpc_required"),
            (DriverConfig(network="testnet_bradbury", private_key="", rpc_url="http://node.example", contract_path=FIXTURE), "invalid_rpc_endpoint"),
            (DriverConfig(network="testnet_bradbury", private_key="", rpc_url="https://user:secret@node.example/api", contract_path=FIXTURE), "invalid_rpc_endpoint"),
            (DriverConfig(network="testnet_bradbury", private_key="", rpc_url="https://node.example/api?token=secret", contract_path=FIXTURE), "invalid_rpc_endpoint"),
            (DriverConfig(network="testnet_bradbury", private_key="", rpc_url="https://rpc.testnet-chain.genlayer.com", contract_path=FIXTURE), "underlying_or_unqualified_bradbury_rpc"),
            (DriverConfig(network="studionet", private_key="", rpc_url="https://wrong.example", contract_path=FIXTURE), "rpc_endpoint_not_pinned"),
        )
        for config, expected in scenarios:
            sdk = FakeSDK()
            driver = GenLayerSDKDriver(config, sdk=sdk)
            with self.subTest(expected=expected), self.assertRaisesRegex(DriverError, expected):
                driver.prepare_readonly()
            self.assertEqual(sdk.create_calls, 0)
            self.assertEqual(sdk.account_calls, 0)

    def test_wrong_sdk_chain_id_and_wrong_consensus_addresses_are_not_accepted(self):
        sdk = FakeSDK()
        sdk.chains.testnet_asimov.id = 1
        driver = configured_driver(sdk)
        with self.assertRaisesRegex(DriverError, "sdk_network_chain_id_unqualified"):
            driver.prepare()
        self.assertEqual(sdk.create_calls, 0)

        class IdentityClient:
            def __init__(self, chain_id, genesis_hash=None, manager=None, wrong_code=False):
                self.w3 = SimpleNamespace()
                self.w3.to_hex = lambda value: "0x" + bytes(value).hex()
                self.w3.eth = SimpleNamespace(
                    chain_id=chain_id,
                    get_code=lambda _address: b"" if wrong_code else b"contract-code",
                    get_block=lambda _number: {"hash": bytes.fromhex(genesis_hash[2:])},
                    contract=lambda **_kwargs: SimpleNamespace(
                        functions=SimpleNamespace(addressManager=lambda: SimpleNamespace(call=lambda: manager))
                    ),
                )

        expected_bradbury = SimpleNamespace(
            id=4221,
            consensus_main_contract={"address": "0x0112bf6e83497965a5fdd6dad1e447a6e004271d", "abi": ["main"]},
            consensus_data_contract={"address": "0x85d7bf947a512fc640c75327a780c90847267697", "abi": ["data"]},
        )
        identity_client = IdentityClient(4221, "0x17d89764d4f4f963158fc4c5e8f2cb670c2837740b35f37081c12860c3df97e4",
                                         "0x3f71ae080bd6270a91dbcc7960d88797828538e0")
        self.assertTrue(GenLayerSDKDriver._verify_builtin_network_identity(
            identity_client, expected_bradbury, "testnet_bradbury", "https://node.example/api"))
        wrong_address_chain = SimpleNamespace(
            id=4221,
            consensus_main_contract={"address": "0x" + "c" * 40, "abi": ["main"]},
            consensus_data_contract=expected_bradbury.consensus_data_contract,
        )
        self.assertFalse(GenLayerSDKDriver._verify_builtin_network_identity(
            identity_client, wrong_address_chain, "testnet_bradbury", "https://node.example/api"))
        wrong_manager = IdentityClient(4221, "0x17d89764d4f4f963158fc4c5e8f2cb670c2837740b35f37081c12860c3df97e4",
                                       "0x" + "c" * 40)
        self.assertFalse(GenLayerSDKDriver._verify_builtin_network_identity(
            wrong_manager, expected_bradbury, "testnet_bradbury", "https://node.example/api"))
        wrong_code = IdentityClient(4221, "0x17d89764d4f4f963158fc4c5e8f2cb670c2837740b35f37081c12860c3df97e4",
                                    "0x3f71ae080bd6270a91dbcc7960d88797828538e0", wrong_code=True)
        self.assertFalse(GenLayerSDKDriver._verify_builtin_network_identity(
            wrong_code, expected_bradbury, "testnet_bradbury", "https://node.example/api"))

    def test_preflight_runs_exact_snapshot_no_broadcast_and_without_signing_capability(self):
        runtime_probe = json.loads(GenLayerSDKDriver._runtime_probe_submission())
        runtime_probe_base = {key: value for key, value in runtime_probe.items() if key != "snapshot_sha256"}
        canonical_probe = json.dumps(runtime_probe_base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(runtime_probe["snapshot_sha256"], hashlib.sha256(canonical_probe.encode("utf-8")).hexdigest())
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk = FakeSDK()
            profile_path, profile_hash, _ = write_fee_profile(Path(temp_dir))
            signer = "0x" + "b" * 40
            driver = GenLayerSDKDriver(
                DriverConfig(network="testnet_asimov", private_key="", broadcast_authorized=False,
                             signer_address=signer, fee_profile_path=profile_path,
                             fee_profile_sha256=profile_hash, max_total_cost_wei=10**18,
                             contract_path=FIXTURE),
                network_identity_verifier=lambda client, chain, target: client.w3.eth.chain_id == chain.id,
                sdk=sdk,
            )
            exact_input = self.submission()
            verified = _make_verified_submission(
                exact_input, "10000000-0000-4000-8000-000000000001",
                "10000000-0000-4000-8000-000000000001", "a" * 64,
                "mandatory_gate_optional_advisory_v1")

            root = ModuleType("genlayer_py")
            root.__path__ = []
            contracts = ModuleType("genlayer_py.contracts")
            contracts.__path__ = []
            abi = ModuleType("genlayer_py.abi")
            abi.__path__ = []
            calldata_module = ModuleType("genlayer_py.abi.calldata")
            utils_module = ModuleType("genlayer_py.contracts.utils")
            transactions_module = ModuleType("genlayer_py.abi.transactions")
            encoded_calls = []
            calldata_module.encode = lambda value: json.dumps(value, sort_keys=True).encode("utf-8")
            utils_module.make_calldata_object = lambda **kwargs: kwargs
            def serialize(value):
                encoded_calls.append(value)
                return "0x" + hashlib.sha256(repr(value).encode()).hexdigest()
            transactions_module.serialize = serialize
            with patch.dict("sys.modules", {
                "genlayer_py": root,
                "genlayer_py.contracts": contracts,
                "genlayer_py.abi": abi,
                "genlayer_py.abi.calldata": calldata_module,
                "genlayer_py.contracts.utils": utils_module,
                "genlayer_py.abi.transactions": transactions_module,
            }):
                report = driver.preflight(
                    verified, snapshot_id=verified.snapshot_id,
                    snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                )
            self.assertEqual(report["snapshot_id"], verified.snapshot_id)
            self.assertEqual(report["snapshot_sha256"], verified.snapshot_sha256)
            self.assertEqual(report["contract_input_sha256"], hashlib.sha256(exact_input.encode()).hexdigest())
            self.assertEqual(report["preflight_outcome"], "ready_for_human_review")
            self.assertFalse(report["authorization"]["this_preflight_signing_performed"])
            self.assertFalse(report["authorization"]["this_preflight_broadcast"])
            self.assertFalse(report["authorization"]["signing_capability_enabled_for_preflight"])
            self.assertFalse(report["authorization"]["broadcast_capability_enabled_for_preflight"])
            self.assertEqual(sdk.account_calls, 0)
            self.assertFalse(sdk.client.provider.sent)
            self.assertFalse(any(call[0] in {"deploy", "write"} for call in sdk.client.calls))
            probes = [params[0] for method, params in sdk.client.provider.rpc_calls if method == "gen_call"]
            self.assertEqual(len(probes), 2)
            self.assertTrue(all(call["type"] == "deploy" and call["to"] == "0x0000000000000000000000000000000000000000" for call in probes))
            self.assertEqual(probes[-1]["from"], signer)
            self.assertEqual(len(encoded_calls), 2)

    def test_mutable_repository_runner_is_rejected_before_sdk_initialization(self):
        sdk = FakeSDK()
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            contract_path = Path(temp_dir) / "mutable.py"
            contract_path.write_text('# { "Depends": "py-genlayer:latest" }\n', encoding="utf-8")
            driver = configured_driver(sdk, contract_path=contract_path)
            with self.assertRaisesRegex(DriverError, "mutable_contract_runtime_reference"):
                driver.prepare()
        self.assertEqual(sdk.create_calls, 0)

    def test_false_or_non_boolean_runtime_qualification_never_reaches_sdk(self):
        for verifier_result in (False, None, 1):
            sdk=FakeSDK()
            driver=configured_driver(sdk)
            driver.runtime_qualifier=lambda *_args, result=verifier_result: result
            with self.subTest(verifier_result=verifier_result), self.assertRaisesRegex(DriverError,"runtime_not_qualified"):
                driver.prepare()
            self.assertEqual(sdk.create_calls,0)
            self.assertFalse(any(call[0] in {"deploy","write"} for call in sdk.client.calls))

    def test_missing_target_runtime_or_funding_verifier_fails_closed(self):
        sdk = FakeSDK()
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key=KEY, broadcast_authorized=True,
                         contract_path=FIXTURE), sdk=sdk)
        with self.assertRaisesRegex(DriverError, "fee_profile_or_cost_cap_missing"):
            driver.prepare()
        self.assertEqual(sdk.create_calls, 0)

    def test_query_only_connection_requires_target_identity_verifier(self):
        sdk = FakeSDK()
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key="", contract_path=FIXTURE), sdk=sdk)
        with self.assertRaisesRegex(DriverError, "network_identity_verifier_missing"):
            driver.prepare_readonly()
        self.assertEqual(sdk.create_calls, 0)
        self.assertEqual(sdk.account_calls, 0)
        self.assertFalse(any(call[0] in {"deploy", "write"} for call in sdk.client.calls))

    def test_false_or_non_boolean_identity_verifier_results_never_authorize_writes(self):
        for verifier_result in (False, None, 1):
            sdk=FakeSDK()
            driver=configured_driver(sdk)
            driver.network_identity_verifier=lambda *_args, result=verifier_result: result
            with self.subTest(verifier_result=verifier_result), self.assertRaisesRegex(DriverError,"network_identity_not_verified"):
                driver.prepare()
            self.assertEqual(sdk.create_calls,1)
            self.assertFalse(any(call[0] in {"deploy","write"} for call in sdk.client.calls))

    def test_wrong_chain_id_and_funding_fail_before_any_transaction_method(self):
        for client, funding, expected in (
            (FakeClient(chain_id=1), lambda *_: True, "network_chain_id_mismatch"),
            (FakeClient(), lambda *_: False, "funding_not_verified"),
        ):
            sdk = FakeSDK(client)
            driver = configured_driver(sdk, funding_verifier=funding)
            with self.assertRaisesRegex(DriverError, expected):
                driver.prepare()
            self.assertFalse(any(call[0] in {"deploy", "write"} for call in client.calls))

    def test_uncertain_poll_timeout_never_proceeds_to_evaluation(self):
        sdk = FakeSDK(FakeClient(pending=True))
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        lifecycle = Lifecycle()
        with self.assertRaisesRegex(DriverError, "transaction_poll_timeout"):
            driver._poll(runtime, TX_DEPLOY, lifecycle)
        self.assertIn(("outcome_unknown", "POLL_TIMEOUT"), lifecycle.events)
        self.assertFalse(any(call[0] == "write" for call in sdk.client.calls))

    def test_mismatched_or_missing_transaction_reference_fails_closed(self):
        for transaction, expected in (
            ({"hash": "0x" + "f" * 64, "lifecycle": {"state": "finalized"},
              "tx_execution_result_name": "FINISHED_WITH_RETURN"}, "transaction_reference_mismatch"),
            ({"lifecycle": {"state": "finalized"}, "tx_execution_result_name": "FINISHED_WITH_RETURN"},
             "transaction_reference_mismatch"),
            ({"tx_id": "not-a-hash", "lifecycle": {"state": "finalized"},
              "tx_execution_result_name": "FINISHED_WITH_RETURN"}, "transaction_reference_invalid"),
        ):
            class MismatchedClient(FakeClient):
                def get_transaction(self, *, transaction_hash):
                    self.calls.append(("poll", transaction_hash))
                    return transaction
            driver = configured_driver(FakeSDK(MismatchedClient()))
            runtime = driver.prepare()
            lifecycle = Lifecycle()
            with self.subTest(expected=expected), self.assertRaisesRegex(DriverError, expected):
                driver._poll(runtime, TX_DEPLOY, lifecycle)
            self.assertIn(("outcome_unknown", "TRANSACTION_REFERENCE_" +
                           ("INVALID" if expected.endswith("invalid") else "MISMATCH")), lifecycle.events)

    def test_mismatched_finalized_receipt_is_not_used_for_contract_address(self):
        sdk = FakeSDK()
        sdk.client.receipt_hash = "0x" + "f" * 64
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        verified = _make_verified_submission(
            self.submission(), "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000001", "a" * 64,
            "mandatory_gate_optional_advisory_v1")
        lifecycle = Lifecycle()
        with self.assertRaisesRegex(DriverError, "transaction_reference_mismatch"):
            driver(verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                   snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                   lifecycle=lifecycle, runtime=runtime)
        self.assertFalse(any(call[0] == "write" for call in sdk.client.calls))

    def test_finalized_receipt_must_report_successful_execution(self):
        for status, execution, expected in (
            ("pending", "FINISHED_WITH_RETURN", "finalized_receipt_status_mismatch"),
            ("finalized", "NOT_VOTED", "finalized_receipt_execution_unverified"),
            ("finalized", "FINISHED_WITH_ERROR", "contract_execution_failed"),
        ):
            sdk = FakeSDK()
            sdk.client.receipt_status = status.lower()
            sdk.client.receipt_execution = execution
            driver = configured_driver(sdk)
            runtime = driver.prepare()
            verified = _make_verified_submission(
                self.submission(), "10000000-0000-4000-8000-000000000001",
                "10000000-0000-4000-8000-000000000001", "a" * 64,
                "mandatory_gate_optional_advisory_v1")
            lifecycle = Lifecycle()
            with self.subTest(status=status, execution=execution), self.assertRaisesRegex(DriverError, expected):
                driver(verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                       snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                       lifecycle=lifecycle, runtime=runtime)
            self.assertFalse(any(call[0] == "write" for call in sdk.client.calls))

    def test_finalized_transaction_timeout_and_undetermined_states_are_not_success(self):
        for status, state_name, code in (("canceled", "rejected", "transaction_canceled"),
                                         ("undetermined", "rejected", "transaction_undetermined"),
                                         ("validators_timeout", "timed_out", "validators_timeout"),
                                         ("leader_timeout", "timed_out", "leader_timeout")):
            class TerminalClient(FakeClient):
                def get_transaction(self, *, transaction_hash):
                    return {"tx_id": transaction_hash, "lifecycle": {"state": "decided", "outcome": status},
                            "tx_execution_result_name": "NOT_VOTED"}
            driver = configured_driver(FakeSDK(TerminalClient()))
            runtime = driver.prepare()
            lifecycle = Lifecycle()
            with self.subTest(status=status), self.assertRaisesRegex(DriverError, code):
                driver._poll(runtime, TX_DEPLOY, lifecycle)
            self.assertIn((state_name, code, status), lifecycle.events)

    def test_contract_source_change_after_preflight_blocks_deployment(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".py",
                                         dir=FIXTURE.parent, delete=False) as handle:
            source_path = Path(handle.name)
            original = FIXTURE.read_text(encoding="utf-8")
            handle.write(original)
        try:
            sdk = FakeSDK()
            driver = configured_driver(sdk, contract_path=source_path)
            runtime = driver.prepare()
            source_path.write_text(original + "\n# changed after qualification\n", encoding="utf-8")
            verified = _make_verified_submission(
                self.submission(), "10000000-0000-4000-8000-000000000001",
                "10000000-0000-4000-8000-000000000001", "a" * 64,
                "mandatory_gate_optional_advisory_v1")
            with self.assertRaisesRegex(DriverError, "contract_source_changed_after_preflight"):
                driver(verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                       snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                       lifecycle=Lifecycle(), runtime=runtime)
            self.assertFalse(any(call[0] in {"deploy", "write"} for call in sdk.client.calls))
        finally:
            source_path.unlink(missing_ok=True)

    def test_prepared_runtime_cannot_be_mutated_or_reused_after_config_replacement(self):
        driver = configured_driver()
        runtime = driver.prepare()
        with self.assertRaises(FrozenInstanceError):
            runtime.broadcast_ready = False
        driver.config = DriverConfig(network="testnet_asimov", private_key=KEY,
                                     broadcast_authorized=False, contract_path=FIXTURE)
        self.assertFalse(driver._runtime_was_prepared(runtime))

    def test_runtime_capability_cannot_be_reused_across_driver_instances(self):
        issuing_driver = configured_driver()
        runtime = issuing_driver.prepare()
        other_driver = configured_driver()
        verified = _make_verified_submission(
            self.submission(), "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000001", "a" * 64,
            "mandatory_gate_optional_advisory_v1")
        with self.assertRaisesRegex(DriverError, "runtime_preflight_required"):
            other_driver(verified, request_id=verified.request_id, snapshot_id=verified.snapshot_id,
                         snapshot_sha256=verified.snapshot_sha256, policy_version=verified.policy_version,
                         lifecycle=Lifecycle(), runtime=runtime)

    def test_finalized_execution_error_is_not_read_as_a_result(self):
        sdk = FakeSDK(FakeClient(execution="FINISHED_WITH_ERROR"))
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        lifecycle = Lifecycle()
        with self.assertRaisesRegex(DriverError, "contract_execution_failed"):
            driver._poll(runtime, TX_DEPLOY, lifecycle)
        self.assertIn(("failed", "contract_execution_failed", "finalized"), lifecycle.events)


if __name__ == "__main__":
    unittest.main()
