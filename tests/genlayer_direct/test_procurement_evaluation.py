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
OPTIONAL_REQUIREMENT_ID = "40000000-0000-4000-8000-000000000002"
OPTIONAL_EVIDENCE_ID = "50000000-0000-4000-8000-000000000003"
OPTIONAL_DOCUMENT_ID = "60000000-0000-4000-8000-000000000003"
DEFAULT_EXCERPT = "Licence valid through 2027-12-31."
DOC_HASH = hashlib.sha256(DEFAULT_EXCERPT.encode("utf-8")).hexdigest()


def evidence_item(evidence_id, document_id, version, filename, excerpt, requirement_id=REQUIREMENT_ID):
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
        "requirement_id": requirement_id,
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


def submission(*, evidence=True, excerpt=DEFAULT_EXCERPT, aggregation_policy="mandatory_gate_optional_advisory_v1"):
    requirement = {
        "id": REQUIREMENT_ID,
        "code": "ELIG-01",
        "text": "The bidder must hold a current operating licence.",
        "category": "eligibility",
        "mandatory": True,
        "kind": "judgment",
    }
    items = [evidence_item(EVIDENCE_ID, DOCUMENT_ID, 2, "licence-v2.txt", excerpt)] if evidence else []
    payload = {
        "schema_version": "tenderlens.evaluation-submission.v1",
        "evaluation_id": EVALUATION_ID,
        "snapshot_id": SNAPSHOT_ID,
        "tender_id": TENDER_ID,
        "aggregation_policy": aggregation_policy,
        "requirements": [requirement],
        "evidence": items,
    }
    payload["snapshot_sha256"] = hashlib.sha256(encoded(payload).encode("utf-8")).hexdigest()
    return payload


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)



def refreshed_submission(payload):
    body = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
    payload["snapshot_sha256"] = hashlib.sha256(encoded(body).encode("utf-8")).hexdigest()
    return payload


def policy_submission(policy, missing=()):
    payload = submission(evidence=False, aggregation_policy=policy)
    payload["requirements"] = [
        {
            "id": REQUIREMENT_ID,
            "code": "MAND-1",
            "text": "The bidder must satisfy the mandatory licence criterion.",
            "category": "eligibility",
            "mandatory": True,
            "kind": "judgment",
        },
        {
            "id": OPTIONAL_REQUIREMENT_ID,
            "code": "OPT-1",
            "text": "The bidder should provide optional sustainability details.",
            "category": "technical",
            "mandatory": False,
            "kind": "judgment",
        },
    ]
    if "MAND-1" not in missing:
        payload["evidence"].append(evidence_item(
            EVIDENCE_ID, DOCUMENT_ID, 1, "mandatory.txt", "Mandatory criterion evidence.", REQUIREMENT_ID
        ))
    if "OPT-1" not in missing:
        payload["evidence"].append(evidence_item(
            OPTIONAL_EVIDENCE_ID, OPTIONAL_DOCUMENT_ID, 1, "optional.txt", "Optional criterion evidence.", OPTIONAL_REQUIREMENT_ID
        ))
    return refreshed_submission(payload)


def deploy_policy_evaluation(direct_vm, direct_deploy, policy, mandatory_status, optional_status, missing=()):
    payload = policy_submission(policy, missing)
    if "MAND-1" not in missing:
        direct_vm.mock_llm(
            r'"code":"MAND-1"',
            encoded({"status": mandatory_status, "reason": "Mandatory evidence supports this interpretation.", "evidence_ids": [EVIDENCE_ID]}),
        )
    if "OPT-1" not in missing:
        direct_vm.mock_llm(
            r'"code":"OPT-1"',
            encoded({"status": optional_status, "reason": "Optional evidence supports this interpretation.", "evidence_ids": [OPTIONAL_EVIDENCE_ID]}),
        )
    return direct_deploy(str(CONTRACT), encoded(payload))

def deploy_evaluation(direct_deploy, direct_vm, response, payload=None):
    direct_vm.mock_llm(
        r"Classify this requirement using only the supplied evidence data",
        encoded(response),
    )
    return direct_deploy(str(CONTRACT), encoded(payload or submission()))


