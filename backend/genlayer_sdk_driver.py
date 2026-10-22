"""Pinned GenLayer SDK v0.19.0-rc.2 transaction driver.

The production path fails closed on target identity, runtime execution, measured
fee-profile, signer, funding, explicit scoped authorization, and durable broadcast
journal evidence. Offline tests use SDK/provider/signing doubles and block sockets.
Deployment is per evaluation because the contract constructor stores one immutable
submission; there is no reusable configured application-contract address.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable

from backend.evaluation_adapter import SubmissionLifecycle, is_adapter_verified_submission

SDK_VERSION = "0.19.0rc2"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = REPOSITORY_ROOT / "tests/genlayer_direct/contracts/procurement_evaluation.py"
NETWORK_NAMES = {"studionet", "studio_devnet", "testnet_asimov", "testnet_bradbury"}
EXPECTED_CHAIN_IDS = {"studionet": 61999, "studio_devnet": 61997, "testnet_asimov": 4221, "testnet_bradbury": 4221}
STUDIO_ENDPOINTS = {
    "studionet": "https://studio.genlayer.com/api",
    "studio_devnet": "https://studio-dev.genlayer.com/api",
}
SDK_DEFAULT_ENDPOINTS = {
    "testnet_asimov": "https://rpc-asimov.genlayer.com",
}
MAX_FEE_PROFILE_AGE = timedelta(days=7)
BRADBURY_NODE_ENDPOINT = "https://rpc-bradbury.genlayer.com"
BRADBURY_GENESIS_HASH = "0x17d89764d4f4f963158fc4c5e8f2cb670c2837740b35f37081c12860c3df97e4"
BRADBURY_ADDRESS_MANAGER = "0x3f71ae080bd6270a91dbcc7960d88797828538e0"
# Current official Bradbury protocol manifest at commit
# 8e570073bd04b1450045dd2433e70383e5971655 (2026-10-19). These mappings are
# checked against the live AddressManager, not the older SDK's static addresses.
BRADBURY_PROTOCOL_REGISTRY = {
    "ConsensusMain": "0x2e2766cbc3e5e61ce93bbcdcbb3eacb82082a019",
    "ConsensusData": "0xc5fa6f82bee53598825d6edf7e3e24613082c75b",
    "Staking": "0xd8fba96c0dbb0257028dbc0107060b958e5c7113",
}
BRADBURY_ADDRESS_MANAGER_ABI = [{
    "type": "function",
    "name": "getAddress",
    "stateMutability": "view",
    "inputs": [{"name": "key", "type": "string"}],
    "outputs": [{"name": "", "type": "address"}],
}]
NETWORK_CONSENSUS_ADDRESSES = {
    "studionet": {
        "main": "0xb7278a61aa25c888815afc32ad3cc52ff24fe575",
        "data": "0x88b0f18613db92bf970ffe264e02496e20a74d16",
    },
    "studio_devnet": {
        "main": "0xb7278a61aa25c888815afc32ad3cc52ff24fe575",
        "data": "0x88b0f18613db92bf970ffe264e02496e20a74d16",
    },
}
STUDIO_CHAIN_IDS = {61997, 61999}

TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class DriverError(RuntimeError):
    """Sanitized integration error; never contains calldata, documents, or keys."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _parse_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_decimal_env(value: str) -> int | None:
    if not isinstance(value, str) or len(value) > 78 or not re.fullmatch(r"[0-9]+", value):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if 0 < parsed <= (2**256 - 1) else None


@dataclass(frozen=True)
class DriverConfig:
    network: str
    private_key: str = field(repr=False)
    broadcast_authorized: bool = False
    rpc_url: str | None = None
    signer_address: str | None = None
    fee_profile_path: Path | None = None
    fee_profile_sha256: str | None = None
    max_total_cost_wei: int | None = None
    authorized_snapshot_id: str | None = None
    authorized_snapshot_sha256: str | None = None
    authorization_expires_at: datetime | None = None
    authorization_expires_at_raw: str | None = field(default=None, repr=False)
    authorized_network: str | None = None
    authorized_rpc_endpoint_sha256: str | None = None
    authorized_signer_address: str | None = None
    authorized_contract_sha256: str | None = None
    authorized_fee_profile_sha256: str | None = None
    authorized_max_total_cost_wei: int | None = None
    contract_path: Path = DEFAULT_CONTRACT
    poll_interval_seconds: float = 1.0
    max_polls: int = 300

    @classmethod
    def from_environment(cls, environ=None, *, contract_path: Path = DEFAULT_CONTRACT):
        env = os.environ if environ is None else environ
        raw_cap = env.get("TENDERLENS_GENLAYER_MAX_TOTAL_COST_WEI", "")
        raw_authorized_cap = env.get("TENDERLENS_GENLAYER_AUTHORIZED_MAX_TOTAL_COST_WEI", "")
        return cls(
            network=env.get("TENDERLENS_GENLAYER_NETWORK", ""),
            private_key=env.get("TENDERLENS_GENLAYER_PRIVATE_KEY", ""),
            broadcast_authorized=env.get("TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED") == "1",
            rpc_url=env.get("TENDERLENS_GENLAYER_RPC_URL") or None,
            signer_address=env.get("TENDERLENS_GENLAYER_SIGNER_ADDRESS") or None,
            fee_profile_path=Path(env["TENDERLENS_GENLAYER_FEE_PROFILE"]).expanduser()
                             if env.get("TENDERLENS_GENLAYER_FEE_PROFILE") else None,
            fee_profile_sha256=env.get("TENDERLENS_GENLAYER_FEE_PROFILE_SHA256") or None,
            max_total_cost_wei=_parse_decimal_env(raw_cap),
            authorized_snapshot_id=env.get("TENDERLENS_GENLAYER_AUTHORIZED_SNAPSHOT_ID") or None,
            authorized_snapshot_sha256=env.get("TENDERLENS_GENLAYER_AUTHORIZED_SNAPSHOT_SHA256") or None,
            authorization_expires_at=_parse_aware_datetime(env.get("TENDERLENS_GENLAYER_AUTHORIZATION_EXPIRES_AT")),
            authorization_expires_at_raw=env.get("TENDERLENS_GENLAYER_AUTHORIZATION_EXPIRES_AT") or None,
            authorized_network=env.get("TENDERLENS_GENLAYER_AUTHORIZED_NETWORK") or None,
            authorized_rpc_endpoint_sha256=env.get("TENDERLENS_GENLAYER_AUTHORIZED_RPC_ENDPOINT_SHA256") or None,
            authorized_signer_address=env.get("TENDERLENS_GENLAYER_AUTHORIZED_SIGNER_ADDRESS") or None,
            authorized_contract_sha256=env.get("TENDERLENS_GENLAYER_AUTHORIZED_CONTRACT_SHA256") or None,
            authorized_fee_profile_sha256=env.get("TENDERLENS_GENLAYER_AUTHORIZED_FEE_PROFILE_SHA256") or None,
            authorized_max_total_cost_wei=_parse_decimal_env(raw_authorized_cap),
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
    broadcast_authorized: bool
    sdk: Any = field(repr=False)
    _gate_token: object = field(repr=False, compare=False)
    _config_token: DriverConfig = field(repr=False, compare=False)
    contract_source_sha256: str | None = None
    rpc_endpoint: str | None = None
    signer_address: str | None = None
    fee_profile: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    fee_quotes: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    max_fee_per_gas: int | None = None
    max_total_cost_wei: int | None = None
    estimated_total_cost_wei: int | None = None
    wallet_balance_wei: int | None = None
    balance_sufficient: bool | None = None
    cost_within_cap: bool | None = None
    qualified_input_hashes: set[str] = field(default_factory=set, repr=False, compare=False)
    provider_lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)


@dataclass(frozen=True)
class ReadOnlyAccount:
    """Address-only SDK context for view calls; it has no signing capability."""
    address: str


