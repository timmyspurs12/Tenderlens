"""Deterministic adversarial tests for the application-to-contract boundary."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.app import TenderLensAPI, canonical_snapshot_digest
from backend.evaluation_adapter import AdapterError, EvaluationAdapter

TOKEN = "t" * 48
ADDRESS_A = "0x" + "a" * 40
ADDRESS_B = "0x" + "b" * 40
TX_DEPLOY_A = "0x" + "1" * 64
TX_EVAL_A = "0x" + "2" * 64
TX_DEPLOY_B = "0x" + "3" * 64
TX_EVAL_B = "0x" + "4" * 64


def call_api(app, method, path, value=None):
    import io
    raw = b"" if value is None else json.dumps(value).encode()
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json", "HTTP_AUTHORIZATION": "Bearer " + TOKEN,
        "wsgi.input": io.BytesIO(raw),
    }
    result = {}
    body = b"".join(app(environ, lambda status, headers: result.update(status=status)))
    return int(result["status"].split()[0]), json.loads(body) if body else None


class EvaluationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.api = TenderLensAPI(root / "api.sqlite", root / "blobs", TOKEN, "adapter-ws")
        self.adapter = EvaluationAdapter(self.api)
        status, tender = call_api(self.api, "POST", "/api/tenders", {"title": "Test tender", "reference": "ADAPTER-1"})
        self.assertEqual(status, 201)
        self.tender_id = tender["id"]
        status, self.requirement = call_api(self.api, "POST", f"/api/tenders/{self.tender_id}/requirements", {
            "code": "R-1", "text": "Hold a valid licence.", "category": "eligibility", "mandatory": True, "kind": "judgment",
        })
        self.assertEqual(status, 201)

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self, policy="mandatory_gate_optional_advisory_v1"):
        status, value = call_api(self.api, "POST", f"/api/tenders/{self.tender_id}/evaluation-snapshots", {"aggregation_policy": policy})
        self.assertEqual(status, 201)
        return value

    def rewrite_authenticated_row(self, snapshot_id, mutate):
        with self.api.db() as conn:
            conn.execute("DROP TRIGGER evaluation_snapshots_no_update")
            row = conn.execute("SELECT * FROM evaluation_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            payload = json.loads(row["payload_json"])
            mutate(payload)
            payload["snapshot_sha256"] = canonical_snapshot_digest(payload)
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            tag = self.api.snapshot_auth_tag(row["id"], row["tender_id"], row["workspace"], payload["snapshot_sha256"], raw, row["created_at"])
            conn.execute("UPDATE evaluation_snapshots SET input_sha256=?,payload_json=?,auth_tag=? WHERE id=?", (payload["snapshot_sha256"], raw, tag, snapshot_id))
            conn.execute("CREATE TRIGGER evaluation_snapshots_no_update BEFORE UPDATE ON evaluation_snapshots BEGIN SELECT RAISE(ABORT, 'evaluation snapshots are immutable'); END")

    def submit_refs(self, request_id, address=ADDRESS_A, deploy_tx=TX_DEPLOY_A, eval_tx=TX_EVAL_A):
        self.assertTrue(self.adapter.claim_submission(request_id))
        lifecycle = self.adapter.lifecycle(request_id)
        lifecycle.deployment_started()
        lifecycle.deployment_submitted(deploy_tx)
        lifecycle.deployment_finalized(address)
        lifecycle.evaluation_started()
        lifecycle.evaluation_submitted(eval_tx)
        lifecycle.finalized()
        return self.adapter.get_request(request_id)

    def result_for(self, prepared):
        payload = json.loads(prepared["contract_input_json"])
        decisions = [{
            "requirement_id": item["id"], "code": item["code"], "mandatory": item["mandatory"],
            "status": "insufficient_evidence", "reason": "No evidence was supplied for this requirement; absence is not compliance.", "evidence_refs": [],
        } for item in payload["requirements"]]
        unresolved = [{"requirement_id": item["requirement_id"], "code": item["code"], "status": item["status"]}
                      for item in decisions]
        return json.dumps({
            "schema_version": "tenderlens.evaluation-result.v1",
            "evaluation_id": payload["evaluation_id"], "tender_id": payload["tender_id"],
            "snapshot_id": payload["snapshot_id"], "snapshot_sha256": payload["snapshot_sha256"],
            "aggregation_policy": payload["aggregation_policy"], "overall_status": "inconclusive",
            "decisions": decisions, "unresolved_requirements": unresolved, "disclaimer": "Evidence interpretation only; not legal advice and not an automatic procurement award.",
        }, sort_keys=True, separators=(",", ":"))

    def test_legacy_milestone5_ledger_migrates_uncertain_submission_conservatively(self):
        root = Path(self.tmp.name)
        db_path = root / "legacy.sqlite"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE evaluation_snapshots(id TEXT PRIMARY KEY,tender_id TEXT,workspace TEXT,input_sha256 TEXT,payload_json TEXT,created_at TEXT)")
            conn.execute("CREATE TABLE evaluation_requests(request_id TEXT PRIMARY KEY,workspace TEXT NOT NULL,idempotency_key_hash TEXT NOT NULL,snapshot_id TEXT NOT NULL REFERENCES evaluation_snapshots(id),input_sha256 TEXT NOT NULL,policy_version TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('prepared','submitting','submitted','completed')),contract_address TEXT,deployment_tx_hash TEXT,evaluation_tx_hash TEXT,result_json TEXT,result_reference TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(workspace,idempotency_key_hash))")
            conn.execute("INSERT INTO evaluation_snapshots VALUES(?,?,?,?,?,?)", ("10000000-0000-4000-8000-000000000099", "10000000-0000-4000-8000-000000000098", "legacy", "a" * 64, "{}", "2026-01-01T00:00:00+00:00"))
            conn.execute("INSERT INTO evaluation_requests(request_id,workspace,idempotency_key_hash,snapshot_id,input_sha256,policy_version,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", ("10000000-0000-4000-8000-000000000097", "legacy", "b" * 64, "10000000-0000-4000-8000-000000000099", "a" * 64, "mandatory_gate_optional_advisory_v1", "submitting", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        migrated = TenderLensAPI(db_path, root / "legacy-blobs", TOKEN, "legacy")
        with migrated.db() as conn:
            row = conn.execute("SELECT status,lifecycle_state,deployment_tx_hash FROM evaluation_requests WHERE request_id=?", ("10000000-0000-4000-8000-000000000097",)).fetchone()
        self.assertEqual(row["status"], "submitting")
        self.assertEqual(row["lifecycle_state"], "outcome_unknown")
        self.assertIsNone(row["deployment_tx_hash"])

    def test_valid_snapshot_builds_exact_stored_contract_input_and_tracks_metadata(self):
        snapshot = self.snapshot()
        prepared = self.adapter.prepare(snapshot["snapshot_id"], "retry-key-1", expected_policy="mandatory_gate_optional_advisory_v1")
        self.assertEqual(prepared["contract_input_json"], json.dumps(snapshot["submission"], sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        self.assertEqual(json.loads(prepared["contract_input_json"]), snapshot["submission"])
        self.assertEqual(prepared["snapshot_sha256"], snapshot["input_sha256"])
        self.assertEqual(prepared["policy_version"], "mandatory_gate_optional_advisory_v1")
        request = self.adapter.get_request(prepared["request_id"])
        self.assertEqual(request["status"], "prepared")
        self.assertEqual(request["snapshot_id"], snapshot["snapshot_id"])

    def test_arbitrary_client_payload_is_not_an_adapter_argument(self):
        snapshot = self.snapshot()
        with self.assertRaises(TypeError):
            self.adapter.prepare(snapshot["snapshot_id"], "key", payload={"requirements": []})
        prepared = self.adapter.prepare(snapshot["snapshot_id"], "key")
        self.assertEqual(json.loads(prepared["contract_input_json"]), snapshot["submission"])

    def test_submission_port_receives_verified_bytes_and_duplicate_call_is_idempotent(self):
        snapshot = self.snapshot()
        calls = []

        def submitter(contract_input_json, **metadata):
            calls.append((contract_input_json, metadata))
            lifecycle = metadata["lifecycle"]
            lifecycle.deployment_started()
            lifecycle.deployment_submitted(TX_DEPLOY_A)
            lifecycle.deployment_finalized(ADDRESS_A)
            lifecycle.evaluation_started()
            lifecycle.evaluation_submitted(TX_EVAL_A)
            return {"contract_address": ADDRESS_A, "deployment_tx_hash": TX_DEPLOY_A, "evaluation_tx_hash": TX_EVAL_A}

        first = self.adapter.submit(snapshot["snapshot_id"], "port-key", submitter)
        second = self.adapter.submit(snapshot["snapshot_id"], "port-key", submitter)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].contract_input_json, json.dumps(snapshot["submission"], sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        self.assertEqual(calls[0][1]["request_id"], first["request_id"])
        self.assertEqual(first["status"], "submitted")
        self.assertEqual(second["status"], "submitted")

    def test_ambiguous_submitter_failure_is_not_automatically_retried_or_leaked(self):
        snapshot = self.snapshot()
        attempts = []

        def failing_submitter(_contract_input_json, **_metadata):
            attempts.append(True)
            raise RuntimeError("sensitive calldata should not be returned")

        with self.assertRaisesRegex(AdapterError, "submission_outcome_unknown") as caught:
            self.adapter.submit(snapshot["snapshot_id"], "ambiguous", failing_submitter)
        self.assertNotIn("sensitive calldata", str(caught.exception))
        with self.assertRaisesRegex(AdapterError, "submission_outcome_unknown"):
            self.adapter.submit(snapshot["snapshot_id"], "ambiguous", failing_submitter)
        self.assertEqual(len(attempts), 1)

    def test_durable_phase_hashes_recovery_and_duplicate_result_ingestion(self):
        snapshot = self.snapshot()
        prepared = self.adapter.prepare(snapshot["snapshot_id"], "lifecycle")
        self.assertTrue(self.adapter.claim_submission(prepared["request_id"]))
        lifecycle = self.adapter.lifecycle(prepared["request_id"])
        lifecycle.record_network("testnet_asimov", 4221)
        lifecycle.deployment_started()
        lifecycle.deployment_submitted(TX_DEPLOY_A)
        durable = self.adapter.get_request(prepared["request_id"])
        self.assertEqual(durable["deployment_tx_hash"], TX_DEPLOY_A)
        self.assertEqual(durable["lifecycle_state"], "deployment_submitted")
        lifecycle.pending("PENDING")
        self.assertEqual(self.adapter.get_request(prepared["request_id"])["last_network_status"], "PENDING")
        lifecycle.deployment_finalized(ADDRESS_A)
        lifecycle.evaluation_started()
        lifecycle.evaluation_submitted(TX_EVAL_A)
        lifecycle.pending("ACCEPTED")
        lifecycle.finalized()
        lifecycle.finalized()  # Duplicate status polling/finalization is idempotent.
        result = self.result_for(prepared)
        accepted = self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, result, TX_EVAL_A)
        self.assertEqual(accepted["status"], "completed")
        self.assertEqual(self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, result, TX_EVAL_A), accepted)

    def test_tx_hash_database_write_failure_leaves_unknown_and_never_retries(self):
        snapshot = self.snapshot()
        calls = []
        def submitter(_input, lifecycle, **_metadata):
            calls.append(True)
            lifecycle.deployment_started()
            with self.api.db() as conn:
                conn.execute("CREATE TRIGGER fail_deployment_hash BEFORE UPDATE ON evaluation_requests "
                             "WHEN NEW.lifecycle_state='deployment_submitted' BEGIN SELECT RAISE(ABORT, 'simulated disk boundary'); END")
            lifecycle.deployment_submitted(TX_DEPLOY_A)
        with self.assertRaisesRegex(AdapterError, "submission_outcome_unknown"):
            self.adapter.submit(snapshot["snapshot_id"], "db-boundary", submitter)
        request = self.adapter.get_request(self.adapter.prepare(snapshot["snapshot_id"], "db-boundary")["request_id"])
        self.assertEqual(request["lifecycle_state"], "outcome_unknown")
        self.assertIsNone(request["deployment_tx_hash"])
        with self.assertRaisesRegex(AdapterError, "submission_outcome_unknown"):
            self.adapter.submit(snapshot["snapshot_id"], "db-boundary", submitter)
        self.assertEqual(len(calls), 1)

    def test_known_hash_after_ambiguous_poll_is_reconciled_not_resubmitted(self):
        snapshot = self.snapshot()
        calls = []
        def submitter(_input, lifecycle, **_metadata):
            calls.append(True)
            lifecycle.deployment_started()
            lifecycle.deployment_submitted(TX_DEPLOY_A)
            raise RuntimeError("network timeout")
        with self.assertRaisesRegex(AdapterError, "submission_outcome_unknown"):
            self.adapter.submit(snapshot["snapshot_id"], "known-hash", submitter)
        request = self.adapter.prepare(snapshot["snapshot_id"], "known-hash")
        self.assertEqual(request["lifecycle_state"], "outcome_unknown")
        self.assertEqual(request["deployment_tx_hash"], TX_DEPLOY_A)
        self.adapter.submit(snapshot["snapshot_id"], "known-hash", submitter)
        self.assertEqual(len(calls), 1)

    def test_modified_payload_with_unchanged_digest_fails_closed(self):
        snapshot = self.snapshot()
        with self.api.db() as conn:
            conn.execute("DROP TRIGGER evaluation_snapshots_no_update")
            row = conn.execute("SELECT payload_json FROM evaluation_snapshots WHERE id=?", (snapshot["snapshot_id"],)).fetchone()
            payload = json.loads(row["payload_json"])
            payload["requirements"][0]["text"] = "Attacker substitution."
            conn.execute("UPDATE evaluation_snapshots SET payload_json=?,auth_tag='invalid' WHERE id=?", (json.dumps(payload), snapshot["snapshot_id"]))
        with self.assertRaisesRegex(AdapterError, "integrity_failure"):
            self.adapter.prepare(snapshot["snapshot_id"], "tampered")

    def test_modified_row_with_invalid_hmac_fails_closed(self):
        snapshot = self.snapshot()
        with self.api.db() as conn:
            conn.execute("DROP TRIGGER evaluation_snapshots_no_update")
            conn.execute("UPDATE evaluation_snapshots SET input_sha256=?,auth_tag='0' WHERE id=?", ("0" * 64, snapshot["snapshot_id"]))
        with self.assertRaisesRegex(AdapterError, "integrity_failure"):
            self.adapter.prepare(snapshot["snapshot_id"], "bad-hmac")

    def test_missing_malformed_and_unknown_snapshot_ids_fail_closed(self):
        for value in (None, "not-a-uuid", "10000000-0000-4000-8000-000000000099"):
            with self.subTest(snapshot_id=value), self.assertRaises(AdapterError):
                self.adapter.prepare(value, "key")

    def test_policy_mismatch_unsupported_schema_and_malformed_snapshot_are_rejected(self):
        snapshot = self.snapshot("all_requirements_blocking_v1")
        with self.assertRaisesRegex(AdapterError, "policy_version_mismatch"):
            self.adapter.prepare(snapshot["snapshot_id"], "policy", expected_policy="mandatory_gate_optional_advisory_v1")
        self.rewrite_authenticated_row(snapshot["snapshot_id"], lambda payload: payload.update(schema_version="tenderlens.evaluation-submission.v99"))
        with self.assertRaisesRegex(AdapterError, "unsupported_submission_schema"):
            self.adapter.prepare(snapshot["snapshot_id"], "schema")
        self.rewrite_authenticated_row(snapshot["snapshot_id"], lambda payload: payload.update(schema_version="tenderlens.evaluation-submission.v1", requirements=[]))
        with self.assertRaisesRegex(AdapterError, "malformed_requirements"):
            self.adapter.prepare(snapshot["snapshot_id"], "malformed")

    def test_stale_snapshot_is_rejected_when_age_limit_is_configured(self):
        snapshot = self.snapshot()
        stale = EvaluationAdapter(self.api, max_snapshot_age=__import__("datetime").timedelta(seconds=-1))
        with self.assertRaisesRegex(AdapterError, "stale_snapshot"):
            stale.prepare(snapshot["snapshot_id"], "old")

    def test_payload_digest_relationship_is_checked_before_constructor_input(self):
        snapshot = self.snapshot()
        with self.api.db() as conn:
            conn.execute("DROP TRIGGER evaluation_snapshots_no_update")
            conn.execute("UPDATE evaluation_snapshots SET input_sha256=? WHERE id=?", ("0" * 64, snapshot["snapshot_id"]))
        with self.assertRaisesRegex(AdapterError, "integrity_failure"):
            self.adapter.prepare(snapshot["snapshot_id"], "digest")

    def test_idempotency_duplicate_submission_and_attempt_claim_are_safe(self):
        snapshot = self.snapshot()
        first = self.adapter.prepare(snapshot["snapshot_id"], "same-key")
        retry = self.adapter.prepare(snapshot["snapshot_id"], "same-key")
        self.assertEqual(first["request_id"], retry["request_id"])
        self.assertEqual(retry["status"], "prepared")
        self.assertTrue(self.adapter.claim_submission(first["request_id"]))
        self.assertFalse(self.adapter.claim_submission(first["request_id"]))
        lifecycle = self.adapter.lifecycle(first["request_id"])
        lifecycle.deployment_started()
        lifecycle.deployment_submitted(TX_DEPLOY_A)
        lifecycle.deployment_finalized(ADDRESS_A)
        lifecycle.evaluation_started()
        lifecycle.evaluation_submitted(TX_EVAL_A)
        self.assertEqual(self.adapter.prepare(snapshot["snapshot_id"], "same-key")["status"], "submitted")
        with self.assertRaisesRegex(AdapterError, "snapshot_already_requested"):
            self.adapter.prepare(snapshot["snapshot_id"], "different-key")
        with self.assertRaisesRegex(AdapterError, "idempotency_key_conflict"):
            self.adapter.prepare(self.snapshot()["snapshot_id"], "same-key")

    def test_submission_references_are_required_valid_and_conflicts_fail(self):
        snapshot = self.snapshot()
        prepared = self.adapter.prepare(snapshot["snapshot_id"], "refs")
        lifecycle = self.adapter.lifecycle(prepared["request_id"])
        with self.assertRaisesRegex(AdapterError, "invalid_lifecycle_transition"):
            lifecycle.deployment_started()
        self.assertTrue(self.adapter.claim_submission(prepared["request_id"]))
        lifecycle.deployment_started()
        with self.assertRaisesRegex(AdapterError, "invalid_deployment_tx_hash"):
            lifecycle.deployment_submitted("missing")
        lifecycle.deployment_submitted(TX_DEPLOY_A)
        with self.assertRaisesRegex(AdapterError, "lifecycle_reference_conflict"):
            lifecycle.deployment_submitted(TX_DEPLOY_B)
        lifecycle.deployment_finalized(ADDRESS_A)
        with self.assertRaisesRegex(AdapterError, "invalid_contract_address"):
            lifecycle.deployment_finalized("missing")
        lifecycle.evaluation_started()
        with self.assertRaisesRegex(AdapterError, "invalid_evaluation_tx_hash"):
            lifecycle.evaluation_submitted("missing")
        lifecycle.evaluation_submitted(TX_EVAL_A)
        with self.assertRaisesRegex(AdapterError, "request_not_finalized"):
            self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, self.result_for(prepared))
        with self.assertRaisesRegex(AdapterError, "lifecycle_reference_conflict"):
            lifecycle.evaluation_submitted(TX_EVAL_B)

    def test_result_must_match_request_snapshot_policy_and_actual_recorded_refs(self):
        snapshot = self.snapshot()
        prepared = self.adapter.prepare(snapshot["snapshot_id"], "result")
        self.submit_refs(prepared["request_id"])
        result = json.loads(self.result_for(prepared))
        result["evaluation_id"] = "99999999-9999-4999-8999-999999999999"
        with self.assertRaisesRegex(AdapterError, "result_identity_mismatch"):
            self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, json.dumps(result))
        with self.assertRaisesRegex(AdapterError, "result_transaction_mismatch"):
            self.adapter.accept_result(prepared["request_id"], ADDRESS_B, TX_EVAL_B, self.result_for(prepared))
        with self.assertRaisesRegex(AdapterError, "invalid_result_reference"):
            self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, self.result_for(prepared), "")

    def test_stale_result_from_another_snapshot_is_rejected(self):
        old = self.snapshot()
        old_request = self.adapter.prepare(old["snapshot_id"], "old")
        self.submit_refs(old_request["request_id"], ADDRESS_A, TX_DEPLOY_A, TX_EVAL_A)
        current = self.snapshot()
        new_request = self.adapter.prepare(current["snapshot_id"], "new")
        self.submit_refs(new_request["request_id"], ADDRESS_B, TX_DEPLOY_B, TX_EVAL_B)
        with self.assertRaisesRegex(AdapterError, "result_transaction_mismatch"):
            self.adapter.accept_result(new_request["request_id"], ADDRESS_B, TX_EVAL_A, self.result_for(old_request))
        with self.assertRaisesRegex(AdapterError, "result_identity_mismatch"):
            self.adapter.accept_result(new_request["request_id"], ADDRESS_B, TX_EVAL_B, self.result_for(old_request))

    def test_result_retry_is_idempotent_and_conflicting_result_fails(self):
        snapshot = self.snapshot()
        prepared = self.adapter.prepare(snapshot["snapshot_id"], "finish")
        self.submit_refs(prepared["request_id"])
        result = self.result_for(prepared)
        accepted = self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, result, TX_EVAL_A)
        self.assertEqual(accepted["status"], "completed")
        self.assertEqual(self.adapter.get_result(prepared["request_id"]), json.loads(result))
        self.assertEqual(self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, result, TX_EVAL_A), accepted)
        with self.assertRaisesRegex(AdapterError, "conflicting_result_retry"):
            self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, result, "different-reference")

    def test_missing_and_conflicting_result_identifiers_fail(self):
        with self.assertRaisesRegex(AdapterError, "request_not_found"):
            self.adapter.get_request("10000000-0000-4000-8000-000000000001")
        snapshot = self.snapshot()
        prepared = self.adapter.prepare(snapshot["snapshot_id"], "missing-result")
        not_submitted_result = json.dumps({"schema_version": "tenderlens.evaluation-result.v1"})
        with self.assertRaisesRegex(AdapterError, "request_not_submitted"):
            self.adapter.accept_result(prepared["request_id"], ADDRESS_A, TX_EVAL_A, not_submitted_result)


if __name__ == "__main__":
    unittest.main()
