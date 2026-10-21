# TenderLens API contract v1 (implemented subset)

The server is `backend/app.py`, a minimal Python WSGI + SQLite API. This contract is intentionally narrower than the complete frontend product. The default factory has no GenLayer submitter configured; the optional server-side submission route remains disabled unless both an injected submitter and server-controlled broadcast authorization are present. There is no document parser or browser authentication integration.

## Transport, authorization, tenancy

- JSON requests/responses use UTF-8. Document POST bodies are raw bytes, not multipart. JSON object keys must be unique at every nesting level; ambiguous duplicate-key bodies are rejected.
- All `/api/*` routes require `Authorization: Bearer <TENDERLENS_API_TOKEN>`. Missing or incorrect credentials return `401` with `{ "error": { "code": "unauthorized", "message": "Valid bearer authentication is required." } }`. The public `GET /healthz` is a minimal readiness check and exposes no records. The API refuses to start if the configured token is shorter than 32 characters. Compare uses constant-time `hmac.compare_digest`.
- `TENDERLENS_WORKSPACE` is the single trusted tenant identity assigned by server configuration. A token is bound to exactly one workspace. Cross-workspace object lookups return 404. Do not multiplex tenants behind one shared token.
- Do not place this token in a Vite/public variable or browser bundle. The current SPA has no login/session flow. A same-origin BFF/identity-aware gateway that authenticates users, enforces workspace membership, and injects the service credential is a prerequisite to exposing these routes to the browser.
- The WSGI app does not emit CORS headers: its `OPTIONS` response is `204` without `Access-Control-Allow-Origin`, and API responses do not allow browser cross-origin access. Prefer a same-origin BFF. If an identity-aware gateway must be cross-origin, configure that gateway (not this API) with an exact allowed frontend origin, credentialed CORS, and only required methods/headers; do not use `*` with credentials. Serve only behind TLS and a production-grade reverse proxy. The development WSGI server is not a production server. Configure restrictive ingress, rate limits, backups, malware scanning/quarantine, storage encryption, and retention in deployment.
- Upload body limit 20 MiB. JSON body limit 64 KiB. MIME types accepted: `text/plain`, `application/pdf`, DOCX. Declared MIME is not proof of file format. Originals are stored content-addressed by SHA-256; document rows are append-only version records. No automatic OCR/parser runs.

## Common error schema

