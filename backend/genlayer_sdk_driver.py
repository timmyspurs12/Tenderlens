"""Pinned GenLayer SDK v0.19.0-rc.2 transaction driver.

This is a real SDK adapter, not a mock. It is fail-closed by default: callers must
explicitly authorize broadcast and provide target-runtime qualification and a
network-specific funding verifier. Unit tests inject SDK-shaped fakes and never
open a socket. Deployment is per evaluation because the contract constructor stores
one immutable submission; there is no reusable configured contract address.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend.evaluation_adapter import SubmissionLifecycle, is_adapter_verified_submission

SDK_VERSION = "0.19.0rc2"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = REPOSITORY_ROOT / "tests/genlayer_direct/contracts/procurement_evaluation.py"
NETWORK_NAMES = {"studionet", "testnet_asimov", "testnet_bradbury"}
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class DriverError(RuntimeError):
    """Sanitized integration error; never contains calldata, documents, or keys."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DriverConfig:
    network: str
    private_key: str = field(repr=False)
    broadcast_authorized: bool = False
    rpc_url: str | None = None
    contract_path: Path = DEFAULT_CONTRACT
    poll_interval_seconds: float = 1.0
    max_polls: int = 300

    @classmethod
    def from_environment(cls, environ=None, *, contract_path: Path = DEFAULT_CONTRACT):
        env = os.environ if environ is None else environ
        return cls(
            network=env.get("TENDERLENS_GENLAYER_NETWORK", ""),
            private_key=env.get("TENDERLENS_GENLAYER_PRIVATE_KEY", ""),
            broadcast_authorized=env.get("TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED") == "1",
            rpc_url=env.get("TENDERLENS_GENLAYER_RPC_URL") or None,
            contract_path=contract_path,
        )


@dataclass(frozen=True)
class Runtime:
    client: Any = field(repr=False)
    account: Any = field(repr=False)
    chain: Any = field(repr=False)
    chain_id: int
    network: str
    broadcast_ready: bool
    sdk: Any = field(repr=False)
    _gate_token: object = field(repr=False, compare=False)
    _config_token: DriverConfig = field(repr=False, compare=False)
    contract_source_sha256: str | None = None