class GenLayerSDKDriver:
    """Qualify a GenLayer target and drive durable deploy/evaluate transactions.

    The default production path checks SDK/network identity, makes a no-broadcast
    ``gen_call`` deploy probe, estimates both fee stages from a hash-bound measured
    profile, verifies the spend ceiling and current wallet balance, and guards the
    SDK's prepared EVM transactions before signing. Optional verifier callbacks are
    test/deployment overrides; the application factory does not install them. Driver
    construction is side-effect free; ``prepare`` and reconciliation make read-only
    network calls.
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

    def _authorized_rpc_endpoint(self) -> str | None:
        network = self.config.network
        if network in STUDIO_ENDPOINTS:
            return STUDIO_ENDPOINTS[network]
        if network == "testnet_bradbury":
            try:
                return self._validated_bradbury_node_endpoint(self.config.rpc_url)
            except DriverError:
                return None
        return SDK_DEFAULT_ENDPOINTS.get(network)

    def _validate_authorization_scope(self, source: str, *, snapshot_id: str | None = None,
                                      snapshot_sha256: str | None = None) -> None:
        cfg = self.config
        if not cfg.broadcast_authorized:
            raise DriverError("broadcast_not_authorized")
        if cfg.authorization_expires_at is None:
            code = "authorization_expiry_invalid" if cfg.authorization_expires_at_raw else "authorization_expiry_missing"
            raise DriverError(code)
        if (not isinstance(cfg.authorization_expires_at, datetime)
                or cfg.authorization_expires_at.tzinfo is None
                or cfg.authorization_expires_at.utcoffset() is None):
            raise DriverError("authorization_expiry_invalid")
        if cfg.authorization_expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise DriverError("authorization_expired")
        if not isinstance(cfg.authorized_snapshot_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            cfg.authorized_snapshot_id,
        ):
            raise DriverError("authorized_snapshot_id_missing")
        if not isinstance(cfg.authorized_snapshot_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", cfg.authorized_snapshot_sha256):
            raise DriverError("authorized_snapshot_digest_missing")
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        authorized_endpoint = self._authorized_rpc_endpoint()
        endpoint_hash = (hashlib.sha256(authorized_endpoint.encode("utf-8")).hexdigest()
                         if authorized_endpoint is not None else None)
        scope_matches = (
            cfg.authorized_network == cfg.network
            and cfg.authorized_rpc_endpoint_sha256 == endpoint_hash
            and isinstance(cfg.authorized_signer_address, str)
            and ADDRESS.fullmatch(cfg.authorized_signer_address) is not None
            and cfg.signer_address is not None
            and cfg.authorized_signer_address.lower() == cfg.signer_address.lower()
            and cfg.authorized_contract_sha256 == source_hash
            and cfg.authorized_fee_profile_sha256 == cfg.fee_profile_sha256
            and type(cfg.authorized_max_total_cost_wei) is int
            and cfg.authorized_max_total_cost_wei == cfg.max_total_cost_wei
        )
        if not scope_matches:
            raise DriverError("authorization_scope_mismatch")
        if snapshot_id is not None and snapshot_id != cfg.authorized_snapshot_id:
            raise DriverError("approved_snapshot_mismatch")
        if snapshot_sha256 is not None and snapshot_sha256 != cfg.authorized_snapshot_sha256:
            raise DriverError("approved_snapshot_mismatch")

    def _static_preflight(self, *, require_broadcast: bool, allow_unapproved: bool = False,
                          preview_only: bool = False):
        cfg = self.config
        if cfg.network not in NETWORK_NAMES:
            raise DriverError("unsupported_network")
        if cfg.network == "testnet_bradbury":
            self._validated_bradbury_node_endpoint(cfg.rpc_url)
        elif cfg.network in STUDIO_ENDPOINTS:
            if cfg.rpc_url is not None and cfg.rpc_url != STUDIO_ENDPOINTS[cfg.network]:
                raise DriverError("rpc_endpoint_not_pinned")
        elif cfg.network in SDK_DEFAULT_ENDPOINTS:
            if cfg.rpc_url is not None and cfg.rpc_url != SDK_DEFAULT_ENDPOINTS[cfg.network]:
                raise DriverError("rpc_endpoint_not_pinned")
        if require_broadcast and not preview_only and (not isinstance(cfg.private_key, str)
                                                        or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", cfg.private_key)):
            raise DriverError("invalid_account_configuration")
        if cfg.poll_interval_seconds <= 0 or cfg.max_polls < 1:
            raise DriverError("invalid_poll_configuration")
        if (self.runtime_qualifier is not None and not callable(self.runtime_qualifier)) or (
            self.network_identity_verifier is not None and not callable(self.network_identity_verifier)
        ) or (self.funding_verifier is not None and not callable(self.funding_verifier)):
            raise DriverError("qualification_gate_invalid")
        if require_broadcast:
            if not cfg.broadcast_authorized and not allow_unapproved:
                raise DriverError("broadcast_not_authorized")
            if cfg.fee_profile_path is None:
                raise DriverError("fee_profile_or_cost_cap_missing")
            if not allow_unapproved and (
                type(cfg.max_total_cost_wei) is not int or not 0 < cfg.max_total_cost_wei <= 2**256 - 1
            ):
                raise DriverError("fee_profile_or_cost_cap_missing")
            if cfg.max_total_cost_wei is not None and (
                type(cfg.max_total_cost_wei) is not int or not 0 < cfg.max_total_cost_wei <= 2**256 - 1
            ):
                raise DriverError("invalid_total_cost_cap")
            if not isinstance(cfg.fee_profile_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", cfg.fee_profile_sha256):
                raise DriverError("fee_profile_digest_missing")
            if not isinstance(cfg.signer_address, str) or not ADDRESS.fullmatch(cfg.signer_address):
                raise DriverError("authorized_signer_address_missing")
            if not allow_unapproved:
                if (not isinstance(cfg.authorized_snapshot_id, str)
                        or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", cfg.authorized_snapshot_id)):
                    raise DriverError("authorized_snapshot_id_missing")
                if not isinstance(cfg.authorized_snapshot_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", cfg.authorized_snapshot_sha256):
                    raise DriverError("authorized_snapshot_digest_missing")
            if not callable(self.network_identity_verifier) and cfg.network not in {"studionet", "studio_devnet", "testnet_bradbury"}:
                raise DriverError("network_identity_verifier_missing")
        elif not callable(self.network_identity_verifier) and cfg.network not in {"studionet", "studio_devnet", "testnet_bradbury"}:
            raise DriverError("network_identity_verifier_missing")
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
        if require_broadcast and not allow_unapproved:
            self._validate_authorization_scope(source)
        if require_broadcast and callable(self.runtime_qualifier):
            try:
                qualified = self.runtime_qualifier(dependency, cfg.network)
            except Exception:
                raise DriverError("runtime_qualification_failed") from None
            if qualified is not True:
                raise DriverError("runtime_not_qualified")
        return source

    @staticmethod
    def _validated_bradbury_node_endpoint(endpoint: str | None) -> str:
        # The pinned genlayer-js/genlayer-py chain definitions publish this node
        # RPC. Using it as the reviewed default removes a configuration blocker
        # without weakening the identity, genesis and consensus-address checks
        # that still run against whatever endpoint is selected.
        if endpoint is None or (isinstance(endpoint, str) and not endpoint.strip()):
            return BRADBURY_NODE_ENDPOINT
        if not isinstance(endpoint, str):
            raise DriverError("bradbury_genlayer_node_rpc_required")
        try:
            parsed = urlsplit(endpoint)
            hostname = (parsed.hostname or "").lower()
            _ = parsed.port
        except ValueError:
            raise DriverError("invalid_rpc_endpoint") from None
        if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or hostname in {"localhost", "127.0.0.1", "::1"}):
            raise DriverError("invalid_rpc_endpoint")
        # This is the underlying GenLayer Chain RPC, not a GenLayer node. The
        # official SDK preset host is allowed through to live identity/capability
        # checks; its presence in configuration does not qualify it.
        if hostname == "rpc.testnet-chain.genlayer.com":
            raise DriverError("underlying_bradbury_chain_rpc_not_genlayer_node")
        return endpoint

    @staticmethod
    def _contract_code_present(client: Any, address: str) -> bool:
        code = client.w3.eth.get_code(address)
        if isinstance(code, str):
            return bool(code.removeprefix("0x"))
        return isinstance(code, (bytes, bytearray)) and bool(code)

    @staticmethod
    def _valid_nonzero_address(value: Any) -> bool:
        return (isinstance(value, str) and ADDRESS.fullmatch(value) is not None
                and value.lower() != "0x" + "0" * 40)

    @classmethod
    def _verify_bradbury_identity(cls, client: Any, chain: Any) -> bool:
        """Verify current Bradbury identity through its live AddressManager.

        The pinned SDK descriptor still carries older static ConsensusMain and
        ConsensusData addresses. Resolve the current registry entries and update
        the in-memory SDK chain objects only after every identity check succeeds.
        """
        genesis = client.w3.eth.get_block(0)
        observed_genesis = cls._normalize_hex_ref(genesis.get("hash"), client.w3)
        if observed_genesis != BRADBURY_GENESIS_HASH:
            return False

        manager_address = client.w3.to_checksum_address(BRADBURY_ADDRESS_MANAGER)
        if not cls._contract_code_present(client, manager_address):
            return False
        manager = client.w3.eth.contract(
            address=manager_address, abi=BRADBURY_ADDRESS_MANAGER_ABI,
        )
        resolved: dict[str, str] = {}
        for key, expected in BRADBURY_PROTOCOL_REGISTRY.items():
            address = manager.functions.getAddress(key).call()
            if not cls._valid_nonzero_address(address) or str(address).lower() != expected:
                return False
            checksum_address = client.w3.to_checksum_address(address)
            if not cls._contract_code_present(client, checksum_address):
                return False
            resolved[key] = checksum_address

        main = getattr(chain, "consensus_main_contract", None)
        data = getattr(chain, "consensus_data_contract", None)
        if (not isinstance(main, dict) or not main.get("abi")
                or not isinstance(data, dict) or not data.get("abi")):
            return False
        main_contract = client.w3.eth.contract(address=resolved["ConsensusMain"], abi=main["abi"])
        data_contract = client.w3.eth.contract(address=resolved["ConsensusData"], abi=data["abi"])
        main_manager = main_contract.functions.getAddressManager().call()
        data_manager = data_contract.functions.addressManager().call()
        if (not cls._valid_nonzero_address(str(main_manager))
                or str(main_manager).lower() != BRADBURY_ADDRESS_MANAGER
                or not cls._valid_nonzero_address(str(data_manager))
                or str(data_manager).lower() != BRADBURY_ADDRESS_MANAGER):
            return False

        # genlayer-py deep-copies its chain descriptor in create_client(); update
        # both copies so subsequent SDK actions use the registry-resolved addresses.
        descriptor_contracts = []
        for descriptor in (chain, getattr(client, "chain", None)):
            if descriptor is None:
                continue
            current_main = getattr(descriptor, "consensus_main_contract", None)
            current_data = getattr(descriptor, "consensus_data_contract", None)
            if not isinstance(current_main, dict) or not isinstance(current_data, dict):
                return False
            descriptor_contracts.append((current_main, current_data))
        for current_main, current_data in descriptor_contracts:
            current_main["address"] = resolved["ConsensusMain"]
            current_data["address"] = resolved["ConsensusData"]
        return True

    @classmethod
    def _verify_builtin_network_identity(cls, client: Any, chain: Any, network: str, endpoint: str) -> bool:
        expected_chain_id = EXPECTED_CHAIN_IDS.get(network)
        if expected_chain_id is None or int(chain.id) != expected_chain_id:
            return False
        if int(client.w3.eth.chain_id) != expected_chain_id:
            return False
        if network == "testnet_bradbury":
            return cls._verify_bradbury_identity(client, chain)
        expected_contracts = NETWORK_CONSENSUS_ADDRESSES.get(network)
        if expected_contracts is None:
            return False
        main = getattr(chain, "consensus_main_contract", None)
        data = getattr(chain, "consensus_data_contract", None)
        if not isinstance(main, dict) or not isinstance(data, dict):
            return False
        for name, config in (("main", main), ("data", data)):
            address = config.get("address")
            if (not isinstance(address, str) or not ADDRESS.fullmatch(address)
                    or address.lower() != expected_contracts[name] or not config.get("abi")
                    or not cls._contract_code_present(client, address)):
                return False
        if network in STUDIO_ENDPOINTS:
            return endpoint == STUDIO_ENDPOINTS[network]
        return False

    def _connect(self, *, broadcast_ready: bool, contract_source: str | None = None) -> Runtime:
        sdk = self._load_sdk()
        if getattr(sdk, "version", SDK_VERSION) != SDK_VERSION:
            raise DriverError("pinned_sdk_version_mismatch")
        try:
            chain = copy.deepcopy(getattr(sdk.chains, self.config.network))
            chain_id = int(chain.id)
            expected_chain_id = EXPECTED_CHAIN_IDS.get(self.config.network)
            if expected_chain_id is None or chain_id != expected_chain_id:
                raise DriverError("sdk_network_chain_id_unqualified")
            default_endpoint = chain.rpc_urls["default"]["http"][0]
            if self.config.network in STUDIO_ENDPOINTS:
                endpoint = STUDIO_ENDPOINTS[self.config.network]
                if default_endpoint != endpoint:
                    raise DriverError("pinned_sdk_endpoint_mismatch")
                if self.config.rpc_url is not None and self.config.rpc_url != endpoint:
                    raise DriverError("rpc_endpoint_not_pinned")
            elif self.config.network == "testnet_bradbury":
                endpoint = self._validated_bradbury_node_endpoint(self.config.rpc_url)
            else:
                endpoint = default_endpoint
                expected_endpoint = SDK_DEFAULT_ENDPOINTS.get(self.config.network)
                if expected_endpoint is not None and endpoint != expected_endpoint:
                    raise DriverError("pinned_sdk_endpoint_mismatch")
                if self.config.rpc_url is not None and self.config.rpc_url != endpoint:
                    raise DriverError("rpc_endpoint_not_pinned")
            if broadcast_ready and hashlib.sha256(endpoint.encode("utf-8")).hexdigest() != self.config.authorized_rpc_endpoint_sha256:
                raise DriverError("authorization_scope_mismatch")
            # Query-only recovery does not derive/import a private key. The public
            # signer address is retained separately for view calls and result reads.
            account = sdk.create_account(account_private_key=self.config.private_key) if broadcast_ready else None
            if broadcast_ready and self.config.signer_address:
                if str(account.address).lower() != self.config.signer_address.lower():
                    raise DriverError("signer_address_mismatch")
            client = sdk.create_client(chain=chain, endpoint=endpoint, account=account)
            observed_chain_id = int(client.w3.eth.chain_id)
        except DriverError:
            raise
        except Exception:
            raise DriverError("network_preflight_failed") from None
        if observed_chain_id != chain_id or observed_chain_id != expected_chain_id:
            raise DriverError("network_chain_id_mismatch")
        try:
            if callable(self.network_identity_verifier):
                identity_verified = self.network_identity_verifier(client, chain, self.config.network)
            else:
                identity_verified = self._verify_builtin_network_identity(client, chain, self.config.network, endpoint)
        except DriverError:
            raise
        except Exception:
            raise DriverError("network_identity_verification_failed") from None
        if identity_verified is not True:
            raise DriverError("network_identity_not_verified")
        if broadcast_ready and callable(self.funding_verifier):
            try:
                funded = self.funding_verifier(client, account, self.config.network)
            except Exception:
                raise DriverError("funding_verification_failed") from None
            if funded is not True:
                raise DriverError("funding_not_verified")
        return Runtime(client=client, account=account, chain=chain, chain_id=chain_id,
                       network=self.config.network, broadcast_ready=broadcast_ready,
                       broadcast_authorized=(broadcast_ready and self.config.broadcast_authorized), sdk=sdk,
                       _gate_token=self._runtime_gate_token, _config_token=self.config,
                       contract_source_sha256=(hashlib.sha256(contract_source.encode("utf-8")).hexdigest()
                                               if contract_source is not None else None),
                       rpc_endpoint=endpoint,
                       signer_address=(str(account.address) if account is not None else self.config.signer_address))

    @staticmethod
    def _runtime_probe_submission() -> str:
        probe = {
            "schema_version": "tenderlens.evaluation-submission.v1",
            "evaluation_id": "10000000-0000-4000-8000-000000000001",
            "snapshot_id": "10000000-0000-4000-8000-000000000002",
            "tender_id": "10000000-0000-4000-8000-000000000003",
            "aggregation_policy": "mandatory_gate_optional_advisory_v1",
            "requirements": [{
                "id": "10000000-0000-4000-8000-000000000004",
                "code": "RUNTIME-PROBE", "text": "Synthetic runtime compatibility probe.",
                "category": "eligibility", "mandatory": True, "kind": "judgment",
            }],
            "evidence": [],
        }
        canonical_body = json.dumps(probe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        probe["snapshot_sha256"] = hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()
        return json.dumps(probe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _qualify_runtime(self, runtime: Runtime, source: str,
                         submission_json: str | None = None) -> None:
        match = re.search(r"\"Depends\"\s*:\s*\"py-genlayer:([^\"]+)\"", source)
        if not match:
            raise DriverError("contract_runtime_reference_missing")
        dependency = match.group(1)
        payload = submission_json if submission_json is not None else self._runtime_probe_submission()
        input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if input_hash in runtime.qualified_input_hashes:
            return
        if callable(self.runtime_qualifier):
            # Test/deployment harness override. The default production path below
            # always asks the selected GenLayer node to execute a no-broadcast deploy call.
            try:
                qualified = self.runtime_qualifier(dependency, runtime.network)
            except Exception:
                raise DriverError("runtime_qualification_failed") from None
            if qualified is not True:
                raise DriverError("runtime_not_qualified")
            runtime.qualified_input_hashes.add(input_hash)
            return
        sender_address = (runtime.account.address if runtime.account is not None else runtime.signer_address)
        if not isinstance(sender_address, str) or not ADDRESS.fullmatch(sender_address):
            raise DriverError("runtime_probe_signer_missing")
        try:
            calldata = importlib.import_module("genlayer_py.abi.calldata")
            abi_utils = importlib.import_module("genlayer_py.contracts.utils")
            abi_transactions = importlib.import_module("genlayer_py.abi.transactions")
            constructor_args = calldata.encode(abi_utils.make_calldata_object(
                method=None, args=[payload], kwargs=None
            ))
            encoded_deployment = abi_transactions.serialize([source, constructor_args, False])
            response = runtime.client.provider.make_request(
                method="gen_call",
                params=[{
                    "type": "deploy", "data": encoded_deployment,
                    "from": sender_address,
                    "to": "0x0000000000000000000000000000000000000000",
                    "status": "finalized",
                }],
            )
        except Exception:
            raise DriverError("runtime_qualification_failed") from None
        result = response.get("result") if isinstance(response, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        if type(code) is not int or code != 0:
            raise DriverError("runtime_not_qualified")
        runtime.qualified_input_hashes.add(input_hash)

    @staticmethod
    def _profile_uint(value: Any, code: str) -> int:
        if type(value) is int and 0 <= value <= (2**256 - 1):
            return value
        if isinstance(value, str) and len(value) <= 78 and re.fullmatch(r"[0-9]+", value):
            parsed = int(value)
            if parsed <= 2**256 - 1:
                return parsed
        raise DriverError(code)

    def _load_fee_profile(self, source: str) -> dict[str, Any]:
        cfg = self.config
        try:
            raw = cfg.fee_profile_path.resolve(strict=True).read_bytes()
        except (AttributeError, OSError, ValueError):
            raise DriverError("fee_profile_unavailable") from None
        if hashlib.sha256(raw).hexdigest() != cfg.fee_profile_sha256:
            raise DriverError("fee_profile_digest_mismatch")
        try:
            profile = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DriverError("fee_profile_invalid") from None
        if not isinstance(profile, dict) or type(profile.get("version")) is not int or profile["version"] != 1:
            raise DriverError("fee_profile_invalid")
        if profile.get("network") != cfg.network:
            raise DriverError("fee_profile_network_mismatch")
        try:
            measured_at = datetime.fromisoformat(str(profile["measuredAt"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            raise DriverError("fee_profile_measurement_time_missing") from None
        if measured_at.tzinfo is None or measured_at > datetime.now(timezone.utc):
            raise DriverError("fee_profile_measurement_time_invalid")
        now_utc = datetime.now(timezone.utc)
        measured_at_utc = measured_at.astimezone(timezone.utc)
        if now_utc - measured_at_utc > MAX_FEE_PROFILE_AGE:
            raise DriverError("fee_profile_stale")
        metadata = profile.get("tenderlens")
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        dependency_match = re.search(r"\"Depends\"\s*:\s*\"py-genlayer:([^\"]+)\"", source)
        if (not isinstance(metadata, dict) or metadata.get("sdkVersion") != SDK_VERSION
                or metadata.get("contractSha256") != source_hash
                or dependency_match is None or metadata.get("runtime") != dependency_match.group(1)):
            raise DriverError("fee_profile_qualification_mismatch")
        valid_until = _parse_aware_datetime(metadata.get("validUntil"))
        if valid_until is None:
            raise DriverError("fee_profile_expiry_missing_or_invalid")
        if valid_until <= datetime.now(timezone.utc):
            raise DriverError("fee_profile_expired")
        if valid_until <= measured_at.astimezone(timezone.utc):
            raise DriverError("fee_profile_expiry_precedes_measurement")
        max_submission_bytes = metadata.get("maxSubmissionBytes")
        if type(max_submission_bytes) is not int or not 1 <= max_submission_bytes <= 250_000:
            raise DriverError("fee_profile_submission_limit_invalid")
        appeal_rounds = metadata.get("appealRounds")
        if type(appeal_rounds) is not int or not 0 <= appeal_rounds <= 2:
            raise DriverError("fee_profile_appeal_policy_invalid")
        gas_limits = metadata.get("maxEvmGas")
        if not isinstance(gas_limits, dict):
            raise DriverError("fee_profile_evm_gas_limits_missing")
        normalized_gas = {}
        for stage in ("deploy", "evaluate"):
            gas = self._profile_uint(gas_limits.get(stage), "fee_profile_evm_gas_limits_invalid")
            if gas <= 0:
                raise DriverError("fee_profile_evm_gas_limits_invalid")
            normalized_gas[stage] = gas
        deploy = profile.get("deploy")
        evaluate = (profile.get("methods") or {}).get("evaluate") if isinstance(profile.get("methods"), dict) else None
        if not isinstance(deploy, dict) or not isinstance(evaluate, dict):
            raise DriverError("fee_profile_methods_missing")
        normalized_methods = {}
        for stage, entry in (("deploy", deploy), ("evaluate", evaluate)):
            try:
                normalized_methods[stage] = {
                    "leaderTimeunitsAllocation": self._profile_uint(entry["leaderTimeunitsAllocation"], "fee_profile_invalid"),
                    "validatorTimeunitsAllocation": self._profile_uint(entry["validatorTimeunitsAllocation"], "fee_profile_invalid"),
                    "executionBudgetPerRound": self._profile_uint(entry["executionBudgetPerRound"], "fee_profile_invalid"),
                    "totalMessageFees": self._profile_uint(entry["totalMessageFees"], "fee_profile_invalid"),
                    "rotationsPerRound": self._profile_uint(entry["rotationsPerRound"], "fee_profile_invalid"),
                }
            except (KeyError, TypeError):
                raise DriverError("fee_profile_invalid") from None
        profile = copy.deepcopy(profile)
        profile["tenderlens"]["maxEvmGas"] = normalized_gas
        profile["tenderlens"]["maxSubmissionBytes"] = max_submission_bytes
        profile["tenderlens"]["appealRounds"] = appeal_rounds
        profile["_normalizedMethods"] = normalized_methods
        profile["_sha256"] = hashlib.sha256(raw).hexdigest()
        return profile

    @staticmethod
    def _fee_estimate_options(profile: dict[str, Any], stage: str) -> dict[str, Any]:
        method = profile["_normalizedMethods"][stage]
        rounds = profile["tenderlens"]["appealRounds"]
        rotations = method["rotationsPerRound"]
        return {
            "leaderTimeunitsAllocation": method["leaderTimeunitsAllocation"],
            "validatorTimeunitsAllocation": method["validatorTimeunitsAllocation"],
            "executionBudgetPerRound": method["executionBudgetPerRound"],
            "totalMessageFees": method["totalMessageFees"],
            "appealRounds": rounds,
            "rotations": [rotations] * (rounds + 1),
        }

    @staticmethod
    def _max_fee_per_gas(runtime: Runtime) -> int:
        if runtime.chain_id in STUDIO_CHAIN_IDS:
            return 0
        try:
            block = runtime.client.w3.eth.get_block("latest")
            base_fee = block.get("baseFeePerGas")
            if type(base_fee) is not int or base_fee < 0:
                raise ValueError
            priority_fee = int(runtime.client.w3.to_wei(2, "gwei"))
            result = base_fee + priority_fee
        except Exception:
            raise DriverError("evm_fee_quote_unavailable") from None
        if result <= 0:
            raise DriverError("evm_fee_quote_invalid")
        return result

    def _quote_stage(self, runtime: Runtime, profile: dict[str, Any], stage: str) -> dict[str, Any]:
        try:
            estimate = runtime.client.estimate_transaction_fees(self._fee_estimate_options(profile, stage))
        except Exception:
            raise DriverError("genlayer_fee_quote_unavailable") from None
        if not isinstance(estimate, dict):
            raise DriverError("genlayer_fee_quote_invalid")
        distribution = estimate.get("distribution")
        fee_value = estimate.get("feeValue", estimate.get("fee_value"))
        if (not isinstance(distribution, dict) or not distribution
                or type(fee_value) is not int or not 0 < fee_value <= 2**256 - 1):
            raise DriverError("genlayer_fee_quote_invalid")
        return {"fees": {"distribution": distribution, "feeValue": fee_value},
                "feeValue": fee_value, "maxEvmGas": profile["tenderlens"]["maxEvmGas"][stage]}

    def _verify_funding_and_quote(self, runtime: Runtime, source: str, *, enforce_limits: bool) -> Runtime:
        profile = self._load_fee_profile(source)
        fee_quotes = {stage: self._quote_stage(runtime, profile, stage) for stage in ("deploy", "evaluate")}
        max_fee_per_gas = self._max_fee_per_gas(runtime)
        total_cost = sum(
            quote["feeValue"] + quote["maxEvmGas"] * max_fee_per_gas
            for quote in fee_quotes.values()
        )
        cap = self.config.max_total_cost_wei
        if enforce_limits and (
            type(cap) is not int or not 0 < cap <= 2**256 - 1
        ):
            raise DriverError("fee_profile_or_cost_cap_missing")
        within_cap = cap is not None and total_cost <= cap
        if enforce_limits and not within_cap:
            raise DriverError("approved_cost_cap_exceeded")
        if not isinstance(runtime.signer_address, str) or not ADDRESS.fullmatch(runtime.signer_address):
            raise DriverError("authorized_signer_address_missing")
        try:
            balance = runtime.client.w3.eth.get_balance(runtime.signer_address)
        except Exception:
            raise DriverError("wallet_balance_unavailable") from None
        if type(balance) is not int or not 0 <= balance <= 2**256 - 1:
            raise DriverError("wallet_balance_invalid")
        sufficient = balance >= total_cost
        if enforce_limits and not sufficient:
            raise DriverError("insufficient_wallet_balance")
        from dataclasses import replace
        return replace(runtime, fee_profile=profile, fee_quotes=fee_quotes,
                       max_fee_per_gas=max_fee_per_gas, max_total_cost_wei=cap,
                       estimated_total_cost_wei=total_cost, wallet_balance_wei=balance,
                       balance_sufficient=sufficient, cost_within_cap=within_cap)

    def prepare(self) -> Runtime:
        """Validate broadcast prerequisites, then initialize/check SDK and funding."""
        source = self._static_preflight(require_broadcast=True)
        runtime = self._connect(broadcast_ready=True, contract_source=source)
        self._qualify_runtime(runtime, source)
        runtime = self._verify_funding_and_quote(runtime, source, enforce_limits=True)
        return runtime

    def prepare_preview(self) -> Runtime:
        """Qualify and quote a signer/target without allowing or authorizing a send."""
        source = self._static_preflight(require_broadcast=True, allow_unapproved=True, preview_only=True)
        runtime = self._connect(broadcast_ready=False, contract_source=source)
        self._qualify_runtime(runtime, source)
        runtime = self._verify_funding_and_quote(runtime, source, enforce_limits=False)
        return runtime

    def preflight(self, verified_input: Any, *, snapshot_id: str, snapshot_sha256: str,
                  policy_version: str) -> dict[str, Any]:
        """Return a snapshot-bound, no-broadcast operation/cost report for review."""
        self.validate_submission(
            verified_input, request_id=verified_input.request_id if is_adapter_verified_submission(verified_input) else "",
            snapshot_id=snapshot_id, snapshot_sha256=snapshot_sha256,
            policy_version=policy_version, require_authorization=False,
        )
        runtime = self.prepare_preview()
        self.validate_submission(
            verified_input, request_id=verified_input.request_id, snapshot_id=snapshot_id,
            snapshot_sha256=snapshot_sha256, policy_version=policy_version,
            runtime=runtime, require_authorization=False,
        )
        if runtime.fee_quotes is None or runtime.fee_profile is None:
            raise DriverError("preflight_fee_evidence_unavailable")
        dependency = runtime.fee_profile["tenderlens"]["runtime"]
        report = {
            "schema_version": "tenderlens.genlayer-preflight.v1",
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snapshot_sha256,
            "contract_input_sha256": hashlib.sha256(
                verified_input.contract_input_json.encode("utf-8")
            ).hexdigest(),
            "contract_input_bytes": len(verified_input.contract_input_json.encode("utf-8")),
            "operation": {
                "deployment": "deploy_contract(procurement_evaluation.py, exact verified snapshot JSON)",
                "evaluation": "write_contract(evaluate)",
                "result": "read_contract(get_result) after finalized successful evaluation",
                "automatic_broadcast": False,
            },
            "target": {
                "network": runtime.network,
                "chain_id": runtime.chain_id,
                "rpc_endpoint": runtime.rpc_endpoint,
                "sdk_version": SDK_VERSION,
                "network_identity_verified": True,
                "identity_checks": (
                    ["sdk_pinned_endpoint", "chain_id", "consensus_main_and_data_addresses_and_code"]
                    if runtime.network in STUDIO_ENDPOINTS else
                    ["chain_id", "genesis_hash", "address_manager", "consensus_main_and_data_addresses_and_code"]
                    if runtime.network == "testnet_bradbury" else
                    ["injected_test_or_operator_verifier"]
                ),
                "node_rpc_gen_call_probe": "passed_for_exact_constructor_input",
            },
            "contract": {
                "path": str(Path(self.config.contract_path).resolve().relative_to(REPOSITORY_ROOT)),
                "source_sha256": runtime.contract_source_sha256,
                "runtime": dependency,
                "runtime_qualified_by_no_broadcast_deploy_call": True,
                "deployed_contract_address": None,
                "deployment_verification_status": "not_deployed_by_preflight; source verification occurs only after authorized finalized deployment",
                "post_deployment_verification": "gen_getContractCode(finalized) SHA-256 must match source",
            },
            "signer": {
                "address": runtime.signer_address,
                "funding_evidence_status": "balance_read_from_verified_target_rpc",
                "wallet_balance_wei": str(runtime.wallet_balance_wei),
                "balance_sufficient_for_estimated_upper_bound": runtime.balance_sufficient,
            },

            "fees": {
                "fee_profile_sha256": runtime.fee_profile.get("_sha256"),
                "fee_profile_measured_at": runtime.fee_profile.get("measuredAt"),
                "fee_profile_valid_until": runtime.fee_profile.get("tenderlens", {}).get("validUntil"),
                "max_fee_per_gas_wei": str(runtime.max_fee_per_gas),
                "stages": runtime.fee_quotes,
                "expected_total_cost_wei": str(runtime.estimated_total_cost_wei),
                "estimated_upper_bound_wei": str(runtime.estimated_total_cost_wei),
                "configured_cost_cap_wei": (str(runtime.max_total_cost_wei)
                                             if runtime.max_total_cost_wei is not None else None),
                "within_configured_cost_cap": runtime.cost_within_cap,
            },
            "authorization": {
                "server_broadcast_flag_enabled": self.config.broadcast_authorized,
                "this_preflight_signing_performed": False,
                "this_preflight_broadcast": False,
                "signing_capability_enabled_for_preflight": False,
                "broadcast_capability_enabled_for_preflight": False,
            },

        }
        report["preflight_outcome"] = (
            "insufficient_wallet_balance" if runtime.balance_sufficient is not True else
            "cost_cap_missing_or_too_low" if runtime.cost_within_cap is not True else
            "ready_for_human_review"
        )
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        report["report_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return report

    def validate_configuration(self, *, preview_only: bool = True) -> bool:
        """Run local, side-effect-free config/profile checks before factory wiring."""
        source = self._static_preflight(
            require_broadcast=True, allow_unapproved=preview_only, preview_only=preview_only
        )
        self._load_fee_profile(source)
        return True

    def validate_readonly_configuration(self) -> bool:
        """Qualify read-only connectivity without a fee profile, signer, or broadcast.

        Reading a deployed contract's lifecycle or result needs none of the
        deployment gates. Requiring a measured fee profile here previously made
        every read-only wiring attempt fail closed, which is why no host could
        report a live GenLayer connection. Signing and broadcasting still require
        the full gate set in ``_static_preflight(require_broadcast=True)``.
        """
        self._static_preflight(require_broadcast=False)
        return True

    def prepare_readonly(self) -> Runtime:
        """Connect for query/recovery without a signer, broadcast, or funding gate.

        The target identity callback remains mandatory: read-only queries against a
        misidentified endpoint can still corrupt local transaction/result association.
        """
        source = self._static_preflight(require_broadcast=False)
        return self._connect(broadcast_ready=False, contract_source=source)

    def _runtime_was_prepared(self, runtime: Runtime) -> bool:
        return (isinstance(runtime, Runtime)
                and runtime._gate_token is self._runtime_gate_token
                and runtime._config_token is self.config
                and runtime.network == self.config.network)

    @staticmethod
    def _normalize_hex_ref(value: Any, w3: Any) -> str | None:
        if isinstance(value, str):
            normalized = value if value.startswith("0x") else "0x" + value
        elif isinstance(value, (bytes, bytearray)):
            normalized = w3.to_hex(bytes(value))
        elif hasattr(value, "hex"):
            normalized = value.hex()
            if not isinstance(normalized, str):
                return None
            if not normalized.startswith("0x"):
                normalized = "0x" + normalized
        else:
            return None
        return normalized.lower() if TX_HASH.fullmatch(normalized) else None

    @classmethod
    def _raw_evm_hash(cls, runtime: Runtime, raw_transaction: Any) -> str:
        if isinstance(raw_transaction, (bytes, bytearray)):
            raw_transaction = runtime.client.w3.to_hex(bytes(raw_transaction))
        if not isinstance(raw_transaction, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", raw_transaction):
            raise DriverError("signed_evm_transaction_invalid")
        try:
            digest = runtime.client.w3.keccak(hexstr=raw_transaction)
            evm_hash = runtime.client.w3.to_hex(digest).lower()
        except Exception:
            raise DriverError("signed_evm_transaction_hash_failed") from None
        if not TX_HASH.fullmatch(evm_hash):
            raise DriverError("signed_evm_transaction_hash_invalid")
        return evm_hash

    @contextmanager
    def _journal_broadcast(self, runtime: Runtime, stage: str, lifecycle: SubmissionLifecycle):
        """Persist the outer EVM hash before the SDK calls eth_sendRawTransaction.

        The SDK only returns the GenLayer transaction ID after the EVM node accepts the
        signed transaction, mines its receipt, and the SDK decodes a consensus event.
        Recording the hash at the provider boundary closes the lost-response gap: a
        timeout after the RPC accepted the raw transaction remains queryable and is
        never treated as permission to send it again.
        """
        provider = getattr(runtime.client, "provider", None)
        w3 = getattr(runtime.client, "w3", None)
        if provider is None or w3 is None or getattr(w3, "provider", None) is not provider:
            raise DriverError("sdk_provider_binding_unverified")
        original = getattr(provider, "make_request", None)
        if not callable(original):
            raise DriverError("sdk_provider_request_hook_unavailable")
        journal = {"evm_tx_hash": None, "send_attempts": 0}
        persist = getattr(lifecycle, f"{stage}_broadcast", None)
        if not callable(persist):
            raise DriverError("durable_broadcast_journal_unavailable")

        def wrapped(method, params):
            if method in {"eth_sendTransaction", "gen_sendTransaction"}:
                raise DriverError("unsupported_sdk_broadcast_method")
            if method == "eth_sendRawTransaction":
                if journal["send_attempts"]:
                    raise DriverError("multiple_evm_broadcasts_in_one_stage")
                if not isinstance(params, (list, tuple)) or not params:
                    raise DriverError("signed_evm_transaction_invalid")
                evm_hash = self._raw_evm_hash(runtime, params[0])
                # This durable write happens before the original provider can send.
                # If it fails, no raw transaction reaches the network.
                try:
                    persist(evm_hash)
                except Exception:
                    raise DriverError("durable_broadcast_journal_failed") from None
                journal["evm_tx_hash"] = evm_hash
                journal["send_attempts"] += 1
            return original(method, params)

        try:
            with runtime.provider_lock:
                # Pinning the SDK version lets us guard its exact provider boundary;
                # restore both hooks even on RPC errors or cancellation.
                from unittest.mock import patch
                with patch.object(provider, "make_request", new=wrapped):
                    if runtime.fee_quotes is None:
                        yield journal
                    else:
                        fee_stage = {"deployment": "deploy", "evaluation": "evaluate"}.get(stage)
                        quote = runtime.fee_quotes.get(fee_stage) if fee_stage else None
                        if not isinstance(quote, dict) or runtime.max_fee_per_gas is None:
                            raise DriverError("transaction_cost_guard_unavailable")
                        try:
                            sdk_actions = importlib.import_module("genlayer_py.contracts.actions")
                            original_prepare = sdk_actions._prepare_transaction
                        except Exception:
                            raise DriverError("sdk_transaction_guard_unavailable") from None

                        def guarded_prepare(*args, **kwargs):
                            transaction = original_prepare(*args, **kwargs)

                            def parse_uint(value):
                                if type(value) is int and 0 <= value <= 2**256 - 1:
                                    return value
                                if isinstance(value, str) and len(value) <= 78:
                                    if re.fullmatch(r"0x[0-9a-fA-F]+", value):
                                        return int(value, 16)
                                    if re.fullmatch(r"[0-9]+", value):
                                        return int(value)
                                raise ValueError

                            try:
                                if not isinstance(transaction, dict):
                                    raise ValueError
                                gas = parse_uint(transaction.get("gas"))
                                gas_cap = quote["maxEvmGas"]
                                chain_id = parse_uint(transaction.get("chainId"))
                                sender = str(transaction.get("from", "")).lower()
                                expected_sender = str(runtime.account.address).lower()
                                recipient = transaction.get("to")
                                consensus = getattr(runtime.chain, "consensus_main_contract", None)
                                expected_recipient = (consensus.get("address")
                                                      if isinstance(consensus, dict) else None)
                                data = transaction.get("data")
                                tx_value = parse_uint(transaction.get("value"))
                                if "maxFeePerGas" in transaction:
                                    max_fee = parse_uint(transaction["maxFeePerGas"])
                                elif "gasPrice" in transaction:
                                    max_fee = parse_uint(transaction["gasPrice"])
                                else:
                                    raise ValueError
                                priority_fee = (
                                    parse_uint(transaction["maxPriorityFeePerGas"])
                                    if "maxPriorityFeePerGas" in transaction else 0
                                )
                            except (KeyError, TypeError, ValueError):
                                raise DriverError("prepared_evm_transaction_invalid") from None
                            if gas <= 0 or gas > gas_cap:
                                raise DriverError("prepared_evm_gas_exceeds_profile")
                            if chain_id != runtime.chain_id or sender != expected_sender:
                                raise DriverError("prepared_evm_transaction_identity_mismatch")
                            if (not isinstance(expected_recipient, str) or not ADDRESS.fullmatch(expected_recipient)
                                    or not isinstance(recipient, str) or recipient.lower() != expected_recipient.lower()
                                    or not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", data)):
                                raise DriverError("prepared_evm_transaction_target_mismatch")
                            if (max_fee > runtime.max_fee_per_gas
                                    or (runtime.chain_id not in STUDIO_CHAIN_IDS and max_fee <= 0)
                                    or priority_fee > max_fee):
                                raise DriverError("prepared_evm_fee_exceeds_quote")
                            if tx_value != quote.get("feeValue"):
                                raise DriverError("prepared_genlayer_fee_value_mismatch")
                            return transaction

                        with patch.object(sdk_actions, "_prepare_transaction", new=guarded_prepare):
                            yield journal
        except DriverError:
            raise
        except Exception:
            raise DriverError("sdk_provider_request_hook_failed") from None

    @classmethod
    def _recover_genlayer_hash_from_evm_receipt(cls, runtime: Runtime, evm_tx_hash: str,
                                                 lifecycle: SubmissionLifecycle) -> str | None:
        """Recover the GenLayer ID emitted by the SDK's outer EVM transaction."""
        try:
            receipt = runtime.client.w3.eth.get_transaction_receipt(evm_tx_hash)
        except Exception:
            lifecycle.pending("EVM_RECEIPT_PENDING_OR_UNAVAILABLE")
            return None
        if not isinstance(receipt, dict):
            lifecycle.pending("EVM_RECEIPT_PENDING_OR_UNAVAILABLE")
            return None
        observed_hash = cls._normalize_hex_ref(receipt.get("transactionHash"), runtime.client.w3)
        if observed_hash != evm_tx_hash.lower():
            lifecycle.outcome_unknown("EVM_RECEIPT_REFERENCE_MISMATCH")
            raise DriverError("evm_receipt_reference_mismatch")
        status = receipt.get("status")
        try:
            evm_status = int(status, 0) if isinstance(status, str) else int(status)
        except (TypeError, ValueError):
            lifecycle.outcome_unknown("EVM_RECEIPT_STATUS_UNVERIFIED")
            raise DriverError("evm_receipt_status_unverified") from None
        if evm_status == 0:
            lifecycle.failed("evm_submission_reverted", "EVM_REVERTED")
            raise DriverError("evm_submission_reverted")
        if evm_status != 1:
            lifecycle.outcome_unknown("EVM_RECEIPT_STATUS_UNVERIFIED")
            raise DriverError("evm_receipt_status_unverified")

        consensus = getattr(runtime.chain, "consensus_main_contract", None)
        if not isinstance(consensus, dict) or not consensus.get("address") or not consensus.get("abi"):
            lifecycle.outcome_unknown("CONSENSUS_EVENT_ABI_UNAVAILABLE")
            raise DriverError("consensus_event_abi_unavailable")
        try:
            contract = runtime.client.w3.eth.contract(
                address=consensus["address"], abi=consensus["abi"]
            )
            try:
                sdk_actions = importlib.import_module("genlayer_py.contracts.actions")
                discard = sdk_actions.DISCARD
            except Exception:
                # Unit-only SDK-shaped fakes may supply the same sentinel without
                # installing GenLayer dependencies. Production loads the pinned SDK.
                discard = getattr(runtime.sdk, "DISCARD", None)
                if discard is None:
                    raise
            # Match the pinned SDK's event precedence. NewTransaction is emitted for
            # immediately activated work; CreatedTransaction is emitted for queued work.
            for event_name in ("NewTransaction", "CreatedTransaction"):
                event = contract.get_event_by_name(event_name)
                decoded = event.process_receipt(receipt, errors=discard)
                if decoded:
                    if len(decoded) != 1:
                        break
                    value = decoded[0].get("args", {}).get("txId")
                    tx_hash = cls._normalize_hex_ref(value, runtime.client.w3)
                    if tx_hash is not None:
                        return tx_hash
                    break
        except DriverError:
            raise
        except Exception:
            lifecycle.outcome_unknown("CONSENSUS_EVENT_DECODE_UNAVAILABLE")
            raise DriverError("consensus_event_decode_unavailable") from None
        lifecycle.outcome_unknown("CONSENSUS_EVENT_MISSING")
        raise DriverError("consensus_event_missing")

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
                "canceled": ("rejected", "transaction_canceled"),
                "undetermined": ("rejected", "transaction_undetermined"),
                "validators_timeout": ("timed_out", "validators_timeout"),
                "leader_timeout": ("timed_out", "leader_timeout"),
            }
            terminal_value = state if state in terminal_failures else outcome
            if terminal_value in terminal_failures:
                terminal_state, code = terminal_failures[terminal_value]
                if terminal_state == "rejected":
                    lifecycle.rejected(code, terminal_value)
                else:
                    lifecycle.timed_out(code, terminal_value)
                raise DriverError(code)
            lifecycle.pending(state or "UNRECOGNIZED")
            self._sleep(self.config.poll_interval_seconds)
        lifecycle.outcome_unknown("POLL_TIMEOUT")
        raise DriverError("transaction_poll_timeout")

    @staticmethod
    def _contract_address(runtime: Runtime, receipt: dict[str, Any]) -> str:
        # Studio returns local-style `data`; public testnets return decoded tx data.
        if runtime.network in {"studionet", "studio_devnet"}:
            value = (receipt.get("data") or {}).get("contract_address")
        else:
            value = (receipt.get("tx_data_decoded") or {}).get("contract_address")
        if not isinstance(value, str) or not ADDRESS.fullmatch(value):
            raise DriverError("deployment_receipt_contract_address_missing")
        return value.lower()

    @staticmethod
    def _read_account(runtime: Runtime):
        if runtime.account is not None:
            return runtime.account
        if isinstance(runtime.signer_address, str) and ADDRESS.fullmatch(runtime.signer_address):
            return ReadOnlyAccount(runtime.signer_address)
        return None

    def validate_submission(self, verified_input: Any, *, request_id: str, snapshot_id: str,
                            snapshot_sha256: str, policy_version: str, runtime: Runtime | None = None,
                            require_authorization: bool = True) -> None:
        if not is_adapter_verified_submission(verified_input):
            raise DriverError("adapter_verified_snapshot_required")
        if (verified_input.request_id != request_id or verified_input.snapshot_id != snapshot_id
                or verified_input.snapshot_sha256 != snapshot_sha256
                or verified_input.policy_version != policy_version):
            raise DriverError("verified_submission_identity_mismatch")
        if require_authorization:
            try:
                source_for_auth = Path(self.config.contract_path).read_text(encoding="utf-8")
            except OSError:
                raise DriverError("contract_source_unavailable") from None
            if (runtime is not None and hashlib.sha256(source_for_auth.encode("utf-8")).hexdigest()
                    != runtime.contract_source_sha256):
                raise DriverError("contract_source_changed_after_preflight")
            self._validate_authorization_scope(
                source_for_auth, snapshot_id=snapshot_id, snapshot_sha256=snapshot_sha256
            )
        try:
            submission = json.loads(verified_input.contract_input_json)
        except (TypeError, ValueError):
            raise DriverError("verified_submission_json_invalid") from None
        if (not isinstance(submission, dict) or submission.get("snapshot_id") != snapshot_id
                or submission.get("snapshot_sha256") != snapshot_sha256
                or submission.get("aggregation_policy") != policy_version):
            raise DriverError("verified_submission_identity_mismatch")
        if runtime is not None:
            if not self._runtime_was_prepared(runtime):
                raise DriverError("runtime_preflight_required")
            if runtime.fee_profile is not None:
                size = len(verified_input.contract_input_json.encode("utf-8"))
                if size > runtime.fee_profile["tenderlens"]["maxSubmissionBytes"]:
                    raise DriverError("submission_exceeds_measured_profile")
            input_hash = hashlib.sha256(verified_input.contract_input_json.encode("utf-8")).hexdigest()
            if input_hash not in runtime.qualified_input_hashes:
                try:
                    source = Path(self.config.contract_path).read_text(encoding="utf-8")
                except OSError:
                    raise DriverError("contract_source_unavailable") from None
                if hashlib.sha256(source.encode("utf-8")).hexdigest() != runtime.contract_source_sha256:
                    raise DriverError("contract_source_changed_after_preflight")
                self._qualify_runtime(runtime, source, verified_input.contract_input_json)

    @staticmethod
    def _verify_deployed_source(runtime: Runtime, contract_address: str,
                                lifecycle: SubmissionLifecycle) -> None:
        """Compare target-reported finalized code with the exact source just deployed."""
        if runtime.contract_source_sha256 is None:
            lifecycle.outcome_unknown("DEPLOYED_CODE_EXPECTED_HASH_UNAVAILABLE")
            raise DriverError("deployed_code_expected_hash_unavailable")
        try:
            response = runtime.client.provider.make_request(
                method="gen_getContractCode",
                params=[{"address": contract_address, "status": "finalized"}],
            )
        except Exception:
            lifecycle.outcome_unknown("DEPLOYED_CODE_QUERY_FAILED")
            raise DriverError("deployed_code_query_failed") from None
        if not isinstance(response, dict) or response.get("error") is not None:
            lifecycle.outcome_unknown("DEPLOYED_CODE_QUERY_FAILED")
            raise DriverError("deployed_code_query_failed")
        encoded = response.get("result")
        if not isinstance(encoded, str) or not encoded:
            lifecycle.outcome_unknown("DEPLOYED_CODE_RESPONSE_INVALID")
            raise DriverError("deployed_code_response_invalid")
        try:
            deployed_code = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            lifecycle.outcome_unknown("DEPLOYED_CODE_RESPONSE_INVALID")
            raise DriverError("deployed_code_response_invalid") from None
        if hashlib.sha256(deployed_code).hexdigest() != runtime.contract_source_sha256:
            lifecycle.failed("deployed_contract_code_mismatch", "DEPLOYED_CODE_MISMATCH")
            raise DriverError("deployed_contract_code_mismatch")

    def __call__(self, verified_input: Any, *, request_id: str, snapshot_id: str,
                 snapshot_sha256: str, policy_version: str, lifecycle: SubmissionLifecycle,
                 runtime: Runtime | None = None):
        if runtime is None or not self._runtime_was_prepared(runtime):
            raise DriverError("runtime_preflight_required")
        if not runtime.broadcast_ready:
            raise DriverError("broadcast_preflight_required")
        if not runtime.broadcast_authorized or not self.config.broadcast_authorized:
            raise DriverError("broadcast_not_authorized")
        self.validate_submission(verified_input, request_id=request_id, snapshot_id=snapshot_id,
                                 snapshot_sha256=snapshot_sha256, policy_version=policy_version,
                                 runtime=runtime)
        submission_json = verified_input.contract_input_json
        try:
            source = Path(self.config.contract_path).read_text(encoding="utf-8")
        except OSError:
            raise DriverError("contract_source_unavailable") from None
        current_source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if (runtime.contract_source_sha256 is None
                or current_source_hash != runtime.contract_source_sha256):
            raise DriverError("contract_source_changed_after_preflight")
        lifecycle.record_network(runtime.network, runtime.chain_id)
        if runtime.fee_quotes is not None:
            report = {
                "sdk_version": SDK_VERSION,
                "network": runtime.network,
                "chain_id": runtime.chain_id,
                "rpc_endpoint": runtime.rpc_endpoint,
                "contract_source_sha256": runtime.contract_source_sha256,
                "runtime": runtime.fee_profile.get("tenderlens", {}).get("runtime") if runtime.fee_profile else None,
                "fee_profile_sha256": runtime.fee_profile.get("_sha256") if runtime.fee_profile else None,
                "fee_quotes": runtime.fee_quotes,
                "max_fee_per_gas": runtime.max_fee_per_gas,
            }
            try:
                report_json = json.dumps(report, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                raise DriverError("preflight_cost_report_invalid") from None
            record_report = getattr(lifecycle, "record_preflight_report", None)
            if not callable(record_report):
                raise DriverError("durable_preflight_cost_report_unavailable")
            record_report(
                signer_address=runtime.signer_address,
                fee_quote_json=report_json,
                estimated_cost_wei=runtime.estimated_total_cost_wei,
                max_total_cost_wei=runtime.max_total_cost_wei,
                wallet_balance_wei=runtime.wallet_balance_wei,
            )
        lifecycle.deployment_started()
        deployment_journal = {"evm_tx_hash": None}
        try:
            with self._journal_broadcast(runtime, "deployment", lifecycle) as journal:
                deployment_journal = journal
                deployment_kwargs = {"code": source, "account": runtime.account, "args": [submission_json]}
                if runtime.fee_quotes is not None:
                    deployment_kwargs["fees"] = runtime.fee_quotes["deploy"]["fees"]
                deployment_hash = runtime.client.deploy_contract(**deployment_kwargs)
        except Exception as exc:
            if deployment_journal.get("evm_tx_hash"):
                lifecycle.outcome_unknown("SDK_DEPLOYMENT_CALL_FAILED_AFTER_EVM_BROADCAST")
                raise DriverError("deployment_submission_outcome_unknown") from None
            if isinstance(exc, DriverError):
                lifecycle.failed(exc.code, "NOT_BROADCAST")
                raise
            lifecycle.failed("deployment_prebroadcast_failed", "NOT_BROADCAST")
            raise DriverError("deployment_prebroadcast_failed") from None
        deployment_evm_hash = deployment_journal.get("evm_tx_hash")
        if not deployment_evm_hash:
            lifecycle.failed("sdk_broadcast_hook_not_observed", "NO_EVM_BROADCAST")
            raise DriverError("sdk_broadcast_hook_not_observed")
        if not isinstance(deployment_hash, str) or not TX_HASH.fullmatch(deployment_hash):
            lifecycle.outcome_unknown("GENLAYER_TRANSACTION_REFERENCE_INVALID")
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
        self._verify_deployed_source(runtime, contract_address, lifecycle)

        lifecycle.evaluation_started()
        evaluation_journal = {"evm_tx_hash": None}
        try:
            with self._journal_broadcast(runtime, "evaluation", lifecycle) as journal:
                evaluation_journal = journal
                evaluation_kwargs = {
                    "address": contract_address, "function_name": "evaluate",
                    "account": runtime.account, "args": [],
                }
                if runtime.fee_quotes is not None:
                    evaluation_kwargs["fees"] = runtime.fee_quotes["evaluate"]["fees"]
                evaluation_hash = runtime.client.write_contract(**evaluation_kwargs)
        except Exception as exc:
            if evaluation_journal.get("evm_tx_hash"):
                lifecycle.outcome_unknown("SDK_EVALUATION_CALL_FAILED_AFTER_EVM_BROADCAST")
                raise DriverError("evaluation_submission_outcome_unknown") from None
            if isinstance(exc, DriverError):
                lifecycle.failed(exc.code, "NOT_BROADCAST")
                raise
            lifecycle.failed("evaluation_prebroadcast_failed", "NOT_BROADCAST")
            raise DriverError("evaluation_prebroadcast_failed") from None
        evaluation_evm_hash = evaluation_journal.get("evm_tx_hash")
        if not evaluation_evm_hash:
            lifecycle.failed("sdk_broadcast_hook_not_observed", "NO_EVM_BROADCAST")
            raise DriverError("sdk_broadcast_hook_not_observed")
        if not isinstance(evaluation_hash, str) or not TX_HASH.fullmatch(evaluation_hash):
            lifecycle.outcome_unknown("GENLAYER_TRANSACTION_REFERENCE_INVALID")
            raise DriverError("evaluation_transaction_reference_invalid")
        lifecycle.evaluation_submitted(evaluation_hash)
        self._poll(runtime, evaluation_hash, lifecycle)
        lifecycle.finalized()
        try:
            result_json = runtime.client.read_contract(address=contract_address, function_name="get_result",
                                                       args=[], account=self._read_account(runtime))
        except Exception:
            raise DriverError("finalized_result_query_failed") from None
        if not isinstance(result_json, str):
            raise DriverError("finalized_result_decode_invalid")
        return {"contract_address": contract_address, "deployment_tx_hash": deployment_hash.lower(),
                "evaluation_tx_hash": evaluation_hash.lower(),
                "deployment_evm_tx_hash": deployment_evm_hash,
                "evaluation_evm_tx_hash": evaluation_evm_hash,
                "result_json": result_json, "result_reference": evaluation_hash.lower()}

    def reconcile(self, adapter, request_id: str, *, runtime: Runtime | None = None, resume: bool = False):
        """Reconcile GenLayer IDs from either saved IDs or their EVM submission receipts.

        This method never rebroadcasts an operation whose EVM hash is already durable.
        ``resume=True`` only allows advancing from a finalized deployment when the
        evaluation write has never started; the API recovery route always uses false.
        """
        if runtime is None:
            runtime = self.prepare() if resume else self.prepare_readonly()
        if not self._runtime_was_prepared(runtime):
            raise DriverError("runtime_preflight_required")
        if resume and not runtime.broadcast_ready:
            raise DriverError("broadcast_preflight_required")
        if resume and (not runtime.broadcast_authorized or not self.config.broadcast_authorized):
            raise DriverError("broadcast_not_authorized")
        request = adapter.get_request(request_id)
        state = request["lifecycle_state"]
        if state in {"completed", "failed", "rejected", "timed_out"}:
            return request
        lifecycle = adapter.lifecycle(request_id)
        if resume and self.funding_verifier is None and (
            request.get("snapshot_id") != self.config.authorized_snapshot_id
            or request.get("snapshot_sha256") != self.config.authorized_snapshot_sha256
        ):
            raise DriverError("approved_snapshot_mismatch")
        deployment_hash = request.get("deployment_tx_hash")
        deployment_evm_hash = request.get("deployment_evm_tx_hash")
        evaluation_hash = request.get("evaluation_tx_hash")
        evaluation_evm_hash = request.get("evaluation_evm_tx_hash")
        contract_address = request.get("contract_address")

        # First reconcile the later phase if an evaluation EVM transaction is known.
        if evaluation_hash or evaluation_evm_hash:
            if not evaluation_hash:
                evaluation_hash = self._recover_genlayer_hash_from_evm_receipt(
                    runtime, evaluation_evm_hash, lifecycle
                )
                if evaluation_hash is None:
                    return adapter.get_request(request_id)
                lifecycle.evaluation_submitted(evaluation_hash)
            if not contract_address:
                lifecycle.outcome_unknown("EVALUATION_CONTRACT_ADDRESS_MISSING")
                raise DriverError("evaluation_contract_address_missing")
            self._verify_deployed_source(runtime, contract_address, lifecycle)
            self._poll(runtime, evaluation_hash, lifecycle)
            lifecycle.finalized()
            try:
                result_json = runtime.client.read_contract(
                    address=contract_address, function_name="get_result", args=[], account=self._read_account(runtime)
                )
            except Exception:
                raise DriverError("finalized_result_query_failed") from None
            if not isinstance(result_json, str):
                raise DriverError("finalized_result_decode_invalid")
            accepted = adapter.accept_result(
                request_id, contract_address, evaluation_hash, result_json, evaluation_hash
            )
            durable = adapter.get_request(request_id)
            return {**accepted, "contract_address": contract_address,
                    "deployment_tx_hash": durable.get("deployment_tx_hash"),
                    "evaluation_tx_hash": evaluation_hash,
                    "deployment_evm_tx_hash": durable.get("deployment_evm_tx_hash"),
                    "evaluation_evm_tx_hash": durable.get("evaluation_evm_tx_hash")}

        if state == "evaluation_submission_in_progress" or (
            contract_address and state != "deployment_finalized"
        ):
            lifecycle.outcome_unknown(request.get("last_network_status"))
            raise DriverError("evaluation_submission_outcome_unknown_no_hash")

        if not contract_address:
            if not deployment_hash:
                if not deployment_evm_hash:
                    raise DriverError("no_persisted_transaction_for_reconciliation")
                deployment_hash = self._recover_genlayer_hash_from_evm_receipt(
                    runtime, deployment_evm_hash, lifecycle
                )
                if deployment_hash is None:
                    return adapter.get_request(request_id)
                lifecycle.deployment_submitted(deployment_hash)
            self._poll(runtime, deployment_hash, lifecycle)
            try:
                receipt = runtime.client.wait_for_transaction_receipt(
                    transaction_hash=deployment_hash, wait_until="finalized", interval=0, retries=1
                )
            except Exception:
                lifecycle.outcome_unknown("FINALIZED_RECEIPT_UNAVAILABLE")
                raise DriverError("deployment_receipt_unavailable") from None
            self._verify_transaction_reference(receipt, deployment_hash, lifecycle)
            self._verify_finalized_success(receipt, lifecycle)
            contract_address = self._contract_address(runtime, receipt)
            lifecycle.deployment_finalized(contract_address)
            self._verify_deployed_source(runtime, contract_address, lifecycle)
        elif state == "deployment_finalized":
            self._verify_deployed_source(runtime, contract_address, lifecycle)
        else:
            raise DriverError("deployment_submission_outcome_unknown")

        if not resume:
            return adapter.get_request(request_id)

        lifecycle.evaluation_started()
        evaluation_journal = {"evm_tx_hash": None}
        try:
            with self._journal_broadcast(runtime, "evaluation", lifecycle) as journal:
                evaluation_journal = journal
                evaluation_kwargs = {
                    "address": contract_address, "function_name": "evaluate",
                    "account": runtime.account, "args": [],
                }
                if runtime.fee_quotes is not None:
                    evaluation_kwargs["fees"] = runtime.fee_quotes["evaluate"]["fees"]
                evaluation_hash = runtime.client.write_contract(**evaluation_kwargs)
        except Exception as exc:
            if evaluation_journal.get("evm_tx_hash"):
                lifecycle.outcome_unknown("SDK_EVALUATION_CALL_FAILED_AFTER_EVM_BROADCAST")
                raise DriverError("evaluation_submission_outcome_unknown") from None
            if isinstance(exc, DriverError):
                lifecycle.failed(exc.code, "NOT_BROADCAST")
                raise
            lifecycle.failed("evaluation_prebroadcast_failed", "NOT_BROADCAST")
            raise DriverError("evaluation_prebroadcast_failed") from None
        evaluation_evm_hash = evaluation_journal.get("evm_tx_hash")
        if not evaluation_evm_hash:
            lifecycle.failed("sdk_broadcast_hook_not_observed", "NO_EVM_BROADCAST")
            raise DriverError("sdk_broadcast_hook_not_observed")
        if not isinstance(evaluation_hash, str) or not TX_HASH.fullmatch(evaluation_hash):
            lifecycle.outcome_unknown("GENLAYER_TRANSACTION_REFERENCE_INVALID")
            raise DriverError("evaluation_transaction_reference_invalid")
        lifecycle.evaluation_submitted(evaluation_hash)
        self._poll(runtime, evaluation_hash, lifecycle)
        lifecycle.finalized()
        try:
            result_json = runtime.client.read_contract(
                address=contract_address, function_name="get_result", args=[], account=self._read_account(runtime)
            )
        except Exception:
            raise DriverError("finalized_result_query_failed") from None
        if not isinstance(result_json, str):
            raise DriverError("finalized_result_decode_invalid")
        accepted = adapter.accept_result(request_id, contract_address, evaluation_hash, result_json, evaluation_hash)
        durable = adapter.get_request(request_id)
        return {**accepted, "contract_address": contract_address,
                "deployment_tx_hash": durable.get("deployment_tx_hash"),
                "evaluation_tx_hash": evaluation_hash,
                "deployment_evm_tx_hash": durable.get("deployment_evm_tx_hash"),
                "evaluation_evm_tx_hash": durable.get("evaluation_evm_tx_hash")}
