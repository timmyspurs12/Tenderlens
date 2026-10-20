# Milestone 6 — pinned SDK driver and lifecycle boundary

## Checkout provenance

The actual checkout for this work was branch `arena/a72eb9de-tenderlens` at `5f1022acfbb5fdd8d5892e273a836ac356a06242`, with pre-existing modified and untracked Milestone 4/5 files. A prior report cited `e52d19a07f87d4555411cb0176daa654ef1167f6` and a clean tree, but neither matched this checkout. No reset or discard was performed; relevant pre-existing work was preserved and included with these changes.

## Verified SDK source and interface

The driver was implemented against the repository pin `genlayer-py==0.18.0`, source commit `cf421edc0d20e567cc5a3b4da198fce61f1e579f` (fetched and inspected from that exact commit). Its package metadata requires Python `>=3.12`. The ordinary development host used in this review is Python 3.11.2 and has no installed GenLayer SDK; the locked Python 3.12 workflow is the supported SDK import/signature check.

Pinned-source findings used by `backend/genlayer_sdk_driver.py`:

- `genlayer_py.create_account(account_private_key=...)` creates the local signer; `create_client(chain, endpoint, account)` builds a client. The SDK's `create_client` mutates the supplied chain descriptor when an endpoint override is used, so the driver deep-copies it and only accepts the endpoint already present in that named SDK descriptor.
- `GenLayerClient.deploy_contract(code, account, args)` accepts contract source and constructor arguments and returns an SDK transaction hash. The exact authenticated snapshot serialization is passed as the single constructor argument (`args=[submission_json]`).
- `write_contract(address, function_name, account, args)` returns the evaluation transaction hash. The current contract stores one submission in its constructor, so the driver deploys a distinct instance for each evaluation; it cannot safely reuse a single configured contract address.
- `get_transaction(transaction_hash=...)` supplies decoded status and `tx_execution_result_name`; `wait_for_transaction_receipt(transaction_hash, status, interval, retries, full_transaction)` has bounded polling and raises on timeout. The driver uses the query method for explicit lifecycle classification and uses the SDK receipt API after a finalized deployment to extract the contract address.
- `TransactionStatus` includes `PENDING`, `ACCEPTED`, `FINALIZED`, `CANCELED`, `UNDETERMINED`, and timeout/appeal states. `ExecutionResult` includes `FINISHED_WITH_RETURN`, `FINISHED_WITH_ERROR`, and `NOT_VOTED`. Only `FINALIZED` plus `FINISHED_WITH_RETURN` is treated as a successful execution. `CANCELED` and finalized execution errors are recorded as failed; undecided, unrecognized, unavailable, or timed-out outcomes remain pending/unknown and are not resubmitted.
- `read_contract(address, function_name, args, account)` decodes the contract return using the SDK calldata decoder. The driver requests `get_result()` only after a finalized successful evaluation. Its output is still validated and associated by the application adapter; SDK decoding alone is not proof of result authenticity.

The e2e examples in the pinned source demonstrate deployment address extraction from `data.contract_address` for Localnet/Studio and `tx_data_decoded.contract_address` for Asimov. The driver uses the corresponding SDK receipt fields. Asimov and Bradbury both use chain ID 4221 in this SDK pin, so chain ID alone does not uniquely identify the network; custom RPC URLs are rejected and the selected named SDK endpoint is retained. Even this does not independently authenticate the remote endpoint or prove validator consensus.

## Driver gates and current external blockers

`GenLayerSDKDriver` is a concrete SDK submission port, but is not wired to an API route or automatically enabled. A caller passes it to `EvaluationAdapter.submit(...)`. The adapter supplies a process-local verified-input capability minted only after re-reading and authenticating the persisted snapshot; the driver rejects raw caller JSON and verifies the capability's request/snapshot/digest/policy associations. It refuses to initialize the SDK client unless broadcast authorization is explicit, the contract runtime is qualified for the selected target, and a target-specific funding verifier is supplied. These callbacks are required integration points, not built-in evidence: this repository currently has no verified GenVM/WASM runtime qualification artifact and no pinned-SDK high-level GenLayer fee estimator. Funding sufficiency must not be inferred from an EVM balance or gas estimate alone.

