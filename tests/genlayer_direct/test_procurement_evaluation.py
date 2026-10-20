"""Exercise the procurement contract in the pinned GenLayer Direct Mode runner.

Direct Mode requires nondeterministic LLM responses to be supplied through its
mock_llm cheatcode; contract deployment, storage, validation, state transitions,
and result construction remain real contract executions.
"""
import hashlib
import json
from pathlib import Path

import pytest


CONTRACT = Path(__file__).parent / "contracts" / "procurement_evaluation.py"
TENDER_ID = "10000000-0000-4000-8000-000000000001"
EVALUATION_ID = "20000000-0000-4000-8000-000000000001"
SNAPSHOT_ID = "30000000-0000-4000-8000-000000000001"
REQUIREMENT_ID = "40000000-0000-4000-8000-000000000001"
EVIDENCE_ID = "50000000-0000-4000-8000-000000000001"
DOCUMENT_ID = "60000000-0000-4000-8000-000000000001"
DEFAULT_EXCERPT = "Licence valid through 2027-12-31."
DOC_HASH = hashlib.sha256(DEFAULT_EXCERPT.encode("utf-8")).hexdigest()


def evidence_item(evidence_id, document_id, version, filename, excerpt):
    digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    byte_length = len(excerpt.encode("utf-8"))
    provenance = {
        "document_id": document_id,
        "document_sha256": digest,
        "document_version": version,
        "filename": filename,
        "start_byte": 0,
        "end_byte": byte_length,
        "retrieved_at": "2026-10-20T10:00:00+00:00",
        "integrity_check": "sha256_match",
        "source_authenticity": "not_independently_verified",
    }
    return {
        "id": evidence_id,
        "requirement_id": REQUIREMENT_ID,
        "document_id": document_id,
        "document_version": version,
        "document_sha256": digest,
        "filename": filename,
        "start_byte": 0,
        "end_byte": byte_length,
        "excerpt": excerpt,
        "provenance": provenance,
        "created_at": "2026-10-20T10:01:00+00:00",
    }


def submission(*, evidence=True, excerpt=DEFAULT_EXCERPT):
    requirement = {
        "id": REQUIREMENT_ID,
        "code": "ELIG-01",
        "text": "The bidder must hold a current operating licence.",
        "category": "eligibility",
        "mandatory": True,
        "kind": "judgment",
    }
    items = [evidence_item(EVIDENCE_ID, DOCUMENT_ID, 2, "licence-v2.txt", excerpt)] if evidence else []
    return {
        "schema_version": "tenderlens.evaluation-submission.v1",
        "evaluation_id": EVALUATION_ID,
        "snapshot_id": SNAPSHOT_ID,
        "snapshot_sha256": "a" * 64,
        "tender_id": TENDER_ID,
        "requirements": [requirement],
        "evidence": items,
    }


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deploy_evaluation(direct_deploy, direct_vm, response, payload=None):
    direct_vm.mock_llm(
        r"Classify each requirement using only the provided evidence data",
        encoded(response),
    )
    return direct_deploy(str(CONTRACT), encoded(payload or submission()))


def test_compliant_evidence_runs_contract_and_returns_structured_decision(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "compliant", "reason": "Evidence 50000000-0000-4000-8000-000000000001 states the licence remains valid through 2027."},
    )

    assert contract.get_lifecycle_status() == "prepared"
    result = json.loads(contract.evaluate())
    assert result["schema_version"] == "tenderlens.evaluation-result.v1"
    assert result["overall_status"] == "compliant"
    assert result["decisions"][0]["status"] == "compliant"
    assert result["decisions"][0]["reason"].startswith("Evidence 50000000")
    assert result["unresolved_requirements"] == []
    assert contract.get_lifecycle_status() == "evaluated"
    assert json.loads(contract.get_result()) == result


def test_explicitly_non_compliant_evidence_is_not_promoted_to_compliance(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "non_compliant", "reason": "Evidence shows the licence expired before the tender deadline."},
        payload=submission(excerpt="Licence expired on 2025-12-31."),
    )
    result = json.loads(contract.evaluate())
    assert result["overall_status"] == "non_compliant"
    assert result["decisions"][0]["status"] == "non_compliant"


def test_missing_evidence_is_deterministically_insufficient_without_llm(direct_vm, direct_deploy):
    contract = direct_deploy(str(CONTRACT), encoded(submission(evidence=False)))
    result = json.loads(contract.evaluate())
    assert result["overall_status"] == "inconclusive"
    assert result["decisions"][0]["status"] == "insufficient_evidence"
    assert "absence is not compliance" in result["decisions"][0]["reason"]
    assert result["decisions"][0]["evidence_refs"] == []
    assert result["unresolved_requirements"][0]["requirement_id"] == REQUIREMENT_ID


def test_contradictory_evidence_is_distinct_and_unresolved(direct_vm, direct_deploy):
    payload = submission()
    payload["evidence"].append(evidence_item(
        "50000000-0000-4000-8000-000000000002",
        "60000000-0000-4000-8000-000000000002",
        1,
        "revocation-notice.txt",
        "Operating licence revoked effective 2026-05-01.",
    ))
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "contradictory_evidence", "reason": "The submitted licence and the cited revocation notice conflict."},
        payload=payload,
    )
    result = json.loads(contract.evaluate())
    assert result["overall_status"] == "inconclusive"
    assert result["decisions"][0]["status"] == "contradictory_evidence"
    assert {ref["filename"] for ref in result["decisions"][0]["evidence_refs"]} == {"licence-v2.txt", "revocation-notice.txt"}
    assert result["unresolved_requirements"] == [{
        "requirement_id": REQUIREMENT_ID,
        "code": "ELIG-01",
        "status": "contradictory_evidence",
    }]


def test_invalid_json_and_malformed_requirement_are_rejected_at_deployment(direct_deploy):
    with pytest.raises(Exception, match="valid JSON"):
        direct_deploy(str(CONTRACT), "not-json")

    malformed = submission()
    malformed["requirements"] = []
    with pytest.raises(Exception, match="between 1 and 50"):
        direct_deploy(str(CONTRACT), encoded(malformed))


def test_result_preserves_evidence_version_and_byte_range_traceability(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "compliant", "reason": "Evidence supports the stated condition."},
    )
    result = json.loads(contract.evaluate())
    reference = result["decisions"][0]["evidence_refs"][0]
    assert reference == {
        "evidence_id": EVIDENCE_ID,
        "document_id": DOCUMENT_ID,
        "document_version": 2,
        "document_sha256": DOC_HASH,
        "filename": "licence-v2.txt",
        "start_byte": 0,
        "end_byte": 33,
    }
    assert result["snapshot_id"] == SNAPSHOT_ID
    assert result["snapshot_sha256"] == "a" * 64


def test_result_schema_is_stable_and_evaluation_cannot_be_rewritten(direct_vm, direct_deploy):
    response = {"status": "compliant", "reason": "The cited evidence supports the licence requirement."}
    first = deploy_evaluation(direct_deploy, direct_vm, response)
    first_output = first.evaluate()
    direct_vm.clear_mocks()
    second = deploy_evaluation(direct_deploy, direct_vm, response)
    second_output = second.evaluate()

    assert first_output == second_output
    parsed = json.loads(first_output)
    assert list(parsed.keys()) == sorted(parsed.keys())
    assert list(parsed["decisions"][0].keys()) == sorted(parsed["decisions"][0].keys())
    with direct_vm.expect_revert("evaluation has already been completed"):
        first.evaluate()
    assert first.get_result() == first_output