All non-204 failures return `{ "error": { "code": string, "message": string } }` and an `X-Request-ID`. Responses have `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

- `400 validation_error`, `invalid_document`, `invalid_predecessor`, `invalid_requirement`, `invalid_evidence`, `unsupported_evidence_source`
- `401 unauthorized` (missing/invalid bearer token)
- `404 not_found` (including another workspace's records)
- `409 conflict` (duplicate tender reference, idempotency-key conflict, or snapshot already requested)
- `413 file_too_large`
- `415 unsupported_media_type`
- `500 internal_error` / `integrity_failure` / `result_verification_failed` for a stored result that no longer verifies
- `502 result_verification_failed` when a finalized upstream result is rejected by the adapter
- `202 Accepted` indicates no verified result has been stored yet; a completed, verified result returns `200`. `409` also covers an outcome requiring reconciliation; `503 submission_disabled` / `submission_unavailable` indicate server-side submission prerequisites are not enabled or verified.

Database constraint failures map to a generic 409. Server exception details are not returned to clients.

## Schemas

All IDs are UUID strings. Timestamps are UTC ISO-8601 strings.

```ts
type Tender = { id: string; title: string; reference: string; deadline: string | null; created_at: string };
type DocumentVersion = { id: string; group_id: string; version: number; filename: string; content_type: string; size: number; sha256: string; supersedes_document_id: string | null; created_at: string };
type Requirement = { id: string; code: string; text: string; category: 'eligibility'|'mandatory_documentation'|'technical'|'interpretive'; mandatory: boolean; kind: 'objective'|'judgment'; created_at: string };
type Amendment = { id: string; document_id: string; predecessor_id: string|null; relationship: 'explicit_replacement'|'conflicting'|'clarification'|'unresolved'|'none'; comparison_status: 'unavailable_without_verified_comparison_engine'; created_at: string };
type Evidence = { id:string; requirement_id:string|null; document_id:string; document_sha256:string; start_byte:number; end_byte:number; excerpt:string; provenance:{document_id:string; document_sha256:string; document_version:number; filename:string; start_byte:number; end_byte:number; retrieved_at:string; integrity_check:'sha256_match'; source_authenticity:'not_independently_verified'}; created_at:string };
```

### Implemented routes

- `GET /api/tenders` → `{ items: Tender[] }`
- `POST /api/tenders` → `201 Tender`; body `{ title: string (1–200), reference: string (1–120), deadline?: string (1–80) }`. Duplicate reference within a workspace is `409`.
- `GET /api/tenders/{tenderId}` → `Tender`
- `GET /api/tenders/{tenderId}/documents` → `{ items: DocumentVersion[] }`
- `POST /api/tenders/{tenderId}/documents` → `201 DocumentVersion`; raw file bytes; headers `Content-Type`, `X-Filename`, optional `X-Document-Group`, optional `X-Supersedes-Document-Id`. Version number is assigned by the server. Each upload creates a new immutable row; identical bytes may share the content-addressed blob but never overwrite a prior version row. A predecessor must belong to the same tender and group.
- `GET /api/tenders/{tenderId}/amendments` → `{ items: Amendment[]; comparison_status: 'unavailable_without_verified_comparison_engine' }`
- `POST /api/tenders/{tenderId}/amendments` → `201 Amendment`; body `{ document_id, predecessor_id?: string|null, relationship }`. Relationship/predecessor are explicit user-submitted annotations, not automatically inferred or independently validated. The API does not claim passage-level differences, supersession, conflicts, or governing language.
- `GET /api/tenders/{tenderId}/requirements` → `{ items: Requirement[] }`
- `POST /api/tenders/{tenderId}/requirements` → `201 Requirement`; body `{ code, text, category, mandatory, kind }`. It registers a stated criterion only; it does not evaluate or endorse it.
- `GET /api/tenders/{tenderId}/evidence` → `{ items: Evidence[] }`
- `POST /api/tenders/{tenderId}/evidence` → `201 Evidence`; body `{ document_id, requirement_id?: string|null, start_byte, end_byte }`. For `text/plain` only, the excerpt is derived from exact UTF-8 bytes at the supplied half-open byte interval and the stored original's SHA-256 is rechecked. PDF/DOCX passage extraction intentionally returns `unsupported_evidence_source`; no page/section/citation is fabricated. A matching hash proves stored-byte integrity, not source authenticity or truth.
- `POST /api/tenders/{tenderId}/evaluation-snapshots` → `201 EvaluationSnapshot`; body `{ evidence_ids?: string[], aggregation_policy?: 'mandatory_gate_optional_advisory_v1'|'all_requirements_blocking_v1' }`. The default is `mandatory_gate_optional_advisory_v1`. The API includes every currently registered requirement and only the listed evidence records; an empty list deliberately represents missing evidence. Evidence must belong to this tender, point at an immutable document version, and still match its stored content hash and UTF-8 byte range. The server creates a snapshot UUID and SHA-256 over canonical JSON and stores the exact payload, the digest, and an internal HMAC authentication tag. The canonical UTF-8 constructor JSON is limited to 250,000 bytes (not characters); both the backend and contract reject oversized payloads before evaluation. The contract also rejects a serialized result over 250,000 UTF-8 bytes before writing result state, matching the adapter's input limit for result ingestion. Response shape is unchanged: `{ snapshot_id, input_sha256, created_at, status: 'prepared_not_submitted', submission: EvaluationSubmission }`.
- `GET /api/tenders/{tenderId}/evaluation-snapshots` → `{ items: [{ id, input_sha256, created_at, status: 'prepared_not_submitted' }] }`. It verifies every row before returning the list and fails closed if any row fails verification.
- `GET /api/tenders/{tenderId}/evaluation-snapshots/{snapshotId}` → the same snapshot metadata plus the `submission` payload. It requires the requested ID to match the stored primary key and embedded payload ID, recomputes the payload digest, compares both embedded and stored digests, and verifies the internal HMAC. Later uploads do not modify this snapshot. The trusted retrieval path is used internally by `backend.evaluation_adapter.EvaluationAdapter`.
- `POST /api/tenders/{tenderId}/evaluation-snapshots/{snapshotId}/submit` → submits only the stored, adapter-verified snapshot, body `{ idempotency_key: string }`. The endpoint refuses to run unless the server was constructed with an explicit submitter and server-side `TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED=1`; the request schema rejects extra fields and cannot enable or authorize broadcasting. With the default factory (no injected submitter) it returns `503 submission_disabled`. A configured submitter must still pass target identity, runtime qualification, explicit server broadcast authorization and signer configuration, and funding gates. A request without a verified stored result returns `202 Accepted`; a completed result returns `200`. Idempotency/snapshot conflicts return `409`; a finalized but invalid result returns `502 result_verification_failed` and is durably marked failed. Ambiguous outcomes remain queryable and are never automatically resubmitted.
- `GET /api/evaluation-requests/{requestId}` → the workspace-scoped durable lifecycle state; completed results are returned only after snapshot/result association validation. The route does not establish that an SDK response is independent proof of validator consensus.

The internal `EvaluationAdapter.prepare(snapshot_id, idempotency_key, expected_policy=None)` accepts a snapshot ID, not a caller-supplied contract payload. It verifies stored data, pins policy/schema metadata, and returns the exact persisted `payload_json` as `contract_input_json`. `submit(..., submitter=...)` provides a retry-safe application port for an explicitly configured GenLayer client: it claims a single submission attempt and passes only verified JSON. The API now exposes an optional submission/status path, but no submitter or target-specific verifier is wired by the default app factory, so submission is disabled unless a server assembler explicitly injects the submitter and the server broadcast flag is enabled. Adapter request state and transaction/result references are workspace-scoped at the API boundary. See [`genlayer-milestone5-adapter.md`](genlayer-milestone5-adapter.md) and [`genlayer-milestone6-sdk-driver.md`](genlayer-milestone6-sdk-driver.md).

`EvaluationSubmission` schema version is `tenderlens.evaluation-submission.v1` and contains `evaluation_id`, `snapshot_id`, `snapshot_sha256`, `tender_id`, `aggregation_policy`, all requirement descriptors, and selected evidence descriptors including evidence/document UUIDs, document version, document SHA-256, filename, byte offsets, excerpt, original provenance, and creation timestamp.

The backend's canonical digest procedure is exact: take the complete submission object before adding `snapshot_sha256` (currently the keys are `schema_version`, `evaluation_id`, `snapshot_id`, `tender_id`, `aggregation_policy`, `requirements`, and `evidence`); serialize it with Python `json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; encode the resulting string as UTF-8 with no added newline or whitespace; compute SHA-256; render lowercase hexadecimal. There is no Unicode normalization. Object key order is canonicalized recursively; array order remains significant. The digest is attached as `snapshot_sha256`, and the same digest is returned as `input_sha256` and persisted alongside the exact payload. Tests cover Unicode, nested objects, key-order invariance, and array-order significance.

