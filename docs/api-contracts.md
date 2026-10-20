# TenderLens API contract v1 (implemented subset)

The server is `backend/app.py`, a minimal Python WSGI + SQLite API. This contract is intentionally narrower than the complete frontend product. There is no GenLayer transaction, assessment API, document parser, or browser authentication integration in this milestone.

## Transport, authorization, tenancy

- JSON requests/responses use UTF-8. Document POST bodies are raw bytes, not multipart.
- Every route, including reads and health-like reads, requires `Authorization: Bearer <TENDERLENS_API_TOKEN>`. The API refuses to start if the configured token is shorter than 32 characters. Compare uses constant-time `hmac.compare_digest`.
- `TENDERLENS_WORKSPACE` is the single trusted tenant identity assigned by server configuration. A token is bound to exactly one workspace. Cross-workspace object lookups return 404. Do not multiplex tenants behind one shared token.
- Do not place this token in a Vite/public variable or browser bundle. The current SPA has no login/session flow. A same-origin BFF/identity-aware gateway that authenticates users, enforces workspace membership, and injects the service credential is a prerequisite to exposing these routes to the browser.
- Serve only behind TLS and a production-grade reverse proxy. The development WSGI server is not a production server. Configure restrictive ingress, rate limits, backups, malware scanning/quarantine, storage encryption, and retention in deployment.
- Upload body limit 20 MiB. JSON body limit 64 KiB. MIME types accepted: `text/plain`, `application/pdf`, DOCX. Declared MIME is not proof of file format. Originals are stored content-addressed by SHA-256; document rows are append-only version records. No automatic OCR/parser runs.

## Common error schema

All non-204 failures return `{ "error": { "code": string, "message": string } }` and an `X-Request-ID`. Responses have `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

- `400 validation_error`, `invalid_document`, `invalid_predecessor`, `invalid_requirement`, `unsupported_evidence_source`
- `401 unauthorized` (missing/invalid bearer token)
- `404 not_found` (including another workspace's records)
- `409 conflict` (duplicate tender reference / uniqueness violation)
- `413 file_too_large`
- `415 unsupported_media_type`
- `500 internal_error` / `integrity_failure`

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

## Not implemented: assessment, GenLayer, lifecycle

No evaluation submission route exists. In particular the server accepts no frontend verdict, consensus result, contract address, transaction hash, execution outcome, or finalization assertion. Lifecycle states reserved for a future verified integration are `draft`, `documents_verified`, `ready_for_evaluation`, `submitted`, `consensus_pending`, `finalized`, `inconclusive`, `failed`, `leader_timeout`, `validator_timeout`, and `unresolved`. API processing, transaction submission, consensus completion, protocol finalization, and contract execution must be represented as separate verified facts. No local transaction receipt alone constitutes a finalized decision.

Before adding an evaluation route, pin a rubric version and exact evidence/document hashes, validate objective checks deterministically, submit an idempotent request keyed by a caller-supplied idempotency key, persist transaction and consensus lifecycle independently, and expose only network-verified state. GenLayer SDK/compiler, network configuration, wallet/credentials, contract tests, and a supported finalization/status query are not present in this repository or environment configuration, so deployment is deliberately blocked.