def test_compliant_evidence_runs_contract_and_returns_structured_decision(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "compliant", "reason": "The licence excerpt states validity through 2027.", "evidence_ids": [EVIDENCE_ID]},
    )

    assert contract.get_lifecycle_status() == "prepared"
    result = json.loads(contract.evaluate())
    assert result["schema_version"] == "tenderlens.evaluation-result.v1"
    assert result["overall_status"] == "compliant"
    assert result["decisions"][0]["status"] == "compliant"
    assert result["decisions"][0]["reason"].startswith("The licence excerpt")
    assert result["unresolved_requirements"] == []
    assert contract.get_lifecycle_status() == "evaluated"
    assert json.loads(contract.get_result()) == result


def test_explicitly_non_compliant_evidence_is_not_promoted_to_compliance(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "non_compliant", "reason": "The licence expired before the tender deadline.", "evidence_ids": [EVIDENCE_ID]},
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
        {"status": "contradictory_evidence", "reason": "The licence and revocation notice conflict.", "evidence_ids": [EVIDENCE_ID, "50000000-0000-4000-8000-000000000002"]},
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


def test_invalid_json_is_rejected_at_deployment(direct_deploy):
    with pytest.raises(Exception, match="valid JSON"):
        direct_deploy(str(CONTRACT), "not-json")


def test_malformed_requirement_set_is_rejected_at_deployment(direct_deploy):
    malformed = submission()
    malformed["requirements"] = []
    with pytest.raises(Exception, match="between 1 and 50"):
        direct_deploy(str(CONTRACT), encoded(malformed))


def test_contract_accepts_digest_shaped_value_without_claiming_payload_integrity(direct_deploy):
    changed_payload = submission()
    changed_payload["requirements"][0]["text"] += " changed after snapshot creation"
    contract = direct_deploy(str(CONTRACT), encoded(changed_payload))
    stored_submission = json.loads(contract.get_submission())
    assert stored_submission["requirements"][0]["text"].endswith("changed after snapshot creation")
    assert stored_submission["snapshot_sha256"] == changed_payload["snapshot_sha256"]
    assert len(stored_submission["snapshot_sha256"]) == 64


def test_malformed_intelligent_response_is_rejected_without_completing_evaluation(direct_vm, direct_deploy):
    direct_vm.mock_llm(r"Classify this requirement using only the supplied evidence data", "not-json")
    contract = direct_deploy(str(CONTRACT), encoded(submission()))
    with direct_vm.expect_revert("did not return valid JSON"):
        contract.evaluate()
    assert contract.get_lifecycle_status() == "prepared"
    assert contract.get_result() == ""


@pytest.mark.parametrize(
    "response,error",
    [
        ({"status": "compliant", "reason": "The evidence appears sufficient."}, "invalid decision schema"),
        ({"status": "award", "reason": "The evidence appears sufficient.", "evidence_ids": [EVIDENCE_ID]}, "unsupported requirement status"),
    ],
)
def test_unsupported_or_incomplete_model_response_is_rejected(direct_vm, direct_deploy, response, error):
    contract = deploy_evaluation(direct_deploy, direct_vm, response)
    with direct_vm.expect_revert(error):
        contract.evaluate()
    assert contract.get_lifecycle_status() == "prepared"


def test_fabricated_structured_evidence_id_is_rejected(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "compliant", "reason": "The supplied material appears sufficient.", "evidence_ids": ["90000000-0000-4000-8000-000000000001"]},
    )
    with direct_vm.expect_revert("cited evidence not present in the submission"):
        contract.evaluate()
    assert contract.get_lifecycle_status() == "prepared"


def test_fabricated_evidence_id_in_reason_is_rejected(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "compliant", "reason": "Evidence 90000000-0000-4000-8000-000000000001 proves the requirement.", "evidence_ids": [EVIDENCE_ID]},
    )
    with direct_vm.expect_revert("reason contains an unsupported evidence ID"):
        contract.evaluate()
    assert contract.get_lifecycle_status() == "prepared"


