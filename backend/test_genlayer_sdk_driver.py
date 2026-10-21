"""Offline tests for the pinned SDK driver; every RPC/account object is a fake."""
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
from types import SimpleNamespace

from backend.evaluation_adapter import _make_verified_submission
from backend.genlayer_sdk_driver import DriverConfig, DriverError, GenLayerSDKDriver

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/genlayer_direct/contracts/sdk_driver_test_fixture.py"
TX_DEPLOY = "0x" + "1" * 64
TX_EVAL = "0x" + "2" * 64
CONTRACT = "0x" + "a" * 40
KEY = "1" * 64


class FakeChain:
    id = 4221
    rpc_urls = {"default": {"http": ["https://sdk.test.invalid"]}}


class FakeClient:
    def __init__(self, *, chain_id=4221, execution="FINISHED_WITH_RETURN", pending=False):
        self.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=chain_id))
        self.execution = execution
        self.pending = pending
        self.receipt_hash = None
        self.receipt_status = "finalized"
        self.receipt_execution = "FINISHED_WITH_RETURN"
        self.calls = []

    def deploy_contract(self, *, code, account, args):
        self.calls.append(("deploy", code, args))
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

    def write_contract(self, *, address, function_name, account, args):
        self.calls.append(("write", address, function_name, args))
        return TX_EVAL

    def read_contract(self, *, address, function_name, args, account):
        self.calls.append(("read", address, function_name, args))
        return '{"fake_result":true}'


class FakeSDK:
    version = "0.19.0rc2"
    chains = SimpleNamespace(testnet_asimov=FakeChain())
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
        assert endpoint == "https://sdk.test.invalid"
        return self.client


class Lifecycle:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def record(*args):
            self.events.append((name, *args))
        return record


def configured_driver(sdk=None, *, funding_verifier=None, contract_path=FIXTURE):
    return GenLayerSDKDriver(
        DriverConfig(network="testnet_asimov", private_key=KEY, broadcast_authorized=True,
                     contract_path=contract_path, poll_interval_seconds=0.001, max_polls=2),
        runtime_qualifier=lambda dependency, network: dependency == "qualified-fixture" and network == "testnet_asimov",
        network_identity_verifier=lambda client, chain, network: network == "testnet_asimov" and client.w3.eth.chain_id == chain.id,
        funding_verifier=funding_verifier or (lambda client, account, network: True),
        sdk=sdk or FakeSDK(), sleep=lambda _seconds: None,
    )


class GenLayerSDKDriverTests(unittest.TestCase):
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
        self.assertEqual(result["result_json"], '{"fake_result":true}')
        self.assertIn(("deployment_submitted", TX_DEPLOY), lifecycle.events)
        self.assertIn(("evaluation_submitted", TX_EVAL), lifecycle.events)
        self.assertIn(("finalized",), lifecycle.events)

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
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key="", contract_path=FIXTURE),
            network_identity_verifier=lambda client, chain, network: client.w3.eth.chain_id == chain.id,
            sdk=sdk)
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

    def test_broadcast_requires_explicit_authorization_before_client_or_rpc(self):
        sdk = FakeSDK()
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key=KEY, contract_path=FIXTURE),
            runtime_qualifier=lambda *_: True, funding_verifier=lambda *_: True, sdk=sdk)
        with self.assertRaisesRegex(DriverError, "broadcast_not_authorized"):
            driver.prepare()
        self.assertEqual(sdk.create_calls, 0)

    def test_mutable_repository_runner_is_rejected_before_sdk_initialization(self):
        sdk = FakeSDK()
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            contract_path = Path(temp_dir) / "mutable.py"
            contract_path.write_text('# { "Depends": "py-genlayer:latest" }\n', encoding="utf-8")
            driver = GenLayerSDKDriver(
                DriverConfig(network="testnet_asimov", private_key=KEY, broadcast_authorized=True,
                             contract_path=contract_path),
                runtime_qualifier=lambda *_: True,
                network_identity_verifier=lambda *_: True,
                funding_verifier=lambda *_: True, sdk=sdk)
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
        with self.assertRaisesRegex(DriverError, "runtime_or_funding_qualification_missing"):
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
        self.assertIn(("outcome_unknown",), lifecycle.events)
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
        for status, code in (("canceled", "transaction_canceled"),
                             ("undetermined", "transaction_undetermined"),
                             ("validators_timeout", "validators_timeout"),
                             ("leader_timeout", "leader_timeout")):
            class TerminalClient(FakeClient):
                def get_transaction(self, *, transaction_hash):
                    return {"tx_id": transaction_hash, "lifecycle": {"state": "decided", "outcome": status},
                            "tx_execution_result_name": "NOT_VOTED"}
            driver = configured_driver(FakeSDK(TerminalClient()))
            runtime = driver.prepare()
            lifecycle = Lifecycle()
            with self.subTest(status=status), self.assertRaisesRegex(DriverError, code):
                driver._poll(runtime, TX_DEPLOY, lifecycle)
            self.assertIn(("failed", code, status), lifecycle.events)

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
