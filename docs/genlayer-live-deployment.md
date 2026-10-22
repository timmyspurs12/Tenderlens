# Live GenLayer deployment (Studionet)

This document records the first successful deployment of
`tests/genlayer_direct/contracts/procurement_evaluation.py` to GenLayer
Studionet, the evidence that backs the claim, and the steps still gated behind
an operator decision. It is a deployment record, not a target-runtime
qualification, a procurement determination, or an audit of validator behaviour.

## Deployed artifact

| Item | Value |
| --- | --- |
| Network | Studionet, chain id `61999` |
| RPC endpoint | `https://studio.genlayer.com/api` (pinned) |
| Contract address | `0x481d38ede6aC854b13F3b677bc31FeCEB9214126` |
| Deploy transaction | `0x92801b0bd50424373ab38251a0fd5f5974272de92ed9cde358f1c4a3d2428818` |
| Transaction state | `FINALIZED` (accepted in 59 s) |
| Signer address | `0xc9f9f74dA9C84851d4e5E1d7C736C100f685E080` |
| Runtime | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` |
| Contract source SHA-256 | `4cad32bead8eaacda7a090925bb1b53158c5d863a9efc02d580fe33d087c1247` |
| Snapshot digest | `7a7e703ca045b5d6a9ad34213b2c8d3fff3263fe961df68dc2983c3c924577b9` |
| Explorer | <https://explorer-studio.genlayer.com/address/0x481d38ede6aC854b13F3b677bc31FeCEB9214126> |
| Evidence record | [`deployments/studionet-20261022T130714Z.json`](../deployments/studionet-20261022T130714Z.json) |
| Workflow run | `39783933383` (`GenLayer deployment`) |
| Checks run | `39783933797`, then `39784918827` after the routing fix (both green) |

`deployed_code_matches_source: true` in the evidence record means the runtime at
the reported address hashed to the reviewed source. It does not prove that the
contract behaves correctly under real validator consensus, and no evaluation
transaction has been executed against it yet.

## How the deployment ran

The deployment is a deliberate, auditable action; ordinary pushes never sign
anything.

1. `.github/deploy-requests/genlayer-deploy.yml` must contain `confirm: deploy`.
   A push that changes that file triggers `.github/workflows/genlayer-deploy.yml`
   on an `arena/**` branch. Manual dispatch is also available with
   `confirm: deploy` and a reviewed network.
2. `scripts/deploy_genlayer.mjs` validates the submission payload and the
   confirmation string, reads `TENDERLENS_GENLAYER_NETWORK`, and signs with
   `TENDERLENS_GENLAYER_PRIVATE_KEY` from repository secrets. The key is never
   committed and never exposed to the browser.
3. The script waits for finality, resolves the deployed address, checks the code
   hash against the reviewed source, and writes
   `deployments/<network>-<timestamp>.json`.
4. The workflow commits that evidence file back to the branch so the record
   survives log retention.

### Two failed attempts, and what they fixed

- `deployments/studionet-20261022T130525Z.json` — `failed_before_record`: the
  first attempt reached `FINALIZED` but the script could not find the deployed
  address and aborted with `deployed_contract_address_missing`. Diagnostics in
  that state were thin, so the script now writes an `{outcome: 'error', code,
  hint}` record and fails the job instead of dying silently.
- `39783694747` — the retry failed the same way. Root cause: `genlayer-js`
  does not always return `txDataDecoded.contractAddress`; for these deploy
  transactions the address is the consensus transaction recipient.
  `extractContractAddress()` in `api/_genlayer.js` now accepts the decoded
  address, `recipient`, `to_address`, `to`, or `data.recipient`, rejects the
  zero address, and fails closed with a hint that names the transaction hash and
  status when none are present.

`probe_submission` describes the payload rather than the broadcast: `true` means
the run deployed the synthetic probe submission instead of a stored tender
snapshot. The successful record was corrected to `true` after the script learned
to record probe mode; it still carries a real transaction hash and `FINALIZED`
status.

## Verifying the live state

The production build serves a read-only GenLayer status document:

```bash
curl -s https://tenderlens-lilac.vercel.app/api/genlayer | jq .
```

Expected shape (values change over time):

```json
{
  "configured": true,
  "network": "studionet",
  "expected_chain_id": 61999,
  "observed_chain_id": 61999,
  "chain_identity_verified": true,
  "endpoint_pinned": true,
  "reachable": true,
  "contract": {
    "address": "0x481d38ede6aC854b13F3b677bc31FeCEB9214126",
    "code_present": true,
    "lifecycle_status": "prepared",
    "result": null
  },
  "deployment": {
    "enabled": false,
    "blockers": ["deployment_flag_off", "signer_key_absent", "contract_address_already_set"]
  }
}
```

`contract.result: null` is correct: the contract is deployed and holds no
evaluation result yet. The UI renders that as an absence, not as an outcome.

## Running the on-chain evaluation

The contract stores its submission immutably at construction, so an evaluation
can only interpret what is already on chain. It exposes one write,
`evaluate()`, which requires `lifecycle_status == "prepared"` and then records
the consensus result; it has no sender or owner check, so any funded or unfunded
key can call it — the gate is the workflow, not the contract.

Studionet is gasless: the deploy transaction was signed by an ephemeral key with
a zero balance and still reached `FINALIZED`. Funding the signer with GEN is
therefore optional on 61999; the public faucet
(`testnet-faucet.genlayer.foundation`) serves Asimov/Bradbury only and requires
0.01 ETH on mainnet, so claiming there does not change a Studionet balance.

To evaluate, run the `GenLayer deployment` workflow with `mode: evaluate`
(manual dispatch) or set `mode: evaluate` in
`.github/deploy-requests/genlayer-deploy.yml` and commit it. The workflow:

1. resolves `GENLAYER_CONTRACT_ADDRESS` from the repository variable and passes
   it as `TENDERLENS_GENLAYER_CONTRACT_ADDRESS` — it never guesses the target;
2. sets `TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED=1` **only** for evaluate
   requests, so deploy and probe runs stay non-broadcasting;
3. signs with `GENLAYER_PRIVATE_KEY` (repository secret, never in the repo);
4. waits for consensus finality and writes
   `deployments/evaluation-<network>-<stamp>.json` with the transaction hash,
   result name, lifecycle status after the write, the summarized result and a
   `probe_submission` flag read back from `get_submission()`.

Without the secret the job fails closed with `signer_not_configured`; without
the flag it fails with `broadcast_not_authorized`; a contract that is no longer
`prepared` fails with `contract_not_prepared`. The live read-only status route
never signs and reports `deployment.broadcast_authorized: false` by default.

## Production routing note

Vercel's filesystem router did not bind the bracketed catch-all
`api/[...path].js` for nested paths on this project: `/api/tenders` reached the
function while `/api/auth/session` (and any other two-segment path) returned the
platform 404 page, which left the workspace stuck on "Connecting…" and showed
the sign-in screen for every visitor. Each public route now has an explicit
function file (`api/tenders.js`, `api/auth/session.js`, `api/auth/login.js`,
`api/auth/logout.js`) that imports the shared handler from `api/_gateway.js`;
`api/[...path].js` remains as a fallback. When adding a route, add a file — do
not rely on the catch-all.

## Still gated

The deployment succeeded, but broadcasting evaluation transactions remains
fail-closed:

- `TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED=1` must be set server-side, together
  with a live, scoped authorization record; a request field cannot enable it.
- A server-held signer key is required; no key is present in the Vercel project,
  so `deployment.enabled` is `false` and `signer_key_absent` is reported.
- Redeploying an address that is already set requires
  `TENDERLENS_GENLAYER_ALLOW_REDEPLOY=1`, which stays off by default.

Records access is separate from the GenLayer read-only status: the workspace
still needs the owner passphrase configured in Vercel, and the gateway proxies
only tender and evaluation-request paths.

## Production verification (2026-10-22T13:15Z)

Checked against the production alias `https://tenderlens-lilac.vercel.app` after
the routing fix (commit `17b0756`, deployment
`dpl_2c7ECDTKK8iDr8dDCHDcGe1MdjU9`, `READY`/`PROMOTED`, 6 functions):

| Request | Result |
| --- | --- |
| `GET /api/genlayer` | `200` — `reachable: true`, `observed_chain_id: 61999`, `chain_identity_verified: true`, `contract.code_present: true`, `lifecycle_status: "prepared"`, `result: null` |
| `GET /api/auth/session` | `401` — `{"error":{"code":"unauthorized", …}}` (previously the SPA HTML) |
| `GET /api/auth/login` | `405` — `{"error":{"code":"method_not_allowed","message":"Use POST."}}` |
| `GET /api/tenders` | `401` — workspace sign-in still required |
| `GET /api/gateway-tests` | `404` from the gateway — the test suites are no longer deployed as endpoints |

The built client bundle contains `/api/genlayer`, the
`tenderlens.genlayer-status.v1` schema check, and the live-status copy, and no
longer contains the retired "No verified live TenderLens contract" claim.
