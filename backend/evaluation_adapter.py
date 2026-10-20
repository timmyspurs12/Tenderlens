"""Application-level binding from authenticated snapshots to contract inputs/results.

This module has no built-in GenLayer client or network configuration. Its `submit`
method delegates exact verified constructor JSON to an injected runtime-specific port.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.app import AGGREGATION_POLICIES, TenderLensAPI, valid_id

RESULT_SCHEMA = "tenderlens.evaluation-result.v1"
SUBMISSION_SCHEMA = "tenderlens.evaluation-submission.v1"
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
CONTRACT_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class AdapterError(ValueError):
    """Safe integration failure; messages contain identifiers/codes, never payloads."""


def _strict_json_object(raw: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AdapterError("duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except AdapterError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterError("invalid_json") from exc
    if not isinstance(value, dict):
        raise AdapterError("json_object_required")
    return value


class EvaluationAdapter:
    """Durable request ledger and verified constructor-input boundary.

    `prepare` accepts only a backend snapshot ID plus an idempotency key. It reads
    the row through the same authenticated snapshot route used by API retrieval and
    returns the exact stored JSON string, not a caller-provided/re-serialized payload.
    The adapter is not a network submitter or receipt verifier.
    """

    def __init__(self, api: TenderLensAPI, max_snapshot_age: timedelta | None = None):
        self.api = api
        self.max_snapshot_age = max_snapshot_age

    @staticmethod
    def _key_hash(workspace: str, key: str) -> str:
        return hashlib.sha256((workspace + "\0" + key).encode("utf-8")).hexdigest()

    def _verified_snapshot(self, snapshot_id: str, expected_policy: str | None):
        if not isinstance(snapshot_id, str) or not valid_id(snapshot_id):
            raise AdapterError("invalid_snapshot_id")
        with self.api.db() as conn:
            row = conn.execute(
                "SELECT tender_id FROM evaluation_snapshots WHERE id=? AND workspace=?",
                (snapshot_id, self.api.workspace),
            ).fetchone()
            if row is None:
                raise AdapterError("snapshot_not_found")
            tender_id = row["tender_id"]

        # Exercise the same bearer-authenticated WSGI route used by API clients.
        # The internal token remains in process memory and is never included in
        # errors, logs, or the returned contract input.
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": f"/api/tenders/{tender_id}/evaluation-snapshots/{snapshot_id}",
            "CONTENT_LENGTH": "0",
            "CONTENT_TYPE": "application/json",
            "HTTP_AUTHORIZATION": "Bearer " + self.api.token,
            "wsgi.input": io.BytesIO(b""),
        }
        wsgi_response = {}
        raw_response = b"".join(self.api(environ, lambda status, headers: wsgi_response.update(status=status)))
        try:
            status_code = int(wsgi_response["status"].split()[0])
            response = json.loads(raw_response) if raw_response else {}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("snapshot_verification_failed") from exc
        if status_code != 200:
            code = response.get("error", {}).get("code", "snapshot_verification_failed")
            raise AdapterError(code)

        # Read the persisted serialization only after the authenticated response,
        # and verify again so a concurrent privileged DB edit cannot race binding.
        with self.api.db() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_snapshots WHERE id=? AND workspace=?",
                (snapshot_id, self.api.workspace),
            ).fetchone()
            if row is None or not self.api.snapshot_row_is_valid(row):
                raise AdapterError("integrity_failure")
            raw_payload = row["payload_json"]
            if not isinstance(raw_payload, str) or len(raw_payload) > 250_000:
                raise AdapterError("snapshot_exceeds_contract_limit")
            submission = _strict_json_object(raw_payload)
            if (row["tender_id"] != tender_id or response.get("snapshot_id") != snapshot_id
                    or response.get("input_sha256") != row["input_sha256"]
                    or response.get("submission") != submission):
                raise AdapterError("snapshot_changed_during_retrieval")

        if submission.get("schema_version") != SUBMISSION_SCHEMA:
            raise AdapterError("unsupported_submission_schema")
        expected_fields = {
            "schema_version", "evaluation_id", "snapshot_id", "snapshot_sha256",
            "tender_id", "aggregation_policy", "requirements", "evidence",
        }
        if set(submission) != expected_fields:
            raise AdapterError("malformed_submission")
        if submission.get("snapshot_id") != snapshot_id or response.get("snapshot_id") != snapshot_id:
            raise AdapterError("snapshot_identity_mismatch")
        if submission.get("snapshot_sha256") != response.get("input_sha256"):
            raise AdapterError("snapshot_digest_mismatch")
        if not all(isinstance(submission.get(field), str) and submission[field] == submission[field].lower() and valid_id(submission[field]) for field in ("evaluation_id", "tender_id")):
            raise AdapterError("malformed_submission_identity")
        policy = submission.get("aggregation_policy")
        if policy not in AGGREGATION_POLICIES:
            raise AdapterError("unsupported_policy_version")
        if expected_policy is not None and expected_policy != policy:
            raise AdapterError("policy_version_mismatch")
        requirements, evidence = submission.get("requirements"), submission.get("evidence")
        if not isinstance(requirements, list) or not 1 <= len(requirements) <= 50:
            raise AdapterError("malformed_requirements")
        if not isinstance(evidence, list) or len(evidence) > 100:
            raise AdapterError("malformed_evidence")
        requirement_ids = set()
        for item in requirements:
            if not isinstance(item, dict) or set(item) != {"id", "code", "text", "category", "mandatory", "kind"}:
                raise AdapterError("malformed_requirement")
            if not isinstance(item.get("id"), str) or item["id"] != item["id"].lower() or not valid_id(item["id"]) or item["id"] in requirement_ids:
                raise AdapterError("malformed_requirement_id")
            requirement_ids.add(item["id"])
            if (not isinstance(item.get("code"), str) or not item["code"].strip() or len(item["code"]) > 80
                    or not isinstance(item.get("text"), str) or not item["text"].strip() or len(item["text"]) > 4000
                    or item.get("category") not in ("eligibility", "mandatory_documentation", "technical", "interpretive")
                    or type(item.get("mandatory")) is not bool or item.get("kind") not in ("objective", "judgment")):
                raise AdapterError("malformed_requirement")
        evidence_ids = set()
        evidence_fields = {
            "id", "requirement_id", "document_id", "document_version", "document_sha256",
            "filename", "start_byte", "end_byte", "excerpt", "provenance", "created_at",
        }
        for item in evidence:
            if not isinstance(item, dict) or set(item) != evidence_fields:
                raise AdapterError("malformed_evidence_item")
            if (not isinstance(item.get("id"), str) or item["id"] != item["id"].lower() or not valid_id(item["id"]) or item["id"] in evidence_ids
                    or (item.get("requirement_id") is not None and item["requirement_id"] not in requirement_ids)
                    or not isinstance(item.get("document_id"), str) or item["document_id"] != item["document_id"].lower() or not valid_id(item["document_id"])
                    or type(item.get("document_version")) is not int or item["document_version"] < 1
                    or not isinstance(item.get("document_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["document_sha256"])
                    or not isinstance(item.get("filename"), str) or not item["filename"].strip() or len(item["filename"]) > 180
                    or type(item.get("start_byte")) is not int or type(item.get("end_byte")) is not int
                    or item["start_byte"] < 0 or item["end_byte"] <= item["start_byte"]
                    or not isinstance(item.get("excerpt"), str) or not item["excerpt"] or len(item["excerpt"]) > 8000
                    or item["end_byte"] - item["start_byte"] != len(item["excerpt"].encode("utf-8"))
                    or not isinstance(item.get("provenance"), dict)
                    or not isinstance(item.get("created_at"), str)):
                raise AdapterError("malformed_evidence_item")
            provenance = item["provenance"]
            provenance_fields = {"document_id", "document_sha256", "document_version", "filename", "start_byte", "end_byte", "retrieved_at", "integrity_check", "source_authenticity"}
            if (set(provenance) != provenance_fields
                    or any(provenance[field] != item[field] for field in ("document_id", "document_sha256", "document_version", "filename", "start_byte", "end_byte"))
                    or provenance.get("integrity_check") != "sha256_match"
                    or provenance.get("source_authenticity") != "not_independently_verified"
                    or not isinstance(provenance.get("retrieved_at"), str)):
                raise AdapterError("malformed_evidence_provenance")
            evidence_ids.add(item["id"])

        try:
            created_at = datetime.fromisoformat(response["created_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("invalid_snapshot_timestamp") from exc
        if created_at.tzinfo is None:
            raise AdapterError("invalid_snapshot_timestamp")
        age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
        if age < -timedelta(minutes=5):
            raise AdapterError("invalid_snapshot_timestamp")
        if self.max_snapshot_age is not None and age > self.max_snapshot_age:
            raise AdapterError("stale_snapshot")
        return row, response, raw_payload, submission

    def prepare(self, snapshot_id: str, idempotency_key: str, expected_policy: str | None = None) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
            raise AdapterError("invalid_idempotency_key")
        key_hash = self._key_hash(self.api.workspace, idempotency_key)
        row, response, raw_payload, submission = self._verified_snapshot(snapshot_id, expected_policy)
        with self.api.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM evaluation_requests WHERE workspace=? AND idempotency_key_hash=?",
                (self.api.workspace, key_hash),
            ).fetchone()
            if prior is not None:
                if prior["snapshot_id"] != snapshot_id or prior["input_sha256"] != response["input_sha256"] or prior["policy_version"] != submission["aggregation_policy"]:
                    raise AdapterError("idempotency_key_conflict")
                request = prior
            else:
                existing_snapshot_request = conn.execute(
                    "SELECT request_id FROM evaluation_requests WHERE workspace=? AND snapshot_id=?",
                    (self.api.workspace, snapshot_id),
                ).fetchone()
                if existing_snapshot_request is not None:
                    raise AdapterError("snapshot_already_requested")
                request_id = str(uuid.uuid4())
                timestamp = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO evaluation_requests(request_id,workspace,idempotency_key_hash,snapshot_id,input_sha256,policy_version,status,created_at,updated_at) VALUES(?,?,?,?,?,?, 'prepared',?,?)",
                    (request_id, self.api.workspace, key_hash, snapshot_id, response["input_sha256"], submission["aggregation_policy"], timestamp, timestamp),
                )
                request = conn.execute("SELECT * FROM evaluation_requests WHERE request_id=?", (request_id,)).fetchone()
        return {
            "request_id": request["request_id"],
            "snapshot_id": request["snapshot_id"],
            "snapshot_sha256": request["input_sha256"],
            "policy_version": request["policy_version"],
            "status": request["status"],
            "contract_input_json": raw_payload,
            "created_at": request["created_at"],
        }

    def submit(self, snapshot_id: str, idempotency_key: str, submitter, expected_policy: str | None = None) -> dict[str, Any]:
        """Run the application's submission port with only verified stored JSON.

        `submitter` must perform the runtime-specific deploy/evaluate steps and return
        real references. This library intentionally has no default network client.
        """
        if not callable(submitter):
            raise AdapterError("submitter_required")
        prepared = self.prepare(snapshot_id, idempotency_key, expected_policy)
        if prepared["status"] in ("submitted", "completed"):
            return {key: value for key, value in prepared.items() if key != "contract_input_json"}
        if not self.claim_submission(prepared["request_id"]):
            # A prior process may have sent a transaction before it crashed. Never
            # retry the external side effect blindly; reconcile by request ID.
            raise AdapterError("submission_outcome_unknown")
        try:
            references = submitter(
                prepared["contract_input_json"],
                request_id=prepared["request_id"],
                snapshot_id=prepared["snapshot_id"],
                snapshot_sha256=prepared["snapshot_sha256"],
                policy_version=prepared["policy_version"],
            )
        except Exception:
            # Do not interpolate provider exceptions: they may contain calldata or
            # evidence. The persisted 'submitting' status signals reconciliation.
            raise AdapterError("submission_outcome_unknown") from None
        if not isinstance(references, dict) or set(references) != {
            "contract_address", "deployment_tx_hash", "evaluation_tx_hash",
        }:
            raise AdapterError("invalid_submission_references")
        submitted = self.record_submission(
            prepared["request_id"], references["contract_address"],
            references["deployment_tx_hash"], references["evaluation_tx_hash"],
        )
        return {**submitted, "snapshot_id": prepared["snapshot_id"], "snapshot_sha256": prepared["snapshot_sha256"], "policy_version": prepared["policy_version"]}

    @staticmethod
    def _validate_tx_ref(value: str, name: str) -> None:
        if not isinstance(value, str) or not TX_HASH.fullmatch(value):
            raise AdapterError("invalid_" + name)

    def claim_submission(self, request_id: str) -> bool:
        """Atomically claim one submission attempt; retries never auto-resubmit.

        A process crash after the external call remains an uncertain outcome and must
        be reconciled using the idempotency/request ID with the runtime operator.
        """
        if not isinstance(request_id, str) or not valid_id(request_id):
            raise AdapterError("invalid_request_id")
        with self.api.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM evaluation_requests WHERE request_id=? AND workspace=?", (request_id, self.api.workspace)).fetchone()
            if row is None:
                raise AdapterError("request_not_found")
            if row["status"] == "prepared":
                conn.execute("UPDATE evaluation_requests SET status='submitting',updated_at=? WHERE request_id=?", (datetime.now(timezone.utc).isoformat(), request_id))
                return True
            return False

    def record_submission(self, request_id: str, contract_address: str, deployment_tx_hash: str, evaluation_tx_hash: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not valid_id(request_id):
            raise AdapterError("invalid_request_id")
        if not isinstance(contract_address, str) or not CONTRACT_ADDRESS.fullmatch(contract_address):
            raise AdapterError("invalid_contract_address")
        self._validate_tx_ref(deployment_tx_hash, "deployment_tx_hash")
        self._validate_tx_ref(evaluation_tx_hash, "evaluation_tx_hash")
        with self.api.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM evaluation_requests WHERE request_id=? AND workspace=?", (request_id, self.api.workspace)).fetchone()
            if row is None:
                raise AdapterError("request_not_found")
            refs = (contract_address.lower(), deployment_tx_hash.lower(), evaluation_tx_hash.lower())
            current = tuple((row[name] or "").lower() for name in ("contract_address", "deployment_tx_hash", "evaluation_tx_hash"))
            if row["status"] in ("submitted", "completed"):
                if current != refs:
                    raise AdapterError("submission_reference_conflict")
                status = row["status"]
            elif row["status"] == "submitting":
                conn.execute(
                    "UPDATE evaluation_requests SET status='submitted',contract_address=?,deployment_tx_hash=?,evaluation_tx_hash=?,updated_at=? WHERE request_id=?",
                    (*refs, datetime.now(timezone.utc).isoformat(), request_id),
                )
                status = "submitted"
            else:
                raise AdapterError("submission_not_claimed")
        return {"request_id": request_id, "status": status, "contract_address": refs[0], "deployment_tx_hash": refs[1], "evaluation_tx_hash": refs[2]}

    @staticmethod
    def _validate_result_body(result: dict[str, Any], submission: dict[str, Any]) -> None:
        expected_fields = {
            "schema_version", "evaluation_id", "tender_id", "snapshot_id", "snapshot_sha256",
            "aggregation_policy", "overall_status", "decisions", "unresolved_requirements", "disclaimer",
        }
        if set(result) != expected_fields or result.get("disclaimer") != "Evidence interpretation only; not legal advice and not an automatic procurement award.":
            raise AdapterError("malformed_result")
        requirements = {item["id"]: item for item in submission["requirements"]}
        evidence = {item["id"]: item for item in submission["evidence"]}
        decisions = result.get("decisions")
        if not isinstance(decisions, list) or len(decisions) != len(requirements):
            raise AdapterError("malformed_result_decisions")
        by_id = {}
        ref_fields = {"evidence_id", "document_id", "document_version", "document_sha256", "filename", "start_byte", "end_byte"}
        for decision in decisions:
            if not isinstance(decision, dict) or set(decision) != {"requirement_id", "code", "mandatory", "status", "reason", "evidence_refs"}:
                raise AdapterError("malformed_result_decision")
            req_id = decision.get("requirement_id")
            req = requirements.get(req_id)
            if req is None or req_id in by_id or decision.get("code") != req["code"] or decision.get("mandatory") is not req["mandatory"]:
                raise AdapterError("result_requirement_mismatch")
            if decision.get("status") not in {"compliant", "non_compliant", "insufficient_evidence", "contradictory_evidence"} or not isinstance(decision.get("reason"), str):
                raise AdapterError("malformed_result_decision")
            refs = decision.get("evidence_refs")
            if not isinstance(refs, list):
                raise AdapterError("malformed_result_evidence_refs")
            seen_refs = set()
            for ref in refs:
                if not isinstance(ref, dict) or set(ref) != ref_fields:
                    raise AdapterError("malformed_result_evidence_ref")
                source = evidence.get(ref.get("evidence_id"))
                if source is None or ref["evidence_id"] in seen_refs:
                    raise AdapterError("result_cites_unsubmitted_evidence")
                expected_ref = {
                    "evidence_id": source["id"], "document_id": source["document_id"],
                    "document_version": source["document_version"], "document_sha256": source["document_sha256"],
                    "filename": source["filename"], "start_byte": source["start_byte"], "end_byte": source["end_byte"],
                }
                if ref != expected_ref or (source["requirement_id"] is not None and source["requirement_id"] != req_id):
                    raise AdapterError("result_evidence_reference_mismatch")
                seen_refs.add(ref["evidence_id"])
            candidates = [item for item in submission["evidence"] if item["requirement_id"] is None or item["requirement_id"] == req_id]
            if not candidates and (decision["status"] != "insufficient_evidence" or refs or decision["reason"] != "No evidence was supplied for this requirement; absence is not compliance."):
                raise AdapterError("result_without_evidence_mismatch")
            if candidates and not refs:
                raise AdapterError("result_missing_evidence_citation")
            by_id[req_id] = decision

        if [decision["requirement_id"] for decision in decisions] != list(requirements):
            raise AdapterError("result_requirement_order_mismatch")
        expected_unresolved = [
            {"requirement_id": decision["requirement_id"], "code": decision["code"], "status": decision["status"]}
            for decision in decisions if decision["status"] in ("insufficient_evidence", "contradictory_evidence")
        ]
        if result.get("unresolved_requirements") != expected_unresolved:
            raise AdapterError("result_unresolved_mismatch")
        statuses = [decision["status"] for decision in decisions]
        has_unresolved = any(status in ("insufficient_evidence", "contradictory_evidence") for status in statuses)
        has_noncompliance = "non_compliant" in statuses
        has_mandatory_noncompliance = any(decision["mandatory"] and decision["status"] == "non_compliant" for decision in decisions)
        has_optional_noncompliance = any(not decision["mandatory"] and decision["status"] == "non_compliant" for decision in decisions)
        if submission["aggregation_policy"] == "all_requirements_blocking_v1" and has_noncompliance:
            overall = "non_compliant"
        elif has_mandatory_noncompliance:
            overall = "mandatory_non_compliance"
        elif has_unresolved:
            overall = "inconclusive"
        elif submission["aggregation_policy"] == "mandatory_gate_optional_advisory_v1" and has_optional_noncompliance:
            overall = "optional_non_compliance"
        else:
            overall = "compliant"
        if result.get("overall_status") != overall:
            raise AdapterError("result_policy_aggregation_mismatch")

    def accept_result(self, request_id: str, contract_address: str, evaluation_tx_hash: str, result_json: str, result_reference: str | None = None) -> dict[str, Any]:
        if not isinstance(request_id, str) or not valid_id(request_id):
            raise AdapterError("invalid_request_id")
        if not isinstance(contract_address, str) or not CONTRACT_ADDRESS.fullmatch(contract_address):
            raise AdapterError("invalid_contract_address")
        self._validate_tx_ref(evaluation_tx_hash, "evaluation_tx_hash")
        if not isinstance(result_json, str) or len(result_json.encode("utf-8")) > 250_000:
            raise AdapterError("invalid_result_size")
        if result_reference is not None and (not isinstance(result_reference, str) or not result_reference.strip() or len(result_reference) > 256):
            raise AdapterError("invalid_result_reference")
        result = _strict_json_object(result_json)
        if result.get("schema_version") != RESULT_SCHEMA:
            raise AdapterError("unsupported_result_schema")
        with self.api.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            request = conn.execute("SELECT * FROM evaluation_requests WHERE request_id=? AND workspace=?", (request_id, self.api.workspace)).fetchone()
            if request is None:
                raise AdapterError("request_not_found")
            if request["status"] not in ("submitted", "completed"):
                raise AdapterError("request_not_submitted")
            if request["contract_address"].lower() != contract_address.lower() or request["evaluation_tx_hash"].lower() != evaluation_tx_hash.lower():
                raise AdapterError("result_transaction_mismatch")
            # Re-verify the snapshot at result-consumption time, not only at prepare.
            snap_row, snap_response, _, submission = self._verified_snapshot(request["snapshot_id"], request["policy_version"])
            del snap_row
            expected = {
                "evaluation_id": submission["evaluation_id"],
                "tender_id": submission["tender_id"],
                "snapshot_id": request["snapshot_id"],
                "snapshot_sha256": request["input_sha256"],
                "aggregation_policy": request["policy_version"],
            }
            if request["input_sha256"] != snap_response["input_sha256"]:
                raise AdapterError("snapshot_changed_since_request")
            if any(result.get(field) != value for field, value in expected.items()):
                raise AdapterError("result_identity_mismatch")
            self._validate_result_body(result, submission)
            if request["status"] == "completed":
                if request["result_json"] != result_json or request["result_reference"] != result_reference:
                    raise AdapterError("conflicting_result_retry")
            else:
                conn.execute(
                    "UPDATE evaluation_requests SET status='completed',result_json=?,result_reference=?,updated_at=? WHERE request_id=?",
                    (result_json, result_reference, datetime.now(timezone.utc).isoformat(), request_id),
                )
        return {"request_id": request_id, "snapshot_id": request["snapshot_id"], "snapshot_sha256": request["input_sha256"], "status": "completed", "result_reference": result_reference}

    def get_result(self, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not valid_id(request_id):
            raise AdapterError("invalid_request_id")
        with self.api.db() as conn:
            row = conn.execute("SELECT * FROM evaluation_requests WHERE request_id=? AND workspace=?", (request_id, self.api.workspace)).fetchone()
            if row is None:
                raise AdapterError("request_not_found")
            if row["status"] != "completed" or row["result_json"] is None:
                raise AdapterError("result_not_available")
            if not row["contract_address"] or not row["evaluation_tx_hash"]:
                raise AdapterError("result_transaction_missing")
            result = _strict_json_object(row["result_json"])
        _, response, _, submission = self._verified_snapshot(row["snapshot_id"], row["policy_version"])
        if row["input_sha256"] != response["input_sha256"] or any(result.get(field) != value for field, value in {
            "evaluation_id": submission["evaluation_id"], "tender_id": submission["tender_id"],
            "snapshot_id": row["snapshot_id"], "snapshot_sha256": row["input_sha256"],
            "aggregation_policy": row["policy_version"],
        }.items()):
            raise AdapterError("result_identity_mismatch")
        self._validate_result_body(result, submission)
        return result

    def get_request(self, request_id: str) -> dict[str, Any]:
        with self.api.db() as conn:
            row = conn.execute("SELECT * FROM evaluation_requests WHERE request_id=? AND workspace=?", (request_id, self.api.workspace)).fetchone()
            if row is None:
                raise AdapterError("request_not_found")
            return {key: row[key] for key in (
                "request_id", "snapshot_id", "input_sha256", "policy_version", "status",
                "contract_address", "deployment_tx_hash", "evaluation_tx_hash", "result_reference",
                "created_at", "updated_at",
            )}
