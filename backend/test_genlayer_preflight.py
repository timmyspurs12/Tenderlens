import tempfile
import unittest
from pathlib import Path

from scripts.genlayer_preflight import (
    EXPECTED_SDK,
    EXPECTED_TEST_SUITE,
    EXPECTED_TEST_SUITE_COMMIT,
    collect_checks,
    format_report,
)

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {
    "genlayer-py": EXPECTED_SDK,
    "genlayer-test": EXPECTED_TEST_SUITE,
    "pytest": "9.1.1",
}


class GenLayerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tests/genlayer_direct/contracts").mkdir(parents=True)
        (self.root / "requirements-genlayer.in").write_text(
            "genlayer-py==0.18.0\n", encoding="utf-8"
        )
        (self.root / "requirements-genlayer.lock.txt").write_text(
            (ROOT / "requirements-genlayer.lock.txt").read_text(encoding="utf-8"), encoding="utf-8"
        )
        source = (ROOT / "tests/genlayer_direct/contracts/procurement_evaluation.py").read_text(encoding="utf-8")
        (self.root / "tests/genlayer_direct/contracts/procurement_evaluation.py").write_text(source, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def checks(self, env=None, rpc_probe=None):
        return collect_checks(
            self.root,
            env or {},
            versions=VERSIONS,
            suite_commit=EXPECTED_TEST_SUITE_COMMIT,
            rpc_probe=rpc_probe,
        )

    @staticmethod
    def by_name(checks, name):
        return next(item for item in checks if item["name"] == name)

    def test_mutable_runtime_and_missing_deployment_configuration_block_preflight(self):
        checks = self.checks()
        self.assertEqual(self.by_name(checks, "Contract runner pin")["level"], "BLOCK")
        self.assertEqual(self.by_name(checks, "WASM compatibility")["level"], "BLOCK")
        self.assertEqual(self.by_name(checks, "Target network")["level"], "BLOCK")
        self.assertEqual(self.by_name(checks, "GenLayer dependency lock")["level"], "PASS")
        self.assertEqual(self.by_name(checks, "Deployment account")["level"], "BLOCK")
        self.assertIn("No deployment transaction", format_report(checks))

    def test_preflight_never_echoes_configured_private_key(self):
        secret = "ab" * 32
        checks = self.checks({
            "TENDERLENS_GENLAYER_NETWORK": "studionet",
            "TENDERLENS_GENLAYER_PRIVATE_KEY": secret,
        })
        report = format_report(checks)
        self.assertNotIn(secret, report)
        self.assertIn("value is not displayed", report)
        self.assertEqual(self.by_name(checks, "Deployment account")["level"], "PASS")
        self.assertEqual(self.by_name(checks, "RPC")["level"], "BLOCK")

    def test_optional_rpc_probe_uses_pinned_sdk_chain_id_and_endpoint(self):
        calls = []
        def probe(url, chain_id):
            calls.append((url, chain_id))
            return True, "synthetic read-only probe succeeded"
        checks = self.checks({"TENDERLENS_GENLAYER_NETWORK": "studionet"}, probe)
        self.assertEqual(calls, [("https://studio.genlayer.com/api", 61999)])
        self.assertEqual(self.by_name(checks, "RPC")["level"], "PASS")

    def test_explicit_runner_content_reference_is_distinguished_from_mutable_alias(self):
        contract = self.root / "tests/genlayer_direct/contracts/procurement_evaluation.py"
        source = contract.read_text(encoding="utf-8").replace("py-genlayer:latest", "py-genlayer:" + "01" * 26)
        contract.write_text(source, encoding="utf-8")
        checks = self.checks({"TENDERLENS_GENLAYER_NETWORK": "testnet_bradbury"})
        self.assertEqual(self.by_name(checks, "Contract runner pin")["level"], "INFO")
        self.assertEqual(self.by_name(checks, "Target network")["level"], "PASS")
        self.assertEqual(self.by_name(checks, "RPC")["level"], "BLOCK")
        self.assertEqual(self.by_name(checks, "Funding")["level"], "BLOCK")

    def test_rpc_failure_and_wrong_chain_are_explicit_blockers(self):
        checks = self.checks(
            {"TENDERLENS_GENLAYER_NETWORK": "testnet_asimov"},
            lambda _url, _chain_id: (False, "RPC returned an unexpected chain ID."),
        )
        self.assertEqual(self.by_name(checks, "RPC")["level"], "BLOCK")
        self.assertIn("unexpected chain ID", self.by_name(checks, "RPC")["detail"])


if __name__ == "__main__":
    unittest.main()
