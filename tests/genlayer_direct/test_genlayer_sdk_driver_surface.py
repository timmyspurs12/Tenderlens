"""Offline import/signature assertions for the repository-pinned genlayer-py API."""
import importlib.metadata
import inspect
import unittest

from genlayer_py import create_account, create_client
from genlayer_py.client.genlayer_client import GenLayerClient
from genlayer_py.types import ExecutionResult


class PinnedGenLayerSDKSurfaceTests(unittest.TestCase):
    def test_locked_sdk_version_and_submission_read_polling_surface(self):
        self.assertEqual(importlib.metadata.version("genlayer-py"), "0.19.0rc2")
        self.assertEqual({"chain", "endpoint", "account"} & set(inspect.signature(create_client).parameters),
                         {"chain", "endpoint", "account"})
        self.assertIn("account_private_key", inspect.signature(create_account).parameters)
        for method in ("deploy_contract", "write_contract", "get_transaction",
                       "wait_for_transaction_receipt", "read_contract"):
            self.assertTrue(callable(getattr(GenLayerClient, method, None)), method)
        deploy = inspect.signature(GenLayerClient.deploy_contract).parameters
        self.assertTrue({"code", "account", "args"}.issubset(deploy))
        write = inspect.signature(GenLayerClient.write_contract).parameters
        self.assertTrue({"address", "function_name", "account", "args"}.issubset(write))
        read = inspect.signature(GenLayerClient.read_contract).parameters
        self.assertTrue({"address", "function_name", "account", "args"}.issubset(read))
        poll = inspect.signature(GenLayerClient.wait_for_transaction_receipt).parameters
        self.assertTrue({"transaction_hash", "wait_until", "interval", "retries", "full_transaction"}.issubset(poll))
        self.assertEqual(poll["wait_until"].default, "decided")
        self.assertEqual(ExecutionResult.FINISHED_WITH_RETURN.value, "FINISHED_WITH_RETURN")
        self.assertEqual(ExecutionResult.FINISHED_WITH_ERROR.value, "FINISHED_WITH_ERROR")

    def test_sdk_chain_descriptors_are_named_but_testnet_chain_ids_are_not_unique(self):
        from genlayer_py.chains import testnet_asimov, testnet_bradbury, studionet
        self.assertEqual(testnet_asimov.id, testnet_bradbury.id)
        self.assertNotEqual(testnet_asimov.rpc_urls["default"]["http"][0],
                            testnet_bradbury.rpc_urls["default"]["http"][0])
        self.assertNotEqual(studionet.rpc_urls["default"]["http"][0],
                            testnet_asimov.rpc_urls["default"]["http"][0])


if __name__ == "__main__":
    unittest.main()