The checked-in contract still declares mutable `py-genlayer:latest`; the driver rejects that reference before SDK client creation. The callback must qualify the exact runtime dependency reference against a real target; merely returning `True` is an operator/integration assertion and is not independently proven by this adapter. The SDK chain descriptors do not establish that the checked-in contract is executable on a deployed GenVM/WASM target. No network probe, account creation against a network, deployment, or transaction was run in this milestone.

The runtime readiness and funding blockers therefore remain external. The driver code has a real SDK call path but is intentionally unusable with the current checked-in runtime pin and without the required external verifiers. It is not a mock live driver, and test fixtures are explicitly non-deployable.

## Durable state and recovery

`evaluation_requests.status` remains the coarse backward-compatible state; `lifecycle_state` and the associated transaction fields form the durable phase ledger:

| Lifecycle state | Meaning / recovery rule |
| --- | --- |
| `prepared` | Verified snapshot and idempotency row persisted; no runtime action. |
| `submission_in_progress` / `deployment_submission_in_progress` | Intent persisted before deployment call. If a crash leaves no returned hash, outcome is uncertain; never auto-deploy again. |
| `deployment_submitted` / `pending` | Deployment hash is durable before polling. Reconciliation queries this hash; no second deployment. |
| `deployment_finalized` | SDK reported finalized successful deployment and the receipt supplied a validated address. Read-only reconciliation stops here by default; explicitly gated `resume=True` may begin the still-unattempted evaluation once. |
| `evaluation_submission_in_progress` | Persisted before `write_contract`. If no hash was persisted, the call may have been accepted; reconciliation does not repeat it. |
| `evaluation_submitted` / `pending` | Evaluation hash is durable before polling. Reconciliation queries only this hash. |
| `finalized` | SDK reported `FINALIZED` and `FINISHED_WITH_RETURN`; result read/ingestion can be retried without a new transaction. |
| `completed` | Result associations, schema, citations, and policy aggregation passed adapter validation and were durably stored. Duplicate identical ingestion is idempotent; conflicting ingestion fails. |
| `failed` | A known terminal cancellation or finalized execution error was observed. Static prerequisites are checked before the request is claimed; they leave it `prepared` when they fail. No automatic retry. |
| `outcome_unknown` | Provider/query/write ambiguity, database-write boundary, or timeout cannot prove whether the external call took effect. Query only if a hash was durably recorded; without a hash, operator reconciliation is required. |

The intent row is committed before any deployment transaction. Returned transaction hashes are committed before polling. A crash between the SDK returning a hash and the SQLite hash update can still leave an unknown transaction with no locally persisted identifier; the code fails closed and cannot promise exactly-once submission across that unavoidable database/network boundary. `reconcile()` is query-only by default and uses a read-only runtime context without broadcast/funding gates; `resume=True` requires the full broadcast/runtime/funding gates and can only advance from a known finalized deployment before an evaluation attempt marker. It never retries an uncertain transaction. Recovery never substitutes a snapshot or repairs data.

## Trust and evidence boundaries

- Backend HMAC/authenticated snapshot verification is an application-side authenticity/binding control within the configured backend trust boundary; canonical SHA-256 metadata alone is not authenticity.
- SDK receipt/status checks are observations returned by a configured endpoint, not independently authenticated validator-consensus proofs. Stored result association/reference validation does not prove on-chain origin.
- Contract-side field/schema checks and model interpretation are separate from the adapter's integrity checks. Direct public contract callers can bypass application binding.
- Policy aggregation remains `mandatory_gate_optional_advisory_v1` or `all_requirements_blocking_v1`. Output remains evidence interpretation, not legal advice, procurement eligibility, or an award decision.
- Direct Mode remains mocked nondeterministic execution; it establishes neither actual-model accuracy nor deployed GenVM/WASM compatibility or validator consensus.
