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

## Path to follow in the live build (paste into the form)

Written as user navigation, not a reviewer checklist. Use the
`tenderlens-lilac.vercel.app` alias, never a per-deployment URL: writes are
checked against that exact origin.

Compact variant:

```
1. Open https://tenderlens-lilac.vercel.app and click "Open the workbench".
2. The tender register loads at /app with no sign-in: browse it, or search by title and reference.
3. Click "Register tender" (/app/tenders/new). Enter a published title and reference, submit, and the new record opens in the register at /app/tenders/<id>.
4. Open Settings (/app/settings) to read the contract live: Studionet 61999, chain identity verified, contract 0x481d38ede6aC854b13F3b677bc31FeCEB9214126, code present, lifecycle "evaluated".
```

Longer variant, step by step:

```
1. Open https://tenderlens-lilac.vercel.app. The landing page states what the product does and its limits; the interface preview is labelled ILLUSTRATIVE - NO TENDER DATA. Click "Open the workbench".
2. The workbench opens at https://tenderlens-lilac.vercel.app/app with no account and no passphrase. If the records service has been idle, this first load can take up to a minute while the instance starts; the page says it is waking the service and retries automatically. The strip across the top shows how many tenders are registered and whether the records service is connected. The search box (Cmd/Ctrl+K) filters by title or reference.
3. Click "Register tender", or open https://tenderlens-lilac.vercel.app/app/tenders/new. Enter the published tender title and reference, optionally the deadline, and submit. The record is written to the workspace service and appears in the register.
4. Click a register row to open that tender at https://tenderlens-lilac.vercel.app/app/tenders/<id>. The banner shows the stored title, reference, deadline and registration time, with Overview, Documents, Amendments, Evaluation and Report tabs. Views report plainly when the connected service has returned no tender-specific data rather than inventing it.
5. Open https://tenderlens-lilac.vercel.app/app/settings for the GenLayer panel, read live on page load: Studionet (chain id 61999), endpoint pinned, contract 0x481d38ede6aC854b13F3b677bc31FeCEB9214126, code present on chain, lifecycle "evaluated", and the stored submission labelled as a deployment probe.
6. Optional API check, paste after the domain: /api/auth/session (mode "public"), /api/tenders (the register as JSON), /api/genlayer (the same chain read).
```

## What the website lets a visitor do

| Task | Where |
| --- | --- |
| Understand the product and its stated limits | `/` |
| Browse, search and filter every registered tender | `/app` |
| Register a tender - the only write open to any visitor | `/app/tenders/new` |
| Open a tender record: identity, deadline, registration time, workspace tabs | `/app/tenders/<id>` |
| Verify the deployed contract's chain identity, code presence and lifecycle | `/app/settings` |
| Read the register, the access mode and the chain status as JSON | `/api/...` |

## Storage note

The workspace runs on a free compute plan, which does not support a persistent
disk. The records service keeps the register in SQLite at `TENDERLENS_DB_PATH`
(`./var/tenderlens.sqlite3` by default), and that file is cleared whenever the
service restarts or redeploys. This is the documented behaviour of the hosting
tier, not a broken integration:

- A tender registered through the form is a real record with a real id, and it
  opens immediately.
- An `/app/tenders/<id>` link stops resolving after a restart. The detail page
  says the record is no longer held rather than blaming the identifier.
- The register can therefore be empty when a steward opens it.

Free instances also stop after a period of inactivity. The first request after
that can take up to a minute while the instance starts; the workbench reports
that it is waking the records service and retries on its own, and the gateway
waits 50 seconds upstream before reporting a timeout.

## Portal fields

**Title**

```
TenderLens — procurement evaluation Intelligent Contract (deployed + evaluated on Studionet)
```

**Description** (fits the 1,000-character field)

```
TenderLens is a source-first procurement review workspace that shows its reasoning. Documents and amendments are versioned; a frozen snapshot turns requirements and evidence excerpts into one canonical, hashed payload that becomes the constructor input of a GenLayer Intelligent Contract on Studionet (61999), so the assessed input cannot be edited afterwards. evaluate() has validators interpret each requirement against those excerpts and return per-requirement status, cited evidence IDs, and every item left unresolved. Deterministic checks verify the snapshot digest and evidence provenance before any model interpretation, and each result carries a disclaimer: evidence interpretation, not legal advice or an award. The workspace reads the chain back and reports chain identity, endpoint pinning, code presence, lifecycle and probe labels. Limits: the deployed instance was evaluated once with a labelled probe payload, so it is a smoke test; the workbench is public, no passphrase required.
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

## Review verification (paste into the form)

```
Verified in production on 2026-10-22. /app opens with no sign-in card and no passphrase field; the tender register loads anonymously and a registered tender opens at /app/tenders/<id> with its stored title, reference and deadline. /api/auth/session returns 200 with mode public; /api/genlayer returns lifecycle evaluated with the probe result visible to anonymous callers. Contract access is pinned to Studionet 61999 and fails closed; privileged writes return 401.
```

## Project links

- Website (required): <https://tenderlens-lilac.vercel.app>
- GitHub: <https://github.com/timmyspurs12/Tenderlens>
- Contract link 1: <https://explorer-studio.genlayer.com/address/0x481d38ede6aC854b13F3b677bc31FeCEB9214126>

Do **not** submit a `/tx/` URL. The portal's contract-link field expects an
address page and its evidence field expects typed sources (GitHub Repository is
the required one). The address page lists both transactions anyway.

## Before submitting

1. The portal requires a complete profile (display name + email).
2. Do not describe the probe result as a tender finding — say "smoke test".
3. If a steward asks for a persistent testnet deployment, that requires a
   Bradbury deployment from a faucet-funded wallet; the public faucet requires
   0.01 ETH on mainnet. Studionet links are what this repository can evidence.
