#!/usr/bin/env python3
"""Read-only qualification probe for a Bradbury GenLayer node RPC candidate.

This tool makes only EVM reads and documented GenLayer read/query calls. It does
not create or load an account, estimate fees, read balances, sign, broadcast, or
deploy. The gen_call probe has type="read" and targets the zero address so it
checks RPC dispatch only; it does not qualify this application's contract runtime.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

BRADBURY_SDK_RPC_CANDIDATE = "https://rpc-bradbury.genlayer.com"
BRADBURY_CHAIN_RPC = "https://rpc.testnet-chain.genlayer.com"
EXPECTED_CHAIN_ID = 4221
EXPECTED_GENESIS_HASH = "0x17d89764d4f4f963158fc4c5e8f2cb670c2837740b35f37081c12860c3df97e4"
EXPECTED_ADDRESS_MANAGER = "0x3f71ae080bd6270a91dbcc7960d88797828538e0"
BRADBURY_PROTOCOL_MANIFEST_COMMIT = "8e570073bd04b1450045dd2433e70383e5971655"
# Current official Bradbury protocol manifest, dated 2026-10-19. These mappings
# are read from the AddressManager at qualification time and compared to the
# versioned manifest; the older SDK's static contract addresses are not trusted.
EXPECTED_REGISTRY = {
    "ConsensusMain": "0x2e2766cbc3e5e61ce93bbcdcbb3eacb82082a019",
    "ConsensusData": "0xc5fa6f82bee53598825d6edf7e3e24613082c75b",
    "Staking": "0xd8fba96c0dbb0257028dbc0107060b958e5c7113",
}
ZERO_ADDRESS = "0x" + "0" * 40
ZERO_TRANSACTION_ID = "0x" + "0" * 64
METHOD_NOT_FOUND_CODES = {-32601}
METHOD_NOT_FOUND_TEXT = ("method not found", "method unavailable", "unknown method")
MAX_RESPONSE_BYTES = 1_000_000
MAX_ERROR_CHARS = 400


@dataclass(frozen=True)
class RpcReply:
    request_id: int
    http_status: int | None
    response: dict[str, Any] | None
    transport_error: str | None = None

    def report(self, method: str) -> dict[str, Any]:
        result: dict[str, Any] = {"request_id": self.request_id, "http_status": self.http_status}
        if self.transport_error is not None:
            result["transport_error"] = self.transport_error[:MAX_ERROR_CHARS]
            return result
        if self.response is None:
            result["protocol_error"] = "missing_json_rpc_response"
            return result
        result["jsonrpc"] = self.response.get("jsonrpc")
        result["response_id"] = self.response.get("id")
        error = self.response.get("error")
        if error is not None:
            if isinstance(error, dict):
                result["error"] = {
                    "code": error.get("code"),
                    "message": str(error.get("message", ""))[:MAX_ERROR_CHARS],
                }
            else:
                result["error"] = str(error)[:MAX_ERROR_CHARS]
            return result
        if "result" not in self.response:
            result["protocol_error"] = "missing_result_and_error"
            return result
        value = self.response["result"]
        if method == "eth_getCode":
            code = _hex_bytes(value)
            result["result"] = {
                "nonempty": bool(code),
                "byte_length": len(code) if code is not None else None,
                "sha256": hashlib.sha256(code).hexdigest() if code is not None else None,
            }
        elif method == "eth_getBlockByNumber":
            if isinstance(value, dict):
                result["result"] = {key: value.get(key) for key in ("number", "hash", "parentHash", "timestamp")}
            else:
                result["result"] = value
        elif method == "gen_getContractCode" and isinstance(value, str):
            try:
                code = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                code = None
            result["result"] = {
                "base64_length": len(value),
                "valid_base64": code is not None,
                "source_byte_length": len(code) if code is not None else None,
                "source_sha256": hashlib.sha256(code).hexdigest() if code is not None else None,
            }
        else:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
            if len(encoded) <= 2_000:
                result["result"] = value
            else:
                result["result_summary"] = {
                    "json_length": len(encoded),
                    "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                }
        return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonRpcClient:
    """One-shot JSON-RPC POST client with positive IDs, no retries, and no redirects."""

    def __init__(self, endpoint: str, timeout: float = 15.0):
        self.endpoint = endpoint
        self.timeout = timeout
        self._next_id = 1
        self._opener = build_opener(_NoRedirect())

    def call(self, method: str, params: list[Any]) -> RpcReply:
        request_id = self._next_id
        self._next_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                http_status = getattr(response, "status", None)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            http_status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except OSError:
                raw = b""
        except (URLError, TimeoutError, OSError) as exc:
            return RpcReply(request_id, None, None, f"{type(exc).__name__}: {str(exc)}")
        if len(raw) > MAX_RESPONSE_BYTES:
            return RpcReply(request_id, http_status, None, "response_exceeded_1mb_limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return RpcReply(request_id, http_status, None, f"invalid_json_response: {type(exc).__name__}")
        if not isinstance(decoded, dict):
            return RpcReply(request_id, http_status, None, "json_rpc_response_is_not_an_object")
        if decoded.get("id") != request_id:
            return RpcReply(request_id, http_status, decoded, "json_rpc_response_id_mismatch")
        if decoded.get("jsonrpc") != "2.0":
            return RpcReply(request_id, http_status, decoded, "json_rpc_version_mismatch")
        return RpcReply(request_id, http_status, decoded)


def validate_endpoint(endpoint: str, *, allow_operator_node: bool = False) -> str:
    if not isinstance(endpoint, str) or not endpoint or endpoint.strip() != endpoint or any(c.isspace() for c in endpoint):
        raise ValueError("RPC URL must be a non-empty URL without whitespace")
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except ValueError:
        raise ValueError("RPC URL is malformed") from None
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("RPC URL must use HTTPS without credentials, query, fragment, or path tokens")
    if hostname in {"localhost", "127.0.0.1", "::1", "rpc.testnet-chain.genlayer.com"}:
        raise ValueError("the underlying chain RPC and local endpoints are not accepted as GenLayer node RPCs")
    candidate_host = urlsplit(BRADBURY_SDK_RPC_CANDIDATE).hostname
    if hostname != candidate_host and not allow_operator_node:
        raise ValueError("non-default endpoints require the explicit --allow-operator-node option")
    return endpoint


def _encode_eth_call(signature: str, arg_types: tuple[str, ...] = (), args: tuple[Any, ...] = ()) -> str:
    # Use Ethereum Keccak and the pinned web3 dependency's ABI encoder.
    from eth_abi import encode
    from eth_utils import keccak

    selector = keccak(text=signature)[:4]
    return "0x" + (selector + encode(arg_types, args)).hex()


def _decode_address(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        return None
    address = "0x" + value[-40:].lower()
    if address == ZERO_ADDRESS:
        return None
    return address


def _hex_bytes(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        return None
    try:
        return bytes.fromhex(value[2:])
    except ValueError:
        return None


def build_gen_call_read_data() -> str:
    """Serialize a harmless empty read-call payload using the pinned GenLayer SDK."""
    from genlayer_py.abi.calldata import encode
    from genlayer_py.abi.transactions import serialize
    from genlayer_py.contracts.utils import make_calldata_object

    encoded_call = encode(make_calldata_object(
        method="__tenderlens_readonly_rpc_probe__", args=[], kwargs=None,
    ))
    serialized = serialize([encoded_call, b"\x00"])
    if isinstance(serialized, bytes):
        return "0x" + serialized.hex()
    if isinstance(serialized, str) and serialized:
        return serialized
    raise ValueError("pinned SDK returned an invalid serialized read call")


def _json_reply(reply: RpcReply | None, method: str) -> dict[str, Any]:
    if reply is None:
        return {"status": "not_run"}
    return reply.report(method)


def _method_status(reply: RpcReply | None) -> str:
    if reply is None:
        return "not_run"
    if (reply.transport_error or reply.response is None
            or reply.http_status is None or not 200 <= reply.http_status < 300):
        return "inconclusive"
    error = reply.response.get("error")
    if error is None:
        return "supported" if "result" in reply.response else "inconclusive"
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message", "")).lower()
    else:
        code = None
        message = str(error).lower()
    if code in METHOD_NOT_FOUND_CODES or any(marker in message for marker in METHOD_NOT_FOUND_TEXT):
        return "unsupported"
    # A structured JSON-RPC application/parameter error is evidence the method
    # name was dispatched, but not proof the probe executed successfully.
    return "recognized_error"


def _request(rpc: Any, method: str, params: list[Any], observations: list[dict[str, Any]], label: str) -> RpcReply | None:
    try:
        reply = rpc.call(method, params)
    except Exception as exc:  # injected transports are kept fail-closed too
        observations.append({
            "label": label, "method": method,
            "transport_error": f"{type(exc).__name__}: {str(exc)}"[:MAX_ERROR_CHARS],
        })
        return None
    if not isinstance(reply, RpcReply):
        observations.append({"label": label, "method": method, "protocol_error": "invalid_transport_result"})
        return None
    observations.append({"label": label, "method": method, "response": reply.report(method)})
    return reply


def _result(reply: RpcReply | None) -> Any:
    if (reply is None or reply.transport_error or reply.response is None
            or reply.http_status is None or not 200 <= reply.http_status < 300
            or reply.response.get("error") is not None):
        return None
    return reply.response.get("result")


def qualify_rpc(endpoint: str, rpc: Any, *, gen_call_data: str) -> dict[str, Any]:
    """Run identity checks first; only probe gen_* methods after identity matches."""
    observations: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    chain_reply = _request(rpc, "eth_chainId", [], observations, "chain_id")
    observed_chain = _result(chain_reply)
    try:
        chain_id = int(observed_chain, 16) if isinstance(observed_chain, str) and observed_chain.startswith("0x") else None
    except ValueError:
        chain_id = None
    checks["eth_chainId"] = {
        "status": "verified" if chain_id == EXPECTED_CHAIN_ID else "blocked" if chain_id is not None else "inconclusive",
        "expected": EXPECTED_CHAIN_ID,
        "observed": chain_id,
        "response": _json_reply(chain_reply, "eth_chainId"),
    }

    identity_stages: dict[str, Any] = {"eth_chainId": checks["eth_chainId"]["status"]}
    if chain_id == EXPECTED_CHAIN_ID:
        genesis_reply = _request(
            rpc, "eth_getBlockByNumber", ["0x0", False], observations, "genesis_block",
        )
        genesis = _result(genesis_reply)
        genesis_hash = genesis.get("hash") if isinstance(genesis, dict) else None
        genesis_status = "verified" if isinstance(genesis_hash, str) and genesis_hash.lower() == EXPECTED_GENESIS_HASH else "blocked" if isinstance(genesis_hash, str) else "inconclusive"
        checks["genesis"] = {
            "status": genesis_status,
            "expected_hash": EXPECTED_GENESIS_HASH,
            "observed_hash": genesis_hash,
            "response": _json_reply(genesis_reply, "eth_getBlockByNumber"),
        }
        identity_stages["genesis"] = genesis_status
    else:
        checks["genesis"] = {"status": "not_run_wrong_or_unknown_chain_id", "expected_hash": EXPECTED_GENESIS_HASH}
        identity_stages["genesis"] = "not_run"

    if chain_id == EXPECTED_CHAIN_ID and identity_stages.get("genesis") == "verified":
        manager_code_reply = _request(
            rpc, "eth_getCode", [EXPECTED_ADDRESS_MANAGER, "latest"], observations, "address_manager_code",
        )
        manager_code = _hex_bytes(_result(manager_code_reply))
        manager_status = "verified" if manager_code else "blocked" if manager_code is not None else "inconclusive"
        checks["address_manager_code"] = {
            "status": manager_status,
            "address": EXPECTED_ADDRESS_MANAGER,
            "response": _json_reply(manager_code_reply, "eth_getCode"),
        }
        identity_stages["address_manager_code"] = manager_status
    else:
        checks["address_manager_code"] = {"status": "not_run_identity_prerequisite_failed"}
        identity_stages["address_manager_code"] = "not_run"

    resolved_registry: dict[str, str | None] = {}
    registry_statuses: dict[str, str] = {}
    if identity_stages.get("address_manager_code") == "verified":
        for key, expected_address in EXPECTED_REGISTRY.items():
            calldata = _encode_eth_call("getAddress(string)", ("string",), (key,))
            reply = _request(
                rpc, "eth_call", [{"to": EXPECTED_ADDRESS_MANAGER, "data": calldata}, "latest"],
                observations, f"address_manager_get_{key}",
            )
            observed_address = _decode_address(_result(reply))
            status = "verified" if observed_address == expected_address else "blocked" if observed_address is not None else "inconclusive"
            resolved_registry[key] = observed_address
            registry_statuses[key] = status
            checks[f"registry.{key}"] = {
                "status": status,
                "expected_address": expected_address,
                "observed_address": observed_address,
                "response": _json_reply(reply, "eth_call"),
            }
            if observed_address is not None:
                code_reply = _request(
                    rpc, "eth_getCode", [observed_address, "latest"], observations, f"registry_{key}_code",
                )
                code = _hex_bytes(_result(code_reply))
                code_status = "verified" if code else "blocked" if code is not None else "inconclusive"
                checks[f"registry.{key}.code"] = {
                    "status": code_status,
                    "address": observed_address,
                    "response": _json_reply(code_reply, "eth_getCode"),
                }
                registry_statuses[f"{key}.code"] = code_status
            else:
                checks[f"registry.{key}.code"] = {"status": "not_run_address_unavailable"}
                registry_statuses[f"{key}.code"] = "not_run"

    prerequisites_verified = (
        identity_stages.get("address_manager_code") == "verified"
        and all(value == "verified" for value in registry_statuses.values())
        and all(f"{key}.code" in registry_statuses and registry_statuses[f"{key}.code"] == "verified"
                for key in EXPECTED_REGISTRY)
    )
    if prerequisites_verified:
        for contract_key, signature in (
            ("ConsensusMain", "getAddressManager()"),
            ("ConsensusData", "addressManager()"),
        ):
            calldata = _encode_eth_call(signature)
            reply = _request(
                rpc, "eth_call", [{"to": resolved_registry[contract_key], "data": calldata}, "latest"],
                observations, f"{contract_key}_manager_link",
            )
            observed_manager = _decode_address(_result(reply))
            status = "verified" if observed_manager == EXPECTED_ADDRESS_MANAGER else "blocked" if observed_manager is not None else "inconclusive"
            checks[f"registry.{contract_key}.manager_link"] = {
                "status": status,
                "expected_address_manager": EXPECTED_ADDRESS_MANAGER,
                "observed_address_manager": observed_manager,
                "response": _json_reply(reply, "eth_call"),
            }
            registry_statuses[f"{contract_key}.manager_link"] = status
    else:
        for contract_key in ("ConsensusMain", "ConsensusData"):
            checks[f"registry.{contract_key}.manager_link"] = {"status": "not_run_registry_prerequisite_failed"}
            registry_statuses[f"{contract_key}.manager_link"] = "not_run"

    identity_status = "verified" if (
        chain_id == EXPECTED_CHAIN_ID
        and identity_stages.get("genesis") == "verified"
        and identity_stages.get("address_manager_code") == "verified"
        and all(status == "verified" for status in registry_statuses.values())
    ) else "blocked" if any(status == "blocked" for status in [*identity_stages.values(), *registry_statuses.values()]) else "inconclusive"

    genlayer_methods: dict[str, Any] = {}
    if identity_status == "verified":
        probes = (
            ("gen_call", [{
                "type": "read",
                "to": ZERO_ADDRESS,
                "from": ZERO_ADDRESS,
                "data": gen_call_data,
                "transaction_hash_variant": "latest-nonfinal",
            }], "read_only_zero_address_dispatch"),
            ("gen_getTransactionLifecycle", [{"txId": ZERO_TRANSACTION_ID}], "zero_tx_id_lifecycle_lookup"),
            ("gen_getContractCode", [{"address": ZERO_ADDRESS, "status": "finalized"}], "zero_address_finalized_code_lookup"),
        )
        for method, params, label in probes:
            reply = _request(rpc, method, params, observations, label)
            genlayer_methods[method] = {
                "status": _method_status(reply),
                "probe": label,
                "read_only": True,
                "response": _json_reply(reply, method),
            }
    else:
        for method in ("gen_call", "gen_getTransactionLifecycle", "gen_getContractCode"):
            genlayer_methods[method] = {
                "status": "not_run_identity_prerequisite_failed",
                "read_only": True,
            }

    all_methods_recognized = all(
        check["status"] in {"supported", "recognized_error"}
        for check in genlayer_methods.values()
    )
    rpc_status = "read_only_rpc_checks_passed" if identity_status == "verified" and all_methods_recognized else (
        "blocked" if identity_status == "blocked" or any(
            check["status"] == "unsupported" for check in genlayer_methods.values()
        ) else "inconclusive"
    )
    return {
        "schema_version": "tenderlens.genlayer-readonly-rpc-qualification.v1",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "candidate_source": "genlayer-py==0.19.0rc2 testnet_bradbury chain descriptor; endpoint presence is not qualification",
        "qualification_status": rpc_status,
        "read_only": True,
        "external_side_effects": [],
        "wallet_created": False,
        "private_key_loaded": False,
        "signing_performed": False,
        "fees_queried": False,
        "balances_queried": False,
        "transaction_submission_attempted": False,
        "deployment_attempted": False,
        "identity": {
            "status": identity_status,
            "expected_chain_id": EXPECTED_CHAIN_ID,
            "expected_genesis_hash": EXPECTED_GENESIS_HASH,
            "expected_address_manager": EXPECTED_ADDRESS_MANAGER,
            "protocol_manifest_commit": BRADBURY_PROTOCOL_MANIFEST_COMMIT,
            "expected_registry": EXPECTED_REGISTRY,
        },
        "checks": checks,
        "genlayer_methods": genlayer_methods,
        "contract_runtime_compatibility": "not_tested_no_deployment_or_contract_execution",
        "deployment_readiness": "not_established",
        "rpc_observations": observations,
        "allowlisted_rpc_methods": sorted({observation["method"] for observation in observations if "method" in observation}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", default=BRADBURY_SDK_RPC_CANDIDATE,
                        help="HTTPS GenLayer node RPC URL (default: pinned SDK Bradbury candidate)")
    parser.add_argument("--allow-operator-node", action="store_true",
                        help="allow an explicitly supplied non-default HTTPS node endpoint")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-request timeout in seconds (default: 15)")
    args = parser.parse_args(argv)
    try:
        endpoint = validate_endpoint(args.rpc_url, allow_operator_node=args.allow_operator_node)
        if not 0.1 <= args.timeout <= 120:
            raise ValueError("timeout must be between 0.1 and 120 seconds")
        gen_call_data = build_gen_call_read_data()
        report = qualify_rpc(endpoint, JsonRpcClient(endpoint, timeout=args.timeout), gen_call_data=gen_call_data)
    except Exception as exc:
        report = {
            "schema_version": "tenderlens.genlayer-readonly-rpc-qualification.v1",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "qualification_status": "inconclusive",
            "read_only": True,
            "external_side_effects": [],
            "deployment_readiness": "not_established",
            "error": f"{type(exc).__name__}: {str(exc)}"[:MAX_ERROR_CHARS],
            "rpc_observations": [],
        }
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report.get("qualification_status") == "read_only_rpc_checks_passed" else 2


if __name__ == "__main__":
    sys.exit(main())