class GenLayerSDKDriver:
    """Deploy, execute, poll and decode through the verified genlayer-py API.

    ``network_identity_verifier`` must confirm the selected remote deployment in
    both read-only and broadcast contexts. ``runtime_qualifier`` must confirm the
    exact dependency reference on the target GenVM/WASM environment. For broadcast,
    ``funding_verifier`` must assess target-specific expected deployment and execution
    costs. The SDK exposes fee-estimation helpers, but each target and contract needs
    a current, measured quote and an explicit funding policy; this driver does not infer
    budgets or treat an SDK estimate as approval. All three are injected gates, not
    built-in evidence. No network call is made by construction or ordinary tests.
    """

    def __init__(self, config: DriverConfig, *, runtime_qualifier: Callable[[str, str], bool] | None = None,
                 network_identity_verifier: Callable[[Any, Any, str], bool] | None = None,
                 funding_verifier: Callable[[Any, Any, str], bool] | None = None, sdk: Any = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.runtime_qualifier = runtime_qualifier
        self.network_identity_verifier = network_identity_verifier
        self.funding_verifier = funding_verifier
        self._sdk = sdk
        self._sleep = sleep
        self._runtime_gate_token = object()

    @classmethod
    def from_environment(cls, *, environ=None, runtime_qualifier=None, network_identity_verifier=None,
                         funding_verifier=None, sdk=None):
        return cls(DriverConfig.from_environment(environ), runtime_qualifier=runtime_qualifier,
                   network_identity_verifier=network_identity_verifier,
                   funding_verifier=funding_verifier, sdk=sdk)

    def _load_sdk(self):
        if self._sdk is not None:
            return self._sdk
        try:
            package_version = importlib.metadata.version("genlayer-py")
            root = importlib.import_module("genlayer_py")
            chains = importlib.import_module("genlayer_py.chains")
            types = importlib.import_module("genlayer_py.types")
        except Exception:
            raise DriverError("pinned_sdk_unavailable") from None
        if package_version != SDK_VERSION:
            raise DriverError("pinned_sdk_version_mismatch")
        self._sdk = type("SDKBindings", (), {
            "version": package_version,
            "create_client": root.create_client,
            "create_account": root.create_account,
            "chains": chains,
            "ExecutionResult": types.ExecutionResult,
        })()
        return self._sdk

    def _static_preflight(self, *, require_broadcast: bool):
        cfg = self.config
        if cfg.network not in NETWORK_NAMES:
            raise DriverError("unsupported_network")
        if require_broadcast and (not isinstance(cfg.private_key, str)
                                  or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", cfg.private_key)):
            raise DriverError("invalid_account_configuration")
        if cfg.poll_interval_seconds <= 0 or cfg.max_polls < 1:
            raise DriverError("invalid_poll_configuration")
        if require_broadcast:
            if not cfg.broadcast_authorized:
                raise DriverError("broadcast_not_authorized")
            if not callable(self.runtime_qualifier) or not callable(self.funding_verifier):
                raise DriverError("runtime_or_funding_qualification_missing")
        elif not callable(self.network_identity_verifier):
            raise DriverError("network_identity_verifier_missing")
        if require_broadcast and not callable(self.network_identity_verifier):
            raise DriverError("network_identity_verifier_missing")
        if not require_broadcast:
            return None
        try:
            source_path = Path(cfg.contract_path).resolve(strict=True)
            source_path.relative_to(REPOSITORY_ROOT)
            source = source_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            raise DriverError("contract_source_unavailable") from None
        match = re.search(r"\"Depends\"\s*:\s*\"py-genlayer:([^\"]+)\"", source)
        if not match:
            raise DriverError("contract_runtime_reference_missing")
        dependency = match.group(1)
        if dependency.strip().lower() in {"latest", "stable", "main", "master", ""}:
            raise DriverError("mutable_contract_runtime_reference")
        try:
            qualified = self.runtime_qualifier(dependency, cfg.network)
        except Exception:
            raise DriverError("runtime_qualification_failed") from None
        if qualified is not True:
            raise DriverError("runtime_not_qualified")
        return source

    def _connect(self, *, broadcast_ready: bool, contract_source: str | None = None) -> Runtime:
        sdk = self._load_sdk()
        if getattr(sdk, "version", SDK_VERSION) != SDK_VERSION:
            raise DriverError("pinned_sdk_version_mismatch")
        try:
            chain = copy.deepcopy(getattr(sdk.chains, self.config.network))
            chain_id = int(chain.id)
            endpoint = chain.rpc_urls["default"]["http"][0]
            if self.config.rpc_url is not None and self.config.rpc_url != endpoint:
                raise DriverError("rpc_endpoint_not_pinned")
            # The pinned SDK accepts account=None. Query-only recovery therefore
            # does not need to derive/import a signer from TENDERLENS_GENLAYER_PRIVATE_KEY.
            account = sdk.create_account(account_private_key=self.config.private_key) if broadcast_ready else None
            # SDK client initialization may perform read-only RPC calls. It is only
            # invoked by an explicit submit/reconcile operation, never by module import.
            client = sdk.create_client(chain=chain, endpoint=endpoint, account=account)
            observed_chain_id = int(client.w3.eth.chain_id)
        except DriverError:
            raise
        except Exception:
            raise DriverError("network_preflight_failed") from None
        if observed_chain_id != chain_id:
            raise DriverError("network_chain_id_mismatch")
        if not callable(self.network_identity_verifier):
            raise DriverError("network_identity_verifier_missing")
        try:
            identity_verified = self.network_identity_verifier(client, chain, self.config.network)
        except Exception:
            raise DriverError("network_identity_verification_failed") from None
        if identity_verified is not True:
            raise DriverError("network_identity_not_verified")
        if broadcast_ready:
            try:
                funded = self.funding_verifier(client, account, self.config.network)
            except Exception:
                raise DriverError("funding_verification_failed") from None
            if funded is not True:
                raise DriverError("funding_not_verified")
        return Runtime(client=client, account=account, chain=chain, chain_id=chain_id,
                       network=self.config.network, broadcast_ready=broadcast_ready, sdk=sdk,
                       _gate_token=self._runtime_gate_token, _config_token=self.config,
                       contract_source_sha256=(hashlib.sha256(contract_source.encode("utf-8")).hexdigest()
                                               if contract_source is not None else None))

    def prepare(self) -> Runtime:
        """Validate broadcast prerequisites, then initialize/check SDK and funding."""
        source = self._static_preflight(require_broadcast=True)
        return self._connect(broadcast_ready=True, contract_source=source)

    def prepare_readonly(self) -> Runtime:
        """Connect for query/recovery without a signer, broadcast, or funding gate.

        The target identity callback remains mandatory: read-only queries against a
        misidentified endpoint can still corrupt local transaction/result association.
        """
        self._static_preflight(require_broadcast=False)
        return self._connect(broadcast_ready=False)

    def _runtime_was_prepared(self, runtime: Runtime) -> bool:
        return (isinstance(runtime, Runtime)
                and runtime._gate_token is self._runtime_gate_token
                and runtime._config_token is self.config
                and runtime.network == self.config.network)

    @staticmethod
    def _enum_text(value):
        return getattr(value, "value", value) if value is not None else None

    @classmethod
    def _verify_finalized_success(cls, receipt: Any, lifecycle: SubmissionLifecycle) -> None:
        if not isinstance(receipt, dict):
            lifecycle.outcome_unknown("FINALIZED_RECEIPT_UNVERIFIED")
            raise DriverError("finalized_receipt_unverified")
        tx_lifecycle = receipt.get("lifecycle")
        state = tx_lifecycle.get("state") if isinstance(tx_lifecycle, dict) else None
        execution = cls._enum_text(receipt.get("tx_execution_result_name"))
        if state != "finalized":
            lifecycle.outcome_unknown("FINALIZED_RECEIPT_STATUS_MISMATCH")
            raise DriverError("finalized_receipt_status_mismatch")
        if execution == "FINISHED_WITH_ERROR":
            lifecycle.failed("contract_execution_failed", "finalized")
            raise DriverError("contract_execution_failed")
        if execution != "FINISHED_WITH_RETURN":
            lifecycle.outcome_unknown("FINALIZED_RECEIPT_EXECUTION_UNVERIFIED")
            raise DriverError("finalized_receipt_execution_unverified")

    @staticmethod
    def _verify_transaction_reference(transaction: Any, expected_hash: str,
                                      lifecycle: SubmissionLifecycle) -> None:
        """Require the pinned SDK's returned hash/tx_id to match the queried ID."""
        if not isinstance(transaction, dict):
            lifecycle.outcome_unknown("TRANSACTION_REFERENCE_UNVERIFIED")
            raise DriverError("transaction_reference_unverified")
        references = []
        for field_name in ("hash", "tx_id"):
            if field_name in transaction and transaction[field_name] is not None:
                value = transaction[field_name]
                if not isinstance(value, str) or not TX_HASH.fullmatch(value):
                    lifecycle.outcome_unknown("TRANSACTION_REFERENCE_INVALID")
                    raise DriverError("transaction_reference_invalid")
                references.append(value.lower())
        if not references or any(value != expected_hash.lower() for value in references):
            lifecycle.outcome_unknown("TRANSACTION_REFERENCE_MISMATCH")
            raise DriverError("transaction_reference_mismatch")

    def _poll(self, runtime: Runtime, tx_hash: str, lifecycle: SubmissionLifecycle):
        for _ in range(self.config.max_polls):
            try:
                tx = runtime.client.get_transaction(transaction_hash=tx_hash)
            except Exception:
                lifecycle.pending("QUERY_ERROR")
                self._sleep(self.config.poll_interval_seconds)
                continue
            if tx is None:
                lifecycle.pending("NOT_FOUND")
                self._sleep(self.config.poll_interval_seconds)
                continue
            self._verify_transaction_reference(tx, tx_hash, lifecycle)
            tx_lifecycle = tx.get("lifecycle")
            state = tx_lifecycle.get("state") if isinstance(tx_lifecycle, dict) else None
            outcome = tx_lifecycle.get("outcome") if isinstance(tx_lifecycle, dict) else None
            execution = self._enum_text(tx.get("tx_execution_result_name"))
            if state == "finalized":
                if execution == "FINISHED_WITH_RETURN":
                    return tx
                if execution == "FINISHED_WITH_ERROR":
                    lifecycle.failed("contract_execution_failed", state)
                    raise DriverError("contract_execution_failed")
                lifecycle.outcome_unknown(state)
                raise DriverError("finalized_execution_result_unrecognized")
            terminal_failures = {
                "canceled": "transaction_canceled",
                "undetermined": "transaction_undetermined",
                "validators_timeout": "validators_timeout",
                "leader_timeout": "leader_timeout",
            }
            terminal_value = state if state in terminal_failures else outcome
            if terminal_value in terminal_failures:
                code = terminal_failures[terminal_value]
                lifecycle.failed(code, terminal_value)
                raise DriverError(code)
            lifecycle.pending(state or "UNRECOGNIZED")
            self._sleep(self.config.poll_interval_seconds)
        lifecycle.outcome_unknown()
        raise DriverError("transaction_poll_timeout")

    @staticmethod
    def _contract_address(runtime: Runtime, receipt: dict[str, Any]) -> str:
        # These are the two result shapes documented by the pinned SDK's own e2e
        # examples: Studio/local-style `data` and testnet `tx_data_decoded`.
        if runtime.network == "studionet":
            value = (receipt.get("data") or {}).get("contract_address")
        else:
            value = (receipt.get("tx_data_decoded") or {}).get("contract_address")
        if not isinstance(value, str) or not ADDRESS.fullmatch(value):
            raise DriverError("deployment_receipt_contract_address_missing")
        return value.lower()

    def __call__(self, verified_input: Any, *, request_id: str, snapshot_id: str,
                 snapshot_sha256: str, policy_version: str, lifecycle: SubmissionLifecycle,
                 runtime: Runtime | None = None):
        if not is_adapter_verified_submission(verified_input):
            raise DriverError("adapter_verified_snapshot_required")
        if (verified_input.request_id != request_id or verified_input.snapshot_id != snapshot_id
                or verified_input.snapshot_sha256 != snapshot_sha256
                or verified_input.policy_version != policy_version):
            raise DriverError("verified_submission_identity_mismatch")
        if runtime is None or not self._runtime_was_prepared(runtime):
            raise DriverError("runtime_preflight_required")
        if not runtime.broadcast_ready:
            raise DriverError("broadcast_preflight_required")
        submission_json = verified_input.contract_input_json
        try:
            submission = json.loads(submission_json)
        except (TypeError, ValueError):
            raise DriverError("verified_submission_json_invalid") from None
        if (not isinstance(submission, dict) or submission.get("snapshot_id") != snapshot_id
                or submission.get("snapshot_sha256") != snapshot_sha256
                or submission.get("aggregation_policy") != policy_version):
            raise DriverError("verified_submission_identity_mismatch")
        try:
            source = Path(self.config.contract_path).read_text(encoding="utf-8")
        except OSError:
            raise DriverError("contract_source_unavailable") from None
        current_source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if (runtime.contract_source_sha256 is None
                or current_source_hash != runtime.contract_source_sha256):
            raise DriverError("contract_source_changed_after_preflight")
        lifecycle.record_network(runtime.network, runtime.chain_id)
        lifecycle.deployment_started()
        try:
            deployment_hash = runtime.client.deploy_contract(code=source, account=runtime.account,
                                                               args=[submission_json])
        except Exception:
            lifecycle.outcome_unknown()
            raise DriverError("deployment_submission_outcome_unknown") from None
        if not isinstance(deployment_hash, str) or not TX_HASH.fullmatch(deployment_hash):
            lifecycle.outcome_unknown()
            raise DriverError("deployment_transaction_reference_invalid")
        lifecycle.deployment_submitted(deployment_hash)
        self._poll(runtime, deployment_hash, lifecycle)
        try:
            deployed = runtime.client.wait_for_transaction_receipt(
                transaction_hash=deployment_hash,
                wait_until="finalized",
                interval=0, retries=1,
            )
        except Exception:
            lifecycle.outcome_unknown("FINALIZED_RECEIPT_UNAVAILABLE")
            raise DriverError("deployment_receipt_unavailable") from None
        self._verify_transaction_reference(deployed, deployment_hash, lifecycle)
        self._verify_finalized_success(deployed, lifecycle)
        contract_address = self._contract_address(runtime, deployed)
        lifecycle.deployment_finalized(contract_address)

        lifecycle.evaluation_started()
        try:
            evaluation_hash = runtime.client.write_contract(
                address=contract_address, function_name="evaluate", account=runtime.account, args=[])
        except Exception:
            lifecycle.outcome_unknown()
            raise DriverError("evaluation_submission_outcome_unknown") from None
        if not isinstance(evaluation_hash, str) or not TX_HASH.fullmatch(evaluation_hash):
            lifecycle.outcome_unknown()
            raise DriverError("evaluation_transaction_reference_invalid")
        lifecycle.evaluation_submitted(evaluation_hash)
        self._poll(runtime, evaluation_hash, lifecycle)
        lifecycle.finalized()
        try:
            result_json = runtime.client.read_contract(address=contract_address, function_name="get_result",
                                                       args=[], account=runtime.account)
        except Exception:
            raise DriverError("finalized_result_query_failed") from None
        if not isinstance(result_json, str):
            raise DriverError("finalized_result_decode_invalid")
        return {"contract_address": contract_address, "deployment_tx_hash": deployment_hash.lower(),
                "evaluation_tx_hash": evaluation_hash.lower(), "result_json": result_json,
                "result_reference": evaluation_hash.lower()}

    def reconcile(self, adapter, request_id: str, *, runtime: Runtime | None = None, resume: bool = False):
        """Query durable hashes; broadcasting the next safe phase requires ``resume=True``."""
        if runtime is None:
            runtime = self.prepare() if resume else self.prepare_readonly()
        if not self._runtime_was_prepared(runtime):
            raise DriverError("runtime_preflight_required")
        if resume and not runtime.broadcast_ready:
            raise DriverError("broadcast_preflight_required")
        request = adapter.get_request(request_id)
        state = request["lifecycle_state"]
        if state == "completed":
            return request
        lifecycle = adapter.lifecycle(request_id)
        deployment_hash = request.get("deployment_tx_hash")
        evaluation_hash = request.get("evaluation_tx_hash")
        if evaluation_hash:
            self._poll(runtime, evaluation_hash, lifecycle)
            lifecycle.finalized()
            contract_address = request.get("contract_address")
            try:
                result_json = runtime.client.read_contract(address=contract_address, function_name="get_result",
                                                           args=[], account=runtime.account)
            except Exception:
                raise DriverError("finalized_result_query_failed") from None
            if not isinstance(result_json, str):
                raise DriverError("finalized_result_decode_invalid")
            return adapter.accept_result(request_id, contract_address, evaluation_hash, result_json, evaluation_hash)

        if state in {"evaluation_submission_in_progress"} or (request.get("contract_address") and state != "deployment_finalized"):
            lifecycle.outcome_unknown(request.get("last_network_status"))
            raise DriverError("evaluation_submission_outcome_unknown_no_hash")
        if not request.get("contract_address"):
            if not deployment_hash:
                raise DriverError("no_persisted_transaction_for_reconciliation")
            deployed = self._poll(runtime, deployment_hash, lifecycle)
            try:
                receipt = runtime.client.wait_for_transaction_receipt(
                    transaction_hash=deployment_hash, wait_until="finalized",
                    interval=0, retries=1,
                )
            except Exception:
                lifecycle.outcome_unknown("FINALIZED_RECEIPT_UNAVAILABLE")
                raise DriverError("deployment_receipt_unavailable") from None
            self._verify_transaction_reference(receipt, deployment_hash, lifecycle)
            self._verify_finalized_success(receipt, lifecycle)
            contract_address = self._contract_address(runtime, receipt)
            lifecycle.deployment_finalized(contract_address)
        else:
            contract_address = request["contract_address"]
            if state != "deployment_finalized":
                raise DriverError("evaluation_submission_outcome_unknown_no_hash")
        if not resume:
            return adapter.get_request(request_id)

        # A finalized deployment with no evaluation-attempt marker is safe to advance.
        # Persist the in-progress marker before the SDK call; if it crashes before the
        # hash is stored, recovery refuses to resubmit and requires operator action.
        lifecycle.evaluation_started()
        try:
            evaluation_hash = runtime.client.write_contract(
                address=contract_address, function_name="evaluate", account=runtime.account, args=[])
        except Exception:
            lifecycle.outcome_unknown()
            raise DriverError("evaluation_submission_outcome_unknown") from None
        if not isinstance(evaluation_hash, str) or not TX_HASH.fullmatch(evaluation_hash):
            lifecycle.outcome_unknown()
            raise DriverError("evaluation_transaction_reference_invalid")
        lifecycle.evaluation_submitted(evaluation_hash)
        self._poll(runtime, evaluation_hash, lifecycle)
        lifecycle.finalized()
        try:
            result_json = runtime.client.read_contract(address=contract_address, function_name="get_result",
                                                       args=[], account=runtime.account)
        except Exception:
            raise DriverError("finalized_result_query_failed") from None
        if not isinstance(result_json, str):
            raise DriverError("finalized_result_decode_invalid")
        return adapter.accept_result(request_id, contract_address, evaluation_hash, result_json, evaluation_hash)
