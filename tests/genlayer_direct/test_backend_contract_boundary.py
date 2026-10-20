"""Prove that a real backend snapshot payload can initialize the Direct Mode contract."""
import hashlib
import io
import json
from pathlib import Path

from backend.app import TenderLensAPI

CONTRACT = Path(__file__).parent / "contracts" / "procurement_evaluation.py"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def call_api(app, method, path, body=b"", *, token="t" * 48, content_type="application/json", headers=None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": content_type,
        "HTTP_AUTHORIZATION": "Bearer " + token,
        "wsgi.input": io.BytesIO(body),
    }
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    response = {}
    chunks = app(environ, lambda status, response_headers: response.update(status=status, headers=response_headers))
    payload = b"".join(chunks)
    parsed = json.loads(payload) if payload else None
    return int(response["status"].split()[0]), parsed


def test_contract_evaluates_backend_generated_snapshot(tmp_path, direct_vm, direct_deploy):
    api = TenderLensAPI(tmp_path / "api.sqlite", tmp_path / "blobs", "t" * 48, "direct-test")
    status, tender = call_api(api, "POST", "/api/tenders", json.dumps({"title": "Tender", "reference": "T-1"}).encode())
    assert status == 201
    status, requirement = call_api(api, "POST", f"/api/tenders/{tender['id']}/requirements", json.dumps({
        "code": "LIC-1", "text": "Hold a current operating licence.", "category": "eligibility", "mandatory": True, "kind": "judgment"
    }).encode())
    assert status == 201

    document_bytes = b"Licence valid through 2027-12-31."
    status, document = call_api(
        api, "POST", f"/api/tenders/{tender['id']}/documents", document_bytes,
        content_type="text/plain", headers={"X-Filename": "licence-v1.txt"},
    )
    assert status == 201
    status, evidence = call_api(api, "POST", f"/api/tenders/{tender['id']}/evidence", json.dumps({
        "document_id": document["id"], "requirement_id": requirement["id"], "start_byte": 0, "end_byte": len(document_bytes)
    }).encode())
    assert status == 201
    status, snapshot = call_api(api, "POST", f"/api/tenders/{tender['id']}/evaluation-snapshots", json.dumps({
        "evidence_ids": [evidence["id"]]
    }).encode())
    assert status == 201
    assert snapshot["status"] == "prepared_not_submitted"
    status, retrieved = call_api(api, "GET", f"/api/tenders/{tender['id']}/evaluation-snapshots/{snapshot['snapshot_id']}")
    assert status == 200
    assert retrieved["submission"] == snapshot["submission"]
    assert retrieved["input_sha256"] == snapshot["input_sha256"]

    direct_vm.mock_llm(
        r"Classify this requirement using only the supplied evidence data",
        encoded({"status": "compliant", "reason": "The provided licence evidence supports the requirement.", "evidence_ids": [evidence["id"]]}),
    )
    contract = direct_deploy(str(CONTRACT), encoded(retrieved["submission"]))
    result = json.loads(contract.evaluate())
    reference = result["decisions"][0]["evidence_refs"][0]
    assert result["evaluation_id"] == retrieved["submission"]["evaluation_id"]
    assert result["tender_id"] == tender["id"]
    assert result["snapshot_id"] == snapshot["snapshot_id"]
    assert result["snapshot_sha256"] == snapshot["input_sha256"]
    assert reference["evidence_id"] == evidence["id"]
    assert reference["document_id"] == document["id"]
    assert reference["document_version"] == 1
    assert reference["document_sha256"] == hashlib.sha256(document_bytes).hexdigest()


def test_direct_contract_input_is_not_bound_to_a_backend_snapshot_without_adapter(tmp_path, direct_deploy):
    api = TenderLensAPI(tmp_path / "api.sqlite", tmp_path / "blobs", "t" * 48, "direct-test")
    status, tender = call_api(api, "POST", "/api/tenders", json.dumps({"title": "Tender", "reference": "T-2"}).encode())
    assert status == 201
    status, requirement = call_api(api, "POST", f"/api/tenders/{tender['id']}/requirements", json.dumps({
        "code": "R-1", "text": "The bidder must hold a current licence.", "category": "eligibility", "mandatory": True, "kind": "judgment"
    }).encode())
    assert status == 201
    status, snapshot = call_api(api, "POST", f"/api/tenders/{tender['id']}/evaluation-snapshots", b"{}")
    assert status == 201

    submitted = json.loads(json.dumps(snapshot["submission"]))
    submitted["requirements"][0]["text"] = "Substituted after the backend snapshot was stored."
    base = {key: value for key, value in submitted.items() if key != "snapshot_sha256"}
    submitted["snapshot_sha256"] = hashlib.sha256(encoded(base).encode("utf-8")).hexdigest()
    contract = direct_deploy(str(CONTRACT), encoded(submitted))
    result = json.loads(contract.evaluate())
    assert result["snapshot_id"] == snapshot["snapshot_id"]
    assert result["snapshot_sha256"] != snapshot["input_sha256"]
    assert result["decisions"][0]["reason"] == "No evidence was supplied for this requirement; absence is not compliance."
