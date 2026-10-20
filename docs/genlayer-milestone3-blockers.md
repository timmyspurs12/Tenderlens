# GenLayer milestone: procurement evaluation prototype

## Verified runtime

The local Agent Mode host remains CPython 3.11.2 and does not have the GenLayer packages installed. Runtime verification is delegated to GitHub Actions on the writable feature branch. The workflow provisions Python 3.12, Node 22.14.0, and `uv==0.13.3`, installs the lockfiles, checks `genlayer-test==0.29.2`, `genlayer-py==0.18.0`, `pytest==9.1.1`, source commit `343e3a358f9e235a93b49c60721ce7676585ff07`, and `gltest --help`, then runs the Direct Mode suite.

**Compatibility pin:** the pinned upstream source commit declares suite version 0.29.2 and permits `genlayer-py>=0.18.0,<0.20.0`. The PyPI wheel `genlayer-test==0.29.2` instead requires `genlayer-py>=0.13.0,<0.17.0`; do not replace the source pin with that incompatible wheel or override its metadata. `requirements-genlayer.in` and its hashed locks preserve the tested source/SDK pair.

## Contract and boundary

`tests/genlayer_direct/contracts/procurement_evaluation.py` accepts a `tenderlens.evaluation-submission.v1` JSON snapshot. It validates the request deterministically, then calls `gl.nondet.exec_prompt` under `gl.eq_principle.strict_eq` for requirements with evidence. Requirements with no matching/global evidence are deterministically marked `insufficient_evidence` without an LLM call. The versioned `tenderlens.evaluation-result.v1` includes per-requirement status and reasoning, evidence IDs plus document version/hash/byte-range references, an overall `compliant` / `non_compliant` / `inconclusive` status, and unresolved requirements. The contract is one-shot: a completed evaluation cannot be overwritten.

The authenticated backend now has immutable `evaluation-snapshots` routes. They freeze all currently registered requirements and explicitly selected evidence records, including source excerpts and immutable document-version provenance, into a canonical payload and SHA-256 fingerprint. This is only a preparation boundary: route state is `prepared_not_submitted`. There is no transaction submission, wallet, contract address, transaction hash, network status polling, result callback, or finalization API. The contract validates the snapshot fingerprint's format and preserves it, but does not cryptographically authenticate the backend or independently recompute the digest; production wiring must validate the result against the stored immutable snapshot.

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

The Direct Mode suite deploys the actual contract in the pinned in-memory runner and tests compliant, explicitly non-compliant, missing, and contradictory evidence; malformed inputs; provenance; stable schema; and one-shot lifecycle. Direct Mode does not perform real nondeterministic model inference or validator consensus: its documented `direct_vm.mock_llm` cheatcode returns fixed model text while real contract code, storage, methods, and state transitions execute. This is not a live deployment or production-readiness claim.

Known constraints: only supplied `text/plain` byte-range evidence is supported; PDF/DOCX extraction, source authenticity, OCR, amendment semantic comparison, verified objective fact extraction, and adversarial prompt-injection resistance are not solved. A model-supported status is an evidence interpretation, not legal advice or an automatic award. Procurement documents may be confidential; contract arguments/calldata/state may be public on a live network, so the prototype is intentionally not deployed.
