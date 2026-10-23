# TenderLens v1 milestone: source-review workflow

**Contribution date:** 2026-10-23

**Scope:** Turn the existing tender detail placeholders into an owner-gated workflow that creates and inspects real source records.

## Documented delta

Before this change, the tender detail routes described documents, requirements, amendments, evidence, and evaluation but rendered placeholder-only states. The backend already stored those records; the browser had no end-to-end workbench for creating or reviewing them.

This update connects the browser to those APIs and adds:

1. **Owner session in the workbench.** The UI reads the BFF session state, exposes its optional passphrase login/logout, and disables privileged forms unless the BFF confirms an owner session. The browser never handles the backend bearer token. A deployment without owner sign-in remains read-only for privileged operations.
2. **Versioned source register.** Owners can upload TXT, PDF, and DOCX source files as append-only versions, select a document group and predecessor, and inspect the server-calculated SHA-256. Existing versions are not overwritten. Unicode filename/group metadata is encoded in headers and decoded/validated by the backend.
3. **Verified text evidence capture.** For TXT only, the owner reselects a local copy of the exact stored version. The client verifies its SHA-256 before displaying up to 1 MiB of text. A selected passage is translated to original UTF-8 byte offsets (including CRLF and Unicode boundaries); the browser submits the range, not a model-generated excerpt. The backend checks the stored hash and derives the returned excerpt from the content-addressed bytes. Requirements can be attached, or the passage can be saved as general context.
4. **Append-only requirements.** The workbench registers the criterion wording, category, mandatory flag, and objective/judgment kind. New SQLite triggers reject direct update/delete attempts so criteria are not silently rewritten after capture.
5. **Explicit amendment register.** Reviewers may link an amendment version to a predecessor and record a relationship. Those values are visibly labeled as human annotations; no changed clauses, affected criteria, or legal effect are inferred.
6. **Immutable snapshot preparation.** Owners choose which saved evidence to include and select an aggregation policy. The backend freezes all current requirements and only the selected evidence into a canonical, hashed snapshot. The page explicitly states that preparation does not run a model, deploy a contract, or broadcast a GenLayer transaction.

## Trust boundary and limitations

- Tender, source metadata, requirement, snapshot, and evidence reads follow the existing public-workbench policy. Saved evidence excerpts are therefore public too: upload only published material, never confidential bids or private attachments. The optional owner cookie gates writes; it is not multi-user identity, account recovery, or per-tender membership.
- The browser does not download stored document bytes. The operator must provide the local TXT copy and the client must match its SHA-256 to the registered version before passage review.
- PDF/DOCX upload is archival only. There is no OCR, parser, or passage extraction. Semantic amendment comparison is not implemented.
- SHA-256 confirms byte identity with the stored upload, not publisher authenticity, factual truth, or legal significance.
- The backend accepts up to 20 MiB, but the hosted function platform may have a lower body-size ceiling. Durable encrypted storage, production identity, and verified real-tender usage remain operator/deployment work.
- Snapshot preparation is not evaluation, an on-chain transaction, legal advice, or an award decision.

## Verification

From the repository root:

```sh
npm ci
npm test
npm run typecheck
npm run lint
npm run build
python -m unittest backend.test_api backend.test_evaluation_adapter backend.test_genlayer_preflight backend.test_genlayer_sdk_driver
```

The tests use synthetic records/files and mocked network responses; they verify code paths and trust boundaries, not adoption, production configuration, or live procurement outcomes.
