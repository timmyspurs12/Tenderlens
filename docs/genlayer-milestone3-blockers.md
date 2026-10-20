# GenLayer milestone: procurement evaluation prototype

## Verified runtime

The local Agent Mode host remains CPython 3.11.2 and does not have the GenLayer packages installed. Runtime verification is delegated to GitHub Actions on the writable feature branch. The workflow provisions Python 3.12, Node 22.14.0, and `uv==0.13.3`, installs the lockfiles, checks `genlayer-test==0.29.2`, `genlayer-py==0.18.0`, `pytest==9.1.1`, source commit `343e3a358f9e235a93b49c60721ce7676585ff07`, and `gltest --help`, then runs the Direct Mode suite.

**Compatibility pin:** the pinned upstream source commit declares suite version 0.29.2 and permits `genlayer-py>=0.18.0,<0.20.0`. The PyPI wheel `genlayer-test==0.29.2` instead requires `genlayer-py>=0.13.0,<0.17.0`; do not replace the source pin with that incompatible wheel or override its metadata. `requirements-genlayer.in` and its hashed locks preserve the tested source/SDK pair.

## Contract and boundary

`tests/genlayer_direct/contracts/procurement_evaluation.py` accepts a `tenderlens.evaluation-submission.v1` JSON snapshot. It validates the request deterministically, then calls `gl.nondet.exec_prompt` under `gl.eq_principle.strict_eq` for requirements with evidence. Requirements with no matching/global evidence are deterministically marked `insufficient_evidence` without an LLM call. The model must return a structured status, reason, and evidence-ID array; IDs must belong to the evidence supplied for that requirement, and result references are rebuilt from the submitted snapshot. The versioned `tenderlens.evaluation-result.v1` includes an aggregation policy, per-requirement status and reasoning, mandatory flag, source-backed document version/hash/byte-range references, an overall status, and unresolved requirements. The contract is one-shot: a completed evaluation cannot be overwritten.

The selectable policies are `mandatory_gate_optional_advisory_v1` (default: mandatory non-compliance is distinct from optional non-compliance; unresolved requirements yield inconclusive unless mandatory non-compliance exists) and `all_requirements_blocking_v1` (any non-compliance blocks). Insufficient and contradictory states are retained in unresolved requirements in either policy. They are evaluation summaries, not legal or award rules.

The authenticated backend now has immutable `evaluation-snapshots` routes. They freeze all currently registered requirements and explicitly selected evidence records, including source excerpts and immutable document-version provenance, into a canonical payload and SHA-256 fingerprint. Canonical bytes are Python `json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`, with the `snapshot_sha256` field omitted; the lowercase SHA-256 hex digest is then attached. This is only a preparation boundary: route state is `prepared_not_submitted`. There is no transaction submission, wallet, contract address, transaction hash, network status polling, result callback, or finalization API.

**Integrity trust boundary:** the contract checks only that the supplied digest is 64 lowercase hex characters and preserves it. Research did not establish SHA-256 support for the exact deployed GenVM runner behind this contract (`py-genlayer:latest`); pinned Direct Mode executes Python directly and cannot prove WASM runner support. Accordingly, no in-contract recomputation is claimed. A changed payload with a digest-shaped value can be accepted. Integrity therefore depends on authenticated backend snapshot creation and the future adapter passing/associating the exact stored payload and comparing both returned snapshot ID and digest. See `docs/api-contracts.md` for serialization details.

## Tests and limitations

Run:

```sh
.venv/bin/python -m pytest tests/genlayer_direct -v
python -m unittest backend.test_api -v
npm run typecheck
npm test
npm run build
npm run lint
```

The Direct Mode suite deploys the actual contract in the pinned in-memory runner and tests valid versus tampered-but-digest-shaped payloads (demonstrating the digest trust boundary), malformed model output, fabricated citations, source-backed references, provenance, stable schema, and mandatory/optional policy combinations across compliant, non-compliant, insufficient, and contradictory statuses. Direct Mode does not perform real nondeterministic model inference or validator consensus: its documented `direct_vm.mock_llm` cheatcode returns fixed model text while real contract Python code, storage, methods, and state transitions execute. This is not a live deployment or production-readiness claim.

Known constraints: only supplied `text/plain` byte-range evidence is supported; PDF/DOCX extraction, source authenticity, OCR, amendment semantic comparison, verified objective fact extraction, and adversarial prompt-injection resistance are not solved. A model-supported status is an evidence interpretation, not legal advice or an automatic award. Procurement documents may be confidential; contract arguments/calldata/state may be public on a live network, so the prototype is intentionally not deployed.