### Integrity, authenticity, immutability, and provenance are distinct

- **Payload-to-digest integrity:** the backend recomputes the canonical digest on every snapshot retrieval and compares it with both `submission.snapshot_sha256` and the database `input_sha256`. The contract itself still checks digest shape only.
- **Backend row authenticity:** each stored row has an internal HMAC over its database identity, workspace/tender identity, creation timestamp, digest, and exact serialized payload. `TENDERLENS_SNAPSHOT_HMAC_KEY` may provide a dedicated stable secret of at least 32 characters; if unset, the API token is used as the HMAC key. The tag is not returned to API clients or passed to the contract. Keep a dedicated key stable across restarts; key rotation makes existing tags fail closed unless an operator deliberately re-seals them. Never log either key.
- **Immutability:** SQL triggers prevent the API from updating or deleting snapshot rows. HMAC detects row edits by a database-only actor who does not possess the server key. It cannot protect against compromise of the application process, secret, or a database-plus-key operator. Legacy rows are migrated only after checking their ID and self-consistent digest; migration authenticates the database contents at upgrade time, not their historical authorship.
- **Authentication:** an unkeyed matching SHA-256 does not prove who created a payload. The API authenticates callers with its bearer token and should be protected by TLS. The internal HMAC authenticates rows only to this backend; it is not a portable signature and is not verified by GenVM.
- **Evidence provenance:** backend checks show that the selected excerpt bytes match the stored document bytes/hash and byte range. They do not prove the source document's publisher, authenticity, truth, or legal significance.
- **Contract/result correspondence:** the contract echoes evaluation ID, snapshot ID, digest, and policy from its own constructor input. The application adapter binds its normal path to the authenticated stored row and associates results with a single request/snapshot plus recorded contract and transaction references. The adapter's result ingestion assumes a trusted runtime-specific caller actually fetched those results from GenLayer; it does not itself verify an RPC receipt. The contract cannot check that constructor values match a TenderLens row, and direct construction can still accept changed payloads with stale-shaped or freshly recomputed digests. Application binding is not on-chain enforcement.

