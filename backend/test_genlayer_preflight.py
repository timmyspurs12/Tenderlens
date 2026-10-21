"""Offline tests for structured qualification and its strict no-side-effects boundary."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from backend.genlayer_sdk_driver import GenLayerSDKDriver
from scripts.genlayer_preflight import (
    EXPECTED_SDK,
    EXPECTED_TEST_SUITE,
    EXPECTED_TEST_SUITE_COMMIT,
    OFFICIAL_IMMUTABLE_RUNTIME,
    REQUIRED_PROJECT_FILES,
    SUPPORTED_NETWORKS,
    VALID_STATUSES,
    collect_report,
    format_report,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {"genlayer-py": EXPECTED_SDK, "genlayer-test": EXPECTED_TEST_SUITE, "pytest": "9.1.1"}


class GenLayerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for relpath in REQUIRED_PROJECT_FILES:
            source = ROOT / relpath
            target = self.root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, env=None, **kwargs):
        return collect_report(self.root, env or {}, versions=VERSIONS,
                              suite_commit=EXPECTED_TEST_SUITE_COMMIT,
                              python_version=(3, 12), **kwargs)

    @staticmethod
    def by_id(report, identifier):
        return next(item for item in report["checks"] if item["id"] == identifier)

    def test_report_schema_has_only_allowed_statuses_and_fails_closed(self):
        report = self.report()
        self.assertEqual(report["schema_version"], "tenderlens.deployment-qualification.v1")
        self.assertFalse(report["ready_to_deploy"])
        self.assertEqual(report["external_side_effects_performed"], [])
        self.assertFalse(report["secret_values_included"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["checks"])
        self.assertEqual(len({check["id"] for check in report["checks"]}), len(report["checks"]))
        for check in report["checks"]:
            self.assertIn(check["status"], VALID_STATUSES)
            for required in ("summary", "evidence", "resolution", "owner", "requires_user_action",
                             "check_scope", "resolution_scope", "external_side_effect_required"):
                self.assertIn(required, check)
        self.assertEqual(self.by_id(report, "runtime.artifact_immutability")["status"], "verified")
        self.assertEqual(self.by_id(report, "runtime.target_compatibility")["status"], "unknown")
        self.assertEqual(self.by_id(report, "target.remote_identity")["status"], "unknown")
        self.assertEqual(self.by_id(report, "target.identity_verifier_gate_present")["status"], "verified")
        self.assertEqual(self.by_id(report, "target.identity_verifier_availability")["status"], "blocked")

    def test_locked_local_dependencies_and_local_syntax_are_classified_without_runtime_claims(self):
        report = self.report()
        for identifier in ("python.runtime", "dependencies.lock_consistency", "dependencies.sdk_installed",
                           "dependencies.direct_mode_suite", "dependencies.pytest_installed",
                           "contract.configuration_consistency", "project.required_files"):
            self.assertEqual(self.by_id(report, identifier)["status"], "verified", identifier)
        self.assertEqual(self.by_id(report, "runtime.target_compatibility")["status"], "unknown")
        self.assertIn("not contract compilation", self.by_id(report, "contract.configuration_consistency")["evidence"])

    def test_official_runtime_directive_must_be_on_the_first_line(self):
        contract = self.root / "tests/genlayer_direct/contracts/procurement_evaluation.py"
        original = contract.read_text(encoding="utf-8")
        contract.write_text("# unrelated version comment\n" + original, encoding="utf-8")
        report = self.report()
        self.assertEqual(self.by_id(report, "runtime.artifact_immutability")["status"], "blocked")
        self.assertFalse(report["ready_to_deploy"])
        contract.write_text(original, encoding="utf-8")

    def test_official_immutable_reference_is_recognized_but_others_and_aliases_are_not(self):
        contract = self.root / "tests/genlayer_direct/contracts/procurement_evaluation.py"
        original = contract.read_text(encoding="utf-8")
        official_directive = "py-genlayer:" + OFFICIAL_IMMUTABLE_RUNTIME
        self.assertIn(official_directive, original)
        report = self.report()
        self.assertEqual(self.by_id(report, "runtime.artifact_immutability")["status"], "verified")
        self.assertEqual(self.by_id(report, "runtime.target_compatibility")["status"], "unknown")
        self.assertFalse(report["ready_to_deploy"])
        for reference, expected in (("latest", "blocked"), ("0.2.9", "unknown"),
                                    ("sha256-" + "ab" * 32, "unknown")):
            with self.subTest(reference=reference):
                contract.write_text(original.replace(official_directive, "py-genlayer:" + reference), encoding="utf-8")
                report = self.report()
                self.assertEqual(self.by_id(report, "runtime.artifact_immutability")["status"], expected)
                self.assertEqual(self.by_id(report, "runtime.target_compatibility")["status"], "unknown")
                self.assertFalse(report["ready_to_deploy"])
        contract.write_text(original, encoding="utf-8")

    def test_inconsistent_runtime_and_build_lock_roots_are_blocked(self):
        lock = self.root / "requirements-genlayer.lock.txt"
        lock.write_text(lock.read_text(encoding="utf-8").replace("genlayer-py==0.19.0rc2", "genlayer-py==0.18.0"), encoding="utf-8")
        report = self.report()
        self.assertEqual(self.by_id(report, "dependencies.lock_consistency")["status"], "blocked")
        self.assertFalse(report["ready_to_deploy"])

    def test_network_configuration_presence_is_separate_from_remote_identity(self):
        report = self.report({"TENDERLENS_GENLAYER_NETWORK": "testnet_asimov"})
        self.assertEqual(self.by_id(report, "target.network_selection")["status"], "verified")
        self.assertEqual(self.by_id(report, "target.endpoint_configuration")["status"], "verified")
        self.assertEqual(self.by_id(report, "target.remote_identity")["status"], "unknown")
        for name, (chain_id, endpoint) in SUPPORTED_NETWORKS.items():
            self.assertIsInstance(chain_id, int)
            self.assertTrue(endpoint.startswith("https://"))
        self.assertIn("chain ID alone is insufficient", self.by_id(report, "target.remote_identity")["resolution"])

    def test_unknown_network_and_unpinned_rpc_override_fail_closed_without_echoing_url(self):
        secret = "rpc-secret-marker-do-not-print"
        private = "ab" * 32
        env = {
            "TENDERLENS_GENLAYER_NETWORK": "testnet_asimov",
            "TENDERLENS_GENLAYER_RPC_URL": "https://user:" + secret + "@rpc.example.invalid/path",
            "TENDERLENS_GENLAYER_PRIVATE_KEY": private,
        }
        report = self.report(env)
        rendered = format_report(report)
        self.assertEqual(self.by_id(report, "target.endpoint_configuration")["status"], "blocked")
        self.assertEqual(self.by_id(report, "target.remote_identity")["status"], "unknown")
        self.assertNotIn(secret, rendered)
        self.assertNotIn(private, rendered)
        self.assertEqual(self.by_id(report, "account.configuration_presence")["status"], "verified")
        self.assertEqual(self.by_id(report, "account.authorization")["status"], "unknown")

    def test_missing_environment_configuration_is_user_action_and_values_are_never_reported(self):
        secret = "cd" * 32
        report = self.report({"TENDERLENS_GENLAYER_PRIVATE_KEY": secret})
        self.assertEqual(self.by_id(report, "target.network_selection")["status"], "requires_user_action")
        self.assertEqual(self.by_id(report, "account.configuration_presence")["status"], "verified")
        self.assertEqual(self.by_id(report, "broadcast.explicit_opt_in")["status"], "requires_user_action")
        self.assertNotIn(secret, format_report(report))
        bad = self.report({"TENDERLENS_GENLAYER_PRIVATE_KEY": "not-a-key"})
        self.assertEqual(self.by_id(bad, "account.configuration_presence")["status"], "blocked")
        self.assertNotIn("not-a-key", format_report(bad))

    def test_missing_contract_evidence_blocks_required_files_runtime_and_configuration(self):
        (self.root / "tests/genlayer_direct/contracts/procurement_evaluation.py").unlink()
        (self.root / "backend/app.py").unlink()
        report = self.report()
        self.assertEqual(self.by_id(report, "project.required_files")["status"], "blocked")
        self.assertEqual(self.by_id(report, "runtime.artifact_immutability")["status"], "blocked")
        self.assertEqual(self.by_id(report, "contract.configuration_consistency")["status"], "blocked")
        self.assertFalse(report["ready_to_deploy"])

    def test_runtime_and_funding_gate_presence_does_not_masquerade_as_verifier_availability(self):
        report = self.report()
        self.assertEqual(self.by_id(report, "runtime.qualification_gate_present")["status"], "verified")
        self.assertEqual(self.by_id(report, "runtime.qualification_verifier_availability")["status"], "blocked")
        self.assertEqual(self.by_id(report, "funding.verifier_gate_present")["status"], "verified")
        self.assertEqual(self.by_id(report, "funding.verifier_availability")["status"], "blocked")
        self.assertEqual(self.by_id(report, "funding.confirmation")["status"], "unknown")

    def test_preflight_is_local_only_and_makes_no_network_or_broadcast_calls(self):
        with patch("socket.create_connection", side_effect=AssertionError("network access attempted")), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("socket connect attempted")), \
             patch.object(socket.socket, "sendto", side_effect=AssertionError("socket send attempted")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("URL request attempted")), \
             patch("subprocess.Popen", side_effect=AssertionError("subprocess attempted")), \
             patch("subprocess.run", side_effect=AssertionError("subprocess attempted")), \
             patch("os.system", side_effect=AssertionError("shell attempted")), \
             patch("os.popen", side_effect=AssertionError("shell attempted")), \
             patch.object(GenLayerSDKDriver, "_load_sdk", side_effect=AssertionError("SDK load attempted")), \
             patch.object(GenLayerSDKDriver, "_connect", side_effect=AssertionError("SDK client attempted")), \
             patch.object(GenLayerSDKDriver, "prepare", side_effect=AssertionError("broadcast preflight called")), \
             patch.object(GenLayerSDKDriver, "prepare_readonly", side_effect=AssertionError("SDK recovery called")), \
             patch.object(GenLayerSDKDriver, "__call__", side_effect=AssertionError("broadcast called")), \
             patch.object(GenLayerSDKDriver, "reconcile", side_effect=AssertionError("reconciliation called")), \
             patch.dict(os.environ, {}, clear=True):
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(["--root", str(ROOT)])
        self.assertEqual(status, 2)
        self.assertEqual(stderr.getvalue(), "")
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["ready_to_deploy"])
        self.assertEqual(report["external_side_effects_performed"], [])
        self.assertEqual(self.by_id(report, "preflight.side_effect_boundary")["status"], "verified")

    def test_network_probe_flag_is_not_available(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--root", str(ROOT), "--probe-rpc"])
        self.assertEqual(caught.exception.code, 2)

    def test_ci_coverage_check_requires_preflight_side_effect_test_and_recovery_tests(self):
        report = self.report()
        self.assertEqual(self.by_id(report, "tests.ci_coverage_configuration")["status"], "verified")
        self.assertEqual(self.by_id(report, "recovery.uncertain_transaction_safety")["status"], "verified")
        self.assertTrue(self.by_id(report, "tests.ci_coverage_configuration")["evidence"])

    def test_self_check_blocks_a_preflight_source_that_imports_network_client(self):
        script = self.root / "scripts/genlayer_preflight.py"
        script.write_text(script.read_text(encoding="utf-8") + "\nimport socket\n", encoding="utf-8")
        report = self.report()
        self.assertEqual(self.by_id(report, "preflight.side_effect_boundary")["status"], "blocked")
        self.assertFalse(report["ready_to_deploy"])


if __name__ == "__main__":
    unittest.main()
