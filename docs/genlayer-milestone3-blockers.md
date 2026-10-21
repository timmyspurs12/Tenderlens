# GenLayer milestone: procurement evaluation prototype

> Historical Milestone 3 state. The application adapter was added later; current binding guarantees and limits are documented in [`genlayer-milestone5-adapter.md`](genlayer-milestone5-adapter.md). The direct-contract bypass and unverified target runtime limitations described below remain current. For the current Milestone 7 offline qualification report and updated blocker matrix, see [`genlayer-milestone7-qualification.md`](genlayer-milestone7-qualification.md); this historical document's statements about running the old preflight are not current instructions.

## Verified runtime

The local Agent Mode host remains CPython 3.11.2 and does not have the GenLayer packages installed. Runtime verification is delegated to GitHub Actions on the writable feature branch. The workflow provisions Python 3.12, Node 22.14.0, and `uv==0.13.3`, installs the lockfiles, checks `genlayer-test==0.30.0rc2`, `genlayer-py==0.19.0rc2`, `pytest==9.1.1`, source commit `d84591b58746a6202b56b720199ce7b9f12fd666`, and `gltest --help`, then runs the Direct Mode suite.

**Current toolchain pin:** official Testing Suite v0.30.0-rc.2 source commit `d84591b58746a6202b56b720199ce7b9f12fd666` permits `genlayer-py>=0.19.0rc2,<0.20.0`. The repository pins that release source and SDK 0.19.0-rc.2, which add support for current GenVM manager runner bundles. This is a prerelease test environment; it does not prove target compatibility.

## Contract and boundary

`tests/genlayer_direct/contracts/procurement_evaluation.py` accepts a `tenderlens.evaluation-submission.v1` JSON snapshot. It validates the request deterministically, then calls `gl.nondet.exec_prompt` under `gl.eq_principle.strict_eq` for requirements with evidence. Requirements with no matching/global evidence are deterministically marked `insufficient_evidence` without an LLM call. The model must return a structured status, reason, and evidence-ID array; IDs must belong to the evidence supplied for that requirement, and result references are rebuilt from the submitted snapshot. The versioned `tenderlens.evaluation-result.v1` includes an aggregation policy, per-requirement status and reasoning, mandatory flag, source-backed document version/hash/byte-range references, an overall status, and unresolved requirements. The contract is one-shot: a completed evaluation cannot be overwritten.

The selectable policies are `mandatory_gate_optional_advisory_v1` (default: mandatory non-compliance is distinct from optional non-compliance; unresolved requirements yield inconclusive unless mandatory non-compliance exists) and `all_requirements_blocking_v1` (any non-compliance blocks). Insufficient and contradictory states are retained in unresolved requirements in either policy. They are evaluation summaries, not legal or award rules.

The authenticated backend freezes all current requirements and selected evidence into a `prepared_not_submitted` snapshot. It computes the documented canonical SHA-256, stores the payload/digest and an internal HMAC, enforces API-level append-only triggers, and verifies row HMAC, IDs, and recomputed digest before listing or returning snapshots. See `docs/api-contracts.md` and `docs/genlayer-milestone4-integrity.md` for the precise trust boundary, migration behavior, and distinction between payload integrity, backend row authentication, evidence provenance, and contract-result correspondence.

The contract still checks only digest shape. Direct construction with modified data and a shaped or newly computed digest remains possible because there is no backend adapter and the exact target GenVM runner has not been pinned/tested. HMAC is a backend-local check and is not passed to or verified by the contract. No transaction, wallet, contract address, transaction hash, network status polling, result callback, or finalization API exists.

## Tests and limitations

Run:

```sh
.venv/bin/python -m pytest tests/genlayer_direct -v
python -m unittest backend.test_api backend.test_genlayer_preflight -v
npm run typecheck
npm test
npm run build
npm run lint
```

The Direct Mode suite deploys the actual contract in the pinned in-memory runner and tests accepted-but-unverified stale and newly recomputed digests, rejection of malformed digest metadata, malformed model output, fabricated citations, cited versus uncited source-backed references, backend snapshot round-trip, the absence of a contract-to-database binding, stable schema, and both mandatory/optional policies. Backend tests exercise canonical digest verification and the internal HMAC boundary, including original-digest tampering, freshly rehashed tampering without the key, corrupted identity, missing tags, and legacy-schema migration. Direct Mode does not perform real nondeterministic model inference or validator consensus: its documented `direct_vm.mock_llm` cheatcode returns fixed model text while real contract Python code, storage, methods, and state transitions execute. This is not a live deployment or production-readiness claim.

Known constraints: only supplied `text/plain` byte-range evidence is supported; PDF/DOCX extraction, source authenticity, OCR, amendment semantic comparison, verified objective fact extraction, and adversarial prompt-injection resistance are not solved. A model-supported status is an evidence interpretation, not legal advice or an automatic award. Procurement documents may be confidential; contract arguments/calldata/state may be public on a live network, so the prototype is intentionally not deployed.
