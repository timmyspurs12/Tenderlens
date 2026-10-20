# Milestone 4 — snapshot integrity and deployment-readiness audit

## Audit scope and decision

This audit follows the repository state at `54e6b551e3e47f23db1452a0202f31ecf8e942a4` and the GenLayer sources cited below. The procurement contract remains a Direct Mode test contract; the repository has no network submission adapter, deployment script, result callback, or configured wallet/network. The contract header uses the mutable runner reference `py-genlayer:latest`.

The exact runner artifact resolved by a target network could not be established. The repo-pinned Direct Mode suite executes ordinary Python and cannot prove WASM compatibility. Consequently this milestone does **not** add `hashlib` or any other unverified in-contract primitive and does not claim trustless on-chain snapshot verification. Instead it strengthens and tests the supported backend verification boundary and provides a fail-closed deployment preflight.

## Snapshot lifecycle and trust matrix

| Stage | Implemented behavior | What it establishes / does not establish |
| --- | --- | --- |
| Requirements | Authenticated workspace API stores explicit requirement text, category, kind, and `mandatory`; snapshot builder selects all registered rows. | Records what this backend received; not procurement-law correctness. |
| Evidence | `text/plain` excerpts are sliced from exact stored bytes. Content hash, document version, and byte range are rechecked while a snapshot is constructed. | Byte integrity and lineage to this backend's uploaded blob; not document publisher authenticity, truth, or legal significance. |
| Construction | Backend creates `snapshot_id`, builds the specified nested JSON body, hashes canonical JSON with sorted object keys, compact separators, UTF-8, and no Unicode normalization, then stores payload and `input_sha256`. | An unkeyed digest gives payload-to-digest consistency only; it does not authenticate an author. Array order is preserved. |
| Persistence | SQLite update/delete triggers enforce application-level append-only snapshots. Each row also has an internal HMAC over row IDs, workspace/tender identity, creation timestamp, digest, and exact stored JSON. | HMAC detects row edits without the server key; not protection against application/key compromise or a database operator who also controls the key. |
| Retrieval | Snapshot list and item GET verify row HMAC, embedded ID versus requested/database ID, digest shape, embedded digest versus stored digest, and recomputed canonical digest. Invalid rows fail with `500 integrity_failure`; absent/mismatched requested IDs return `404`. | Trusted backend retrieval boundary. This does not make an unkeyed digest a signature or authenticate any independent contract call. |
| Contract input | A Direct Mode test passes a backend-retrieved submission to the contract. The contract checks schema and digest format only. A caller can initialize it with modified data and either stale-but-shaped or newly recomputed digest. | The contract is not bound to the database snapshot and cannot authenticate TenderLens. The adversarial test preserves this limitation explicitly. |
| Result | `evaluate()` uses stored contract input and echoes its evaluation/snapshot IDs and digest in the result. | Correspondence to that contract's own constructor data; not proof that constructor data matched a backend row or that a network receipt was accepted. No result ingestion/association endpoint exists. |

### Internal HMAC key and migration

`TENDERLENS_SNAPSHOT_HMAC_KEY` can provide a dedicated, stable secret (at least 32 characters); if unset, the API token is used. The tag stays in SQLite and is never returned. Production operators should set a separate protected key and preserve it across restarts. Rotation invalidates old tags and therefore fails closed until an explicit maintenance re-seal process is performed.

Existing six-column snapshot tables are migrated without changing public response shapes. Rows with an embedded ID and digest that match the stored ID/digest are HMAC-sealed during upgrade; inconsistent rows are left unsealed and fail retrieval. This migration establishes trust in the contents present at upgrade time, not historical authenticity: a previously modified payload with a newly recomputed digest cannot be distinguished by the old schema. Back up and review the old store before upgrading.

## GenLayer runtime and tool investigation

- Repository pins `genlayer-py==0.18.0`; upstream tag `v0.18.0` resolves to commit `cf421edc0d20e567cc5a3b4da198fce61f1e579f`. Its SDK requires Python 3.12+, provides `create_client` and `deploy_contract`, and documents StudioNet, Bradbury, Asimov, and localnet chain descriptors. This SDK is the off-chain transaction client, not proof of the runner installed by validators.
- Direct Mode pins `genlayer-test==0.29.2` to testing-suite source commit `343e3a358f9e235a93b49c60721ce7676585ff07`; Direct Mode is in-memory Python execution with mocked model responses.
- No GenLayer CLI dependency/version, deployment script, RPC configuration, target network selection, wallet secret, or funding check is present in this repo.
- GenVM Manager commit `fdca7b729c08995343ad42a143ca7bf6081a99ef` links GenVM executor commit `2e094c6fdd7fb3b620706a2d20845e004fb6d298`. That executor's CPython WASI build source includes HACL SHA-2 objects, which is evidence that SHA-2 support exists in that source line. It does **not** identify the runner hash used by a target network or demonstrate `hashlib.sha256` behavior for this contract's mutable `py-genlayer:latest` dependency.
- GenVM runner documentation describes `latest` as a mutable alias and content-addressed runner references as 256-bit GVM32 hashes. Until the target's exact `py-genlayer` hash is selected and exercised under matching GenVM/Studio, this contract is not reproducibly pinned to a deployed runtime. No SHA-256 recomputation is added to the contract on the basis of Direct Mode or unbound source evidence.
- Contract source is currently 17,454 bytes at this audited revision; no repository-verified target contract-size ceiling was found, so the preflight reports size but does not invent a threshold.

Useful source references:

- [Pinned genlayer-py v0.18.0](https://github.com/genlayerlabs/genlayer-py/tree/cf421edc0d20e567cc5a3b4da198fce61f1e579f)
- [Pinned GenLayer Testing Suite](https://github.com/genlayerlabs/genlayer-testing-suite/tree/343e3a358f9e235a93b49c60721ce7676585ff07)
- [GenVM Manager audit commit](https://github.com/genlayerlabs/genvm-manager/tree/fdca7b729c08995343ad42a143ca7bf6081a99ef)
- [Linked GenVM executor audit commit](https://github.com/genlayerlabs/genvm-executor/tree/2e094c6fdd7fb3b620706a2d20845e004fb6d298)
- [GenVM runner identity and hash format](https://github.com/genlayerlabs/genvm-manager/blob/fdca7b729c08995343ad42a143ca7bf6081a99ef/docs/website/src/spec/02-execution-environment/04-runners.rst)

## Read-only preflight

Run `python scripts/genlayer_preflight.py`. It reports installed SDK/test pins, Python version, source hash/size, runner pin shape, configured target network, and the presence/shape (never value) of the deployment key. It exits nonzero while any blocker remains. Optional `--probe-rpc` makes one read-only `eth_chainId` request to the selected SDK network; it does not submit a transaction. It never prints a key or attempts deployment. A nonzero exit is expected until exact runner compatibility, authorization, wallet, network, and funding prerequisites are demonstrated.

The preflight cannot establish validator consensus, source compilation against the target runner, a fee threshold, or production readiness. SDK `deploy_contract` is documented but is deliberately not invoked here.

## Preserved policy and evidence controls

The existing `mandatory_gate_optional_advisory_v1` and `all_requirements_blocking_v1` results are unchanged. Structured citations remain confined to evidence supplied to each requirement; references in results are reconstructed from that submission. Malformed or unsupported model responses revert evaluation rather than becoming positive compliance. Model output remains an interpretation—not proof of document authenticity, legal eligibility, or an award decision. Mocked Direct Mode responses do not establish real-model quality or validator agreement.
