# GenLayer deployment evidence

`scripts/deploy_genlayer.mjs` writes the machine-readable outcome of each
deployment attempt here, and the `GenLayer deployment` workflow commits it so
the record survives workflow-log retention.

These files record what a configured GenLayer node returned. They are not an
audit of validator behaviour, not a legal opinion, and not a procurement award.
A `outcome: pending_finality` record means a transaction was broadcast but was
not observed as final; check the reported address and transaction before
treating it as deployed.

## Records in this directory

| File | Outcome |
| --- | --- |
| `studionet-20261022T130525Z.json` | `failed_before_record` — the first attempt reached finality but the deployed address could not be resolved, so no contract address is claimed. |
| `studionet-20261022T130714Z.json` | `finalized` — the live contract (`0x481d38ede6aC854b13F3b677bc31FeCEB9214126`, tx `0x92801b0bd50424373ab38251a0fd5f5974272de92ed9cde358f1c4a3d2428818`) with `deployed_code_matches_source: true`. |

Field notes:

- `probe_submission` describes the **payload**, not the broadcast: `true` means the run
  deployed with the synthetic probe submission instead of a stored tender
  snapshot. Whether a transaction was broadcast is answered by
  `transaction_hash`, `status` and `contract_address`.
- `contract_source_sha256` is the digest of the reviewed contract source the job
  deployed; `deployed_code_matches_source` compares it with the code read back
  at the reported address.
- `snapshot_digest` and `digest_matches` describe the submission payload, not the
  chain state.
- `outcome: error` records always carry `code` and `hint`; they are written from
  the failure path so a failed job leaves evidence instead of nothing.
- `receipt`, `result_name` and `execution_result` capture the consensus
  transaction's decoded result. A `null` `result` on a deployed contract means no
  evaluation has been recorded yet, not that the contract is missing.

See [`../docs/genlayer-live-deployment.md`](../docs/genlayer-live-deployment.md)
for the operator runbook and the production routing note.

## Evaluation records

`evaluation-<network>-<timestamp>.json` follows the same principle for the
single `evaluate()` write:

- `outcome: finalized` carries `transaction_hash`, `result_name`,
  `execution_result`, `lifecycle_status` after the write, the summarized on-chain
  `result`, and `probe_submission` — `true` means the contract's immutable
  submission is the labelled deployment probe rather than a stored tender
  snapshot. A probe result is a network/consensus smoke test, not a procurement
  finding.
- `outcome: pending_finality` means the write was broadcast but not observed as
  final; do not treat the lifecycle or result as settled.
- `outcome: error` carries `code`/`hint` from the fail-closed gate
  (`broadcast_not_authorized`, `signer_not_configured`, `contract_not_prepared`).