def test_result_references_only_structurally_cited_submission_evidence(direct_vm, direct_deploy):
    payload = submission()
    payload["evidence"].append(evidence_item(
        "50000000-0000-4000-8000-000000000004",
        "60000000-0000-4000-8000-000000000004",
        1,
        "uncited-context.txt",
        "Additional supplied context.",
    ))
    refreshed_submission(payload)
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "compliant", "reason": "The cited licence excerpt supports the criterion.", "evidence_ids": [EVIDENCE_ID]},
        payload=payload,
    )
    result = json.loads(contract.evaluate())
    assert [reference["evidence_id"] for reference in result["decisions"][0]["evidence_refs"]] == [EVIDENCE_ID]
    assert result["decisions"][0]["evidence_refs"][0]["filename"] == "licence-v2.txt"


def test_result_preserves_evidence_version_and_byte_range_traceability(direct_vm, direct_deploy):
    contract = deploy_evaluation(
        direct_deploy, direct_vm,
        {"status": "compliant", "reason": "The supplied record supports the condition.", "evidence_ids": [EVIDENCE_ID]},
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
    assert result["snapshot_sha256"] == submission()["snapshot_sha256"]


def test_result_schema_is_stable_and_evaluation_cannot_be_rewritten(direct_vm, direct_deploy):
    response = {"status": "compliant", "reason": "The supplied record supports the licence requirement.", "evidence_ids": [EVIDENCE_ID]}
    contract = deploy_evaluation(direct_deploy, direct_vm, response)
    result = contract.evaluate()

    parsed = json.loads(result)
    assert result == encoded(parsed)
    assert list(parsed.keys()) == sorted(parsed.keys())
    assert list(parsed["decisions"][0].keys()) == sorted(parsed["decisions"][0].keys())
    with direct_vm.expect_revert("evaluation has already been completed"):
        contract.evaluate()
    assert contract.get_result() == result


@pytest.mark.parametrize(
    "policy,mandatory_status,optional_status,missing,expected,unresolved",
    [
        ("mandatory_gate_optional_advisory_v1", "compliant", "compliant", (), "compliant", ()),
        ("mandatory_gate_optional_advisory_v1", "compliant", "non_compliant", (), "optional_non_compliance", ()),
        ("mandatory_gate_optional_advisory_v1", "non_compliant", "compliant", (), "mandatory_non_compliance", ()),
        ("mandatory_gate_optional_advisory_v1", "insufficient_evidence", "compliant", ("MAND-1",), "inconclusive", ("MAND-1",)),
        ("mandatory_gate_optional_advisory_v1", "compliant", "insufficient_evidence", ("OPT-1",), "inconclusive", ("OPT-1",)),
        ("mandatory_gate_optional_advisory_v1", "contradictory_evidence", "compliant", (), "inconclusive", ("MAND-1",)),
        ("mandatory_gate_optional_advisory_v1", "compliant", "contradictory_evidence", (), "inconclusive", ("OPT-1",)),
        ("mandatory_gate_optional_advisory_v1", "non_compliant", "insufficient_evidence", ("OPT-1",), "mandatory_non_compliance", ("OPT-1",)),
        ("all_requirements_blocking_v1", "compliant", "non_compliant", (), "non_compliant", ()),
    ],
)
def test_configurable_aggregation_policy_preserves_mandatory_optional_and_unresolved_states(
    direct_vm, direct_deploy, policy, mandatory_status, optional_status, missing, expected, unresolved
):
    contract = deploy_policy_evaluation(
        direct_vm, direct_deploy, policy, mandatory_status, optional_status, missing
    )
    result = json.loads(contract.evaluate())
    assert result["aggregation_policy"] == policy
    assert result["overall_status"] == expected
    assert [decision["status"] for decision in result["decisions"]] == [
        "insufficient_evidence" if "MAND-1" in missing else mandatory_status,
        "insufficient_evidence" if "OPT-1" in missing else optional_status,
    ]
    assert [item["code"] for item in result["unresolved_requirements"]] == list(unresolved)
