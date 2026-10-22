"""Offline tests for the read-only RPC probe; the transport is always a fake."""
import json
from unittest.mock import patch

from eth_abi import decode
import pytest

from scripts import genlayer_rpc_qualification as probe


class FakeRpc:
    def __init__(self, *, chain_id=probe.EXPECTED_CHAIN_ID, method_errors=True):
        self.chain_id = chain_id
        self.method_errors = method_errors
        self.calls = []

    @staticmethod
    def _address_word(address):
        return "0x" + "0" * 24 + address[2:].lower()

    def call(self, method, params):
        request_id = len(self.calls) + 1
        self.calls.append((method, params))
        result = None
        if method == "eth_chainId":
            result = hex(self.chain_id)
        elif method == "eth_getBlockByNumber":
            result = {"number": "0x0", "hash": probe.EXPECTED_GENESIS_HASH}
        elif method == "eth_getCode":
            result = "0x60006000"
        elif method == "eth_call":
            target = params[0]["to"].lower()
            if target == probe.EXPECTED_ADDRESS_MANAGER:
                encoded_args = bytes.fromhex(params[0]["data"][10:])
                key = decode(("string",), encoded_args)[0]
                result = self._address_word(probe.EXPECTED_REGISTRY[key])
            elif target in {
                probe.EXPECTED_REGISTRY["ConsensusMain"],
                probe.EXPECTED_REGISTRY["ConsensusData"],
            }:
                result = self._address_word(probe.EXPECTED_ADDRESS_MANAGER)
            else:
                raise AssertionError(f"unexpected eth_call target {target}")
        elif method in {"gen_call", "gen_getTransactionLifecycle", "gen_getContractCode"}:
            if not self.method_errors:
                result = {"accepted": True}
            else:
                return probe.RpcReply(
                    request_id,
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": "read-only sentinel probe rejected"},
                    },
                )
        else:
            raise AssertionError(f"unexpected or unsafe JSON-RPC method: {method}")
        return probe.RpcReply(
            request_id,
            200,
            {"jsonrpc": "2.0", "id": request_id, "result": result},
        )


def test_readonly_probe_checks_current_identity_and_dispatches_only_allowlisted_methods():
    rpc = FakeRpc()
    report = probe.qualify_rpc(
        probe.BRADBURY_SDK_RPC_CANDIDATE,
        rpc,
        gen_call_data=probe.build_gen_call_read_data(),
    )

    assert report["qualification_status"] == "read_only_rpc_checks_passed"
    assert report["identity"]["status"] == "verified"
    assert report["checks"]["eth_chainId"]["observed"] == 4221
    assert report["checks"]["genesis"]["observed_hash"] == probe.EXPECTED_GENESIS_HASH
    for key in probe.EXPECTED_REGISTRY:
        assert report["checks"][f"registry.{key}"]["status"] == "verified"
        assert report["checks"][f"registry.{key}.code"]["status"] == "verified"
    for key in ("ConsensusMain", "ConsensusData"):
        assert report["checks"][f"registry.{key}.manager_link"]["status"] == "verified"
    assert all(item["status"] == "recognized_error" for item in report["genlayer_methods"].values())
    assert report["wallet_created"] is False
    assert report["signing_performed"] is False
    assert report["transaction_submission_attempted"] is False
    assert report["deployment_attempted"] is False

    allowed = {
        "eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call",
        "gen_call", "gen_getTransactionLifecycle", "gen_getContractCode",
    }
    assert {method for method, _params in rpc.calls} <= allowed
    read_probes = [params[0] for method, params in rpc.calls if method == "gen_call"]
    assert len(read_probes) == 1
    assert read_probes[0]["type"] == "read"
    assert read_probes[0]["from"] == probe.ZERO_ADDRESS
    assert read_probes[0]["to"] == probe.ZERO_ADDRESS
    lifecycle = next(params[0] for method, params in rpc.calls if method == "gen_getTransactionLifecycle")
    assert lifecycle == {"txId": probe.ZERO_TRANSACTION_ID}
    code_probe = next(params[0] for method, params in rpc.calls if method == "gen_getContractCode")
    assert code_probe == {"address": probe.ZERO_ADDRESS, "status": "finalized"}


def test_identity_mismatch_blocks_genlayer_probes():
    rpc = FakeRpc(chain_id=1)
    report = probe.qualify_rpc(
        probe.BRADBURY_SDK_RPC_CANDIDATE,
        rpc,
        gen_call_data="0x00",
    )
    assert report["identity"]["status"] == "blocked"
    assert all(item["status"] == "not_run_identity_prerequisite_failed"
               for item in report["genlayer_methods"].values())
    assert [method for method, _params in rpc.calls] == ["eth_chainId"]


def test_candidate_endpoint_validation_rejects_chain_rpc_credentials_and_implicit_custom_nodes():
    assert probe.validate_endpoint(probe.BRADBURY_SDK_RPC_CANDIDATE) == probe.BRADBURY_SDK_RPC_CANDIDATE
    with pytest.raises(ValueError, match="underlying chain RPC"):
        probe.validate_endpoint(probe.BRADBURY_CHAIN_RPC)
    with pytest.raises(ValueError, match="credentials"):
        probe.validate_endpoint("https://user:secret@rpc-bradbury.genlayer.com")
    with pytest.raises(ValueError, match="credentials"):
        probe.validate_endpoint("https://rpc-bradbury.genlayer.com/?token=secret")
    with pytest.raises(ValueError, match="path tokens"):
        probe.validate_endpoint("https://rpc-bradbury.genlayer.com/path-token")
    with pytest.raises(ValueError, match="--allow-operator-node"):
        probe.validate_endpoint("https://qualified-node.example")
    assert probe.validate_endpoint("https://qualified-node.example", allow_operator_node=True)


def test_method_not_found_is_not_accepted_as_capability():
    reply = probe.RpcReply(
        1, 200,
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}},
    )
    assert probe._method_status(reply) == "unsupported"


def test_json_rpc_client_starts_with_positive_id_and_never_follows_redirects():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_limit):
            return b'{"jsonrpc":"2.0","id":1,"result":"0x107d"}'

    client = probe.JsonRpcClient(probe.BRADBURY_SDK_RPC_CANDIDATE)
    with patch.object(client._opener, "open", return_value=Response()) as open_request:
        reply = client.call("eth_chainId", [])
    request = open_request.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["id"] == 1
    assert payload["method"] == "eth_chainId"
    assert request.method == "POST"
    assert reply.response["result"] == "0x107d"

    handler = probe._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://other.example/") is None
