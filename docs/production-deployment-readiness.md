# Production deployment readiness

This document records what can be deployed safely from the current repository and what still requires an operator decision or an authorized hosting/network environment. It is not evidence of a live deployment.

## Current application shape

- **Frontend:** React + TypeScript + Vite, built by `npm ci && npm run build` into `dist/`. `vercel.json` configures that static build and SPA fallback.
- **API:** Python 3.12 standard-library WSGI application in `backend/app.py`, with SQLite and content-addressed files on the local filesystem. `/healthz` is a public, minimal readiness check that runs `SELECT 1`; it reports no local paths or secrets.
- **Persistent data:** `TENDERLENS_DB_PATH` and `TENDERLENS_BLOB_DIR` must point to persistent storage on a backend host. Do not run a production instance on an ephemeral filesystem. SQLite and file storage are currently single-service/local-storage architecture; assess a managed database and object storage before multi-instance or higher-volume public service.
- **GenLayer:** the backend has fail-closed driver gates, but the default application factory injects no live submitter or target-specific identity/runtime/funding verifiers. Deployment and evaluation broadcast are not operationally enabled.

## Public-service security blockers

The API authenticates every application request using the server-side `TENDERLENS_API_TOKEN`. The React SPA does not implement a login/session flow and its API client sends no bearer authorization. Never put that API token in a `VITE_*` variable or browser bundle. The current API also has no configurable origin allowlist or application-level rate limiter. Before exposing protected tender records, choose and implement an authenticated BFF/identity-aware gateway, use same-origin routing, and configure host-level rate limits and abuse controls. Do not make the backend token public to make the UI appear connected.

No identity provider, production domain, backend host, database service, object-storage service, or hosting account has been selected or connected. Vercel is the intended static frontend option, but its account integration is not connected in this session. Railway is not available as a connected-app integration; a host could still be configured by its owner through its own dashboard or a future approved deployment integration.

## Vercel frontend configuration

The checked-in `vercel.json` uses the existing frontend scripts and `dist/` output. Configure the Vercel project to build this repository's active branch. Only use public frontend values such as `VITE_API_BASE_URL`; Vite embeds `VITE_*` settings into browser JavaScript. A static site build does not configure or secure the backend and is not a working records application until the authenticated API path is established.

## Backend configuration names

Set backend values only in the backend host's encrypted environment/secret settings. Never put values in source, build-time frontend settings, commits, logs, or chat.

- Required service authentication: `TENDERLENS_API_TOKEN` (at least 32 characters).
- Workspace and persistence: `TENDERLENS_WORKSPACE`, `TENDERLENS_DB_PATH`, `TENDERLENS_BLOB_DIR`.
- Snapshot authentication: `TENDERLENS_SNAPSHOT_HMAC_KEY` (keep stable; set a separate value for production rather than relying on the API-token fallback).
- Runtime binding: host `PORT`; the local development entrypoint also reads `TENDERLENS_HOST` and `TENDERLENS_PORT`.
- GenLayer server-side settings are listed in `backend/genlayer_sdk_driver.py` and `scripts/genlayer_preflight.py`. Do not configure the private-key variable until an authorized wallet custody/signing method and production host are chosen. A broadcast opt-in flag is not signer authorization or a substitute for target/runtime/funding verifiers.

## GenLayer deployment gate

The official current documentation describes Studionet at `https://studio.genlayer.com/api` (chain ID 61999) as a stable hosted development network with a built-in faucet and Studio explorer. Bradbury is the production-like testnet; its docs warn that chain ID 4221 is shared with other network deployments and require verifying the Phase 2 genesis hash and AddressManager. These documents establish published network configuration, not live endpoint identity.

The contract now pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`, the exact runtime identifier in current official first-contract documentation. The previous Direct Mode suite (v0.29.2) failed to load even its minimal storage fixture with this runtime; the repository is being moved to the official v0.30.0-rc.2 testing-suite source and genlayer-py v0.19.0-rc.2, which add support for current GenVM manager runner bundles. A passing Direct Mode run will still prove only behavior in that test harness; it will not establish that current Studionet or Bradbury validators accept this exact runtime/contract. Before deployment, an authorized environment must verify target identity and supported runner, then execute a target-compatible compilation/contract test. This workspace's outbound policy does not permit requests to the GenLayer RPC hosts, so those live checks cannot be performed here.

Current official fee guidance treats fees as measured/profiled requirements and requires live estimation against current network price/cap data. The pinned genlayer-py v0.19.0-rc.2 SDK exposes fee-estimation helpers, but this workspace cannot reach the GenLayer RPC hosts and has not obtained a current quote. No fee amount or reserve has been inferred. Use the official estimator on the selected network for deployment and the synthetic evaluation interaction, record its measured profile and token-denominated maximum, verify wallet balance, and show the total before requesting spending approval. The backend driver intentionally requires an injected funding-verification gate and does not treat an SDK estimate alone as authorization.

The approved workflow is: read-only target identity and runner qualification; secure wallet creation and public-address review; exact fee quote and user approval; user funding through the official testnet route; balance verification; one deployment; finalized receipt, transaction hash/address verification using official network/explorer evidence; then a synthetic-data read/write check. Do not proceed past a gate if evidence is unavailable.

## Local verification performed

- Python backend, adapter, preflight and SDK-driver tests passed locally on Python 3.11.2 after the SDK 0.19 API-shape update; this is not the locked Python 3.12 environment.
- The Vite production build, TypeScript check, frontend tests and lint passed locally after the current frontend/API configuration changes.
- The new locked Python 3.12 environment and Direct Mode tests have not yet run; local Python 3.12 download was blocked by a TLS handshake error. The merged baseline's post-merge GitHub Actions run passed both jobs, but it did not validate these new changes. These results are not deployment evidence.
