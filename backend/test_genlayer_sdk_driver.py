"""Offline tests for the pinned SDK driver; every RPC/account object is a fake."""
from __future__ import annotations

import unittest
from pathlib import Path
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
        self.calls = []

    def deploy_contract(self, *, code, account, args):
        self.calls.append(("deploy", code, args))
        return TX_DEPLOY

    def get_transaction(self, *, transaction_hash):
        self.calls.append(("poll", transaction_hash))
        if self.pending:
            return {"status_name": "PENDING", "tx_execution_result_name": "NOT_VOTED"}
        return {"status_name": "FINALIZED", "tx_execution_result_name": self.execution}

    def wait_for_transaction_receipt(self, *, transaction_hash, status, interval, retries):
        self.calls.append(("receipt", transaction_hash, status, interval, retries))
        return {"tx_data_decoded": {"contract_address": CONTRACT}}

    def write_contract(self, *, address, function_name, account, args):
        self.calls.append(("write", address, function_name, args))
        return TX_EVAL

    def read_contract(self, *, address, function_name, args, account):
        self.calls.append(("read", address, function_name, args))
        return '{"fake_result":true}'


class FakeSDK:
    version = "0.18.0"
    chains = SimpleNamespace(testnet_asimov=FakeChain())
    TransactionStatus = SimpleNamespace(FINALIZED="FINALIZED")
    ExecutionResult = SimpleNamespace(FINISHED_WITH_RETURN="FINISHED_WITH_RETURN",
                                      FINISHED_WITH_ERROR="FINISHED_WITH_ERROR")

    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.create_calls = 0

    def create_account(self, *, account_private_key):
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


def configured_driver(sdk=None, *, funding_verifier=None):
    return GenLayerSDKDriver(
        DriverConfig(network="testnet_asimov", private_key=KEY, broadcast_authorized=True,
                     contract_path=FIXTURE, poll_interval_seconds=0.001, max_polls=2),
        runtime_qualifier=lambda dependency, network: dependency == "qualified-fixture" and network == "testnet_asimov",
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
            DriverConfig(network="testnet_asimov", private_key=KEY, contract_path=FIXTURE), sdk=sdk)
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
            DriverConfig(network="testnet_asimov", private_key=KEY, contract_path=FIXTURE), sdk=sdk)
        runtime = driver.prepare_readonly()
        self.assertFalse(runtime.broadcast_ready)
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
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key=KEY, broadcast_authorized=True),
            runtime_qualifier=lambda *_: True, funding_verifier=lambda *_: True, sdk=sdk)
        with self.assertRaisesRegex(DriverError, "mutable_contract_runtime_reference"):
            driver.prepare()
        self.assertEqual(sdk.create_calls, 0)

    def test_missing_target_runtime_or_funding_verifier_fails_closed(self):
        sdk = FakeSDK()
        driver = GenLayerSDKDriver(
            DriverConfig(network="testnet_asimov", private_key=KEY, broadcast_authorized=True,
                         contract_path=FIXTURE), sdk=sdk)
        with self.assertRaisesRegex(DriverError, "runtime_or_funding_qualification_missing"):
            driver.prepare()
        self.assertEqual(sdk.create_calls, 0)

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

    def test_finalized_execution_error_is_not_read_as_a_result(self):
        sdk = FakeSDK(FakeClient(execution="FINISHED_WITH_ERROR"))
        driver = configured_driver(sdk)
        runtime = driver.prepare()
        lifecycle = Lifecycle()
        with self.assertRaisesRegex(DriverError, "contract_execution_failed"):
            driver._poll(runtime, TX_DEPLOY, lifecycle)
        self.assertIn(("failed", "contract_execution_failed", "FINALIZED"), lifecycle.events)


if __name__ == "__main__":
    unittest.main()