```ts
type EvaluationSubmission = {
  schema_version: 'tenderlens.evaluation-submission.v1';
  evaluation_id: string; snapshot_id: string; snapshot_sha256: string; tender_id: string;
  aggregation_policy: 'mandatory_gate_optional_advisory_v1'|'all_requirements_blocking_v1';
  requirements: Array<{ id: string; code: string; text: string; category: Requirement['category']; mandatory: boolean; kind: Requirement['kind'] }>;
  evidence: Array<{ id: string; requirement_id: string|null; document_id: string; document_version: number; document_sha256: string; filename: string; start_byte: number; end_byte: number; excerpt: string; provenance: Evidence['provenance']; created_at: string }>;
};
type EvaluationResult = {
  schema_version: 'tenderlens.evaluation-result.v1'; evaluation_id: string; tender_id: string;
  snapshot_id: string; snapshot_sha256: string;
  aggregation_policy: 'mandatory_gate_optional_advisory_v1'|'all_requirements_blocking_v1';
  overall_status: 'compliant'|'mandatory_non_compliance'|'optional_non_compliance'|'non_compliant'|'inconclusive';
  decisions: Array<{ requirement_id: string; code: string; mandatory: boolean;
    status: 'compliant'|'non_compliant'|'insufficient_evidence'|'contradictory_evidence';
    reason: string; evidence_refs: Array<{ evidence_id: string; document_id: string; document_version: number; document_sha256: string; filename: string; start_byte: number; end_byte: number }> }>;
  unresolved_requirements: Array<{ requirement_id: string; code: string; status: 'insufficient_evidence'|'contradictory_evidence' }>;
  disclaimer: string;
};
```

### Configurable evaluation aggregation policies

- `mandatory_gate_optional_advisory_v1` (default): any mandatory `non_compliant` finding yields `mandatory_non_compliance`; otherwise any `insufficient_evidence` or `contradictory_evidence` yields `inconclusive`; otherwise an optional `non_compliant` finding yields `optional_non_compliance`; otherwise the result is `compliant`. Optional non-compliance is reported, but is not silently promoted to mandatory eligibility failure.
- `all_requirements_blocking_v1`: any `non_compliant` finding (mandatory or optional) yields `non_compliant`; absent that, any insufficient or contradictory finding yields `inconclusive`; otherwise `compliant`.

In both policies, insufficient and contradictory findings remain unresolved and appear in `unresolved_requirements`, even when a mandatory non-compliance determines the overall status. This is a configurable evaluation summary, not a procurement-law rule, eligibility determination, or award decision. The application does not prescribe which policy a procuring entity should choose. Missing evidence is deterministically `insufficient_evidence` and never reaches the LLM path. Evidence with no `requirement_id` is treated as supplied context for every requirement; requirement-scoped evidence is considered only for that requirement. Model responses must contain exactly `status`, `reason`, and structured `evidence_ids`; citations must identify evidence supplied for that requirement. Result references are constructed from those submitted evidence records, never from model-provided reference metadata.

## Implemented Direct Mode contract; no network lifecycle

`tests/genlayer_direct/contracts/procurement_evaluation.py` is a GenLayer Intelligent Contract. Its constructor deterministically validates the submission schema and version/hash/reference shapes. `evaluate()` classifies each requirement with an Intelligent Contract prompt wrapped in `gl.eq_principle.strict_eq`; when no evidence is linked or supplied, the contract deterministically emits `insufficient_evidence` without calling the model. Output schema `tenderlens.evaluation-result.v1` contains one decision per requirement, reason, evidence references with exact document version/hash/byte range, an overall status, and unresolved requirements. A successful evaluation is one-shot; it cannot silently be rewritten.

`bash .devcontainer/setup.sh --genlayer-only` installs the locked Python/GenLayer environment. Run `.venv/bin/python -m pytest tests/genlayer_direct -v` for Direct Mode contract tests and `python -m unittest backend.test_api backend.test_evaluation_adapter backend.test_genlayer_preflight -v` for backend, adapter, and preflight tests. `python scripts/genlayer_preflight.py` reports deployment blockers without submitting a transaction. The pinned Direct Mode runner requires LLM mocks for nondeterministic calls. These tests execute real contract code and state transitions with fixed mocked model responses; they do not test actual LLM quality, live validator consensus, or Studio/network behavior.

`EvaluationAdapter` has an internal application submit/result port and durable lifecycle ledger. The API has an optional, server-gated submission/status route, but the default factory injects no GenLayer SDK driver and no live network is configured. Transaction references are only persisted when supplied by the explicitly injected runtime driver; result association checks metadata and those references, but does not independently prove that a receipt was fetched from chain. See the Milestone 5 trust-boundary discussion. No legal advice or automatic procurement award is provided. Tender evidence may be confidential; contract calldata/state may be public on a live network, so do not deploy sensitive material. The contract is not deployed; Direct Mode regressions exist but have not been run in the current local environment.
