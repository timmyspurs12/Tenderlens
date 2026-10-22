"""Credential-free, network-free initialization smoke test for the locked SDK."""
from unittest.mock import patch

from genlayer_py import create_client
from genlayer_py.chains import testnet_bradbury
from genlayer_py.provider.provider import GenLayerProvider


BRADBURY_RPC_CANDIDATE = "https://rpc-bradbury.genlayer.com"


def test_pinned_bradbury_client_initializes_without_account_or_rpc_request():
    assert testnet_bradbury.id == 4221
    assert testnet_bradbury.rpc_urls["default"]["http"][0] == BRADBURY_RPC_CANDIDATE

    # This is deliberately the SDK's real client constructor. If initialization
    # unexpectedly contacts the endpoint, fail before any request can leave CI.
    with patch.object(
        GenLayerProvider,
        "make_request",
        side_effect=AssertionError("SDK initialization must not perform network I/O"),
    ) as make_request:
        client = create_client(
            chain=testnet_bradbury,
            endpoint=BRADBURY_RPC_CANDIDATE,
            account=None,
        )

    make_request.assert_not_called()
    assert client.chain.id == 4221
    assert client.provider.url == BRADBURY_RPC_CANDIDATE
    assert client.local_account is None
