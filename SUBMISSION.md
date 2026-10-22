# GenLayer Portal submission — copy/paste package

Everything a steward needs to verify the TenderLens Intelligent Contract. All
links are public; no login is required to read the source, the explorer pages or
the live status document.

- Portal: <https://portal.genlayer.foundation/submit-contribution>
- Contribution type: **Builder → Contract Deployed**
- Network: GenLayer Studionet, chain id `61999`
- Contract: `0x481d38ede6aC854b13F3b677bc31FeCEB9214126`

## Exact explorer URLs

| What it shows | URL |
| --- | --- |
| Contract page (creator, deploy tx, 2 transactions, both `FINALIZED`) | <https://explorer-studio.genlayer.com/address/0x481d38ede6aC854b13F3b677bc31FeCEB9214126> |
| Deploy transaction — `FINALIZED`, GenVM `SUCCESS`, consensus `Accepted` | <https://explorer-studio.genlayer.com/tx/0x92801b0bd50424373ab38251a0fd5f5974272de92ed9cde358f1c4a3d2428818> |
| `evaluate()` transaction — `FINALIZED`, GenVM `SUCCESS`, consensus `Accepted` | <https://explorer-studio.genlayer.com/tx/0x9b99041d8e1009ebcf420341addc396585a93e44e4967ef2a76f52c0520501aa> |

Both explorer paths are live and were checked on 2026-10-22:
`/address/<contract>` and `/tx/<hash>`. The `/contracts/<contract>` path also
resolves to the same contract page.

## Portal fields

**Title**

```
TenderLens — procurement evaluation Intelligent Contract (deployed + evaluated on Studionet)
```

**Description** (fits the 1,000-character field)

```
TenderLens is a source-first procurement review workspace. Its GenLayer Intelligent Contract, procurement_evaluation.py, evaluates a frozen tender snapshot: validators interpret each requirement against cited evidence excerpts and return a structured result (per-requirement status, evidence references, unresolved items) plus a disclaimer. Deterministic checks validate the snapshot digest, sorted-key canonical JSON and evidence provenance before any model interpretation. Lifecycle: prepared -> evaluated (one-way), with get_lifecycle_status, get_submission and get_result views. Deployed on Studionet (61999) and evaluated once: the deploy and evaluate transactions are both FINALIZED with GenVM SUCCESS and consensus Accepted. The evaluation used a labelled DEPLOYMENT-PROBE payload (no real tender data exists), so its result is inconclusive by design and is disclosed as a smoke test, not a procurement finding. The Vercel app reads the contract live through /api/genlayer.
```

**Evidence** — add each row as its own evidence item (description + URL):

| Description | URL |
| --- | --- |
| Deployed contract on the GenLayer Studio explorer (`0x481d…4126`) | `https://explorer-studio.genlayer.com/address/0x481d38ede6aC854b13F3b677bc31FeCEB9214126` |
| Deploy transaction, FINALIZED · GenVM SUCCESS · consensus Accepted | `https://explorer-studio.genlayer.com/tx/0x92801b0bd50424373ab38251a0fd5f5974272de92ed9cde358f1c4a3d2428818` |
| `evaluate()` transaction, FINALIZED · GenVM SUCCESS · consensus Accepted | `https://explorer-studio.genlayer.com/tx/0x9b99041d8e1009ebcf420341addc396585a93e44e4967ef2a76f52c0520501aa` |
| Public repository (source, tests, evidence records) | `https://github.com/timmyspurs12/Tenderlens` |
| Contract source, commit-pinned | `https://github.com/timmyspurs12/Tenderlens/blob/6fbc16dd27f1cea774c4cbddc560d33b6cecf635/tests/genlayer_direct/contracts/procurement_evaluation.py` |
| Deployment runbook and verification notes | `https://github.com/timmyspurs12/Tenderlens/blob/6fbc16dd27f1cea774c4cbddc560d33b6cecf635/docs/genlayer-live-deployment.md` |
| Machine-readable deployment evidence | `https://github.com/timmyspurs12/Tenderlens/blob/6fbc16dd27f1cea774c4cbddc560d33b6cecf635/deployments/studionet-20261022T130714Z.json` |
| Live app's read-only GenLayer status (JSON) | `https://tenderlens-lilac.vercel.app/api/genlayer` |

## What the evidence does and does not establish

- It establishes: a real deployment and a real `evaluate()` write on chain, both
  finalized with successful GenVM execution and accepted consensus; the deployed
  code matches the reviewed source hash; the contract's views answer from the
  live node.
- It does not establish: that the contract is correct under all validator
  compositions, that the product is production-ready, or that any procurement
  determination is valid. The single evaluation used the labelled probe payload,
  and the contract accepts exactly one evaluation, so this deployment can never
  carry a real tender result.

## Before submitting

1. The portal requires a complete profile (display name + email).
2. Do not describe the probe result as a tender finding — say "smoke test".
3. If a steward asks for a persistent testnet deployment, that requires a
   Bradbury deployment from a faucet-funded wallet; the public faucet requires
   0.01 ETH on mainnet. Studionet links are what this repository can evidence.
