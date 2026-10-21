# Tender records: current data flow and safe integration boundary

## Confirmed backend contract

`GET /api/tenders` is implemented by `backend/app.py`. It authenticates all `/api/*` requests before routing and requires `Authorization: Bearer <TENDERLENS_API_TOKEN>`. A missing or bad token receives HTTP 401 and:

```json
{"error":{"code":"unauthorized","message":"Valid bearer authentication is required."}}
```

With valid credentials, the endpoint selects `id`, `title`, `reference`, `deadline`, and `created_at` only where `workspace` equals the server-configured `TENDERLENS_WORKSPACE`, ordered newest first. It returns HTTP 200 with `{ "items": [...] }`; an empty workspace is `{ "items": [] }`. These records are SQLite-backed and are never seeded with demo procurement data. The list API tests exercise both a new/empty store and two isolated workspace instances sharing the same test database.

The frontend contract accepts either that `{ items: Tender[] }` envelope or a top-level array. Each row must contain string `id`, `title`, and `reference`; optional display fields are not used as authentication or provenance claims.

## Authentication and browser boundary

The SPA has no user login, session issuance, or membership check. The API's static service bearer token identifies one configured workspace, so placing it in browser code would grant anyone who extracts it access to that workspace. The frontend therefore sends **no Authorization header**. It includes session cookies so a future identity-aware gateway can use an HttpOnly user session, but no such session provider or gateway exists in this repository.

The safe intended flow is:

`browser (HttpOnly user session) → same-origin BFF / trusted identity gateway → authenticate user + verify workspace membership → add server-only TENDERLENS_API_TOKEN and server-selected TENDERLENS_WORKSPACE → backend GET /api/tenders → validated JSON response → browser`

A same-origin BFF is preferred. A cross-origin gateway is acceptable only if it authenticates and authorizes users itself and sets credentialed CORS for the exact frontend origin. The current backend does not emit CORS headers: `/api/*` `OPTIONS` responds 204 without `Access-Control-Allow-Origin`; do not call it cross-origin from the browser and do not relax it to wildcard access.

The Vercel project inspected for this repository is a Vite static frontend project. Its configured environment-variable list is empty; no `VITE_API_BASE_URL` is configured. No backend project or public backend origin was discoverable in the connected Vercel account or this checkout. The repository has no BFF/serverless API route or identity-provider integration. We therefore cannot verify that a backend is deployed, inspect its persistent database, establish whether it contains records, or claim any live API response. Do not interpret the local temporary SQLite test database as production data.

## Frontend behavior

- `VITE_API_BASE_URL` is optional at build time and must be the public origin of the authenticated user-facing gateway, not a secret and not the raw privileged API. Trailing slashes are normalized.
- Browser calls send `credentials: include` for user-session cookies, never a privileged service token. The client distinguishes missing configuration, 401/403 authentication, HTTP errors, network failures, timeouts, malformed JSON, and malformed tender schemas.
- When the authorized API returns an empty `items` collection, the UI explicitly says no tenders are registered and offers **Register a tender**. It does not insert illustrative or fabricated records.
- Returned tender rows show the API-provided title/reference and open that tender's workspace. Search-empty is distinguished from an empty workspace.
- GenLayer evaluation remains labeled unavailable; records visibility does not imply a live contract or an on-chain evaluation.

## Configuration required when infrastructure is ready

1. Deploy an identity-aware BFF/gateway that supports user sign-in, HttpOnly secure sessions, workspace membership enforcement, server-selected workspace IDs, rate limits, CSRF protection for writes, and request logging without credential leakage. Configure its server-only `TENDERLENS_API_TOKEN` and workspace routing. Do not place either in Vite variables. The current API token is single-workspace; do not share it across tenants.
2. Deploy the backend with TLS and durable encrypted database/blob storage. Set backend-only `TENDERLENS_API_TOKEN` (random, at least 32 characters), `TENDERLENS_WORKSPACE` (trusted workspace identity), and appropriate `TENDERLENS_DB_PATH` / `TENDERLENS_BLOB_DIR`; optionally set `TENDERLENS_SNAPSHOT_HMAC_KEY` (stable, at least 32 characters). The backend's built-in WSGI server is development-only.
3. Only after the gateway origin is known and security-reviewed, configure Vercel project environment variable `VITE_API_BASE_URL` with that public gateway origin for the intended Preview and Production environments, then request authorization before changing production settings or triggering a production deployment. No value is currently set, and no backend URL is inferred here. Keep all service credentials out of `VITE_*` variables.
4. For a same-origin BFF, the BFF must be deployed at the frontend origin and route `/api/tenders`; the static Vercel rewrite alone is not a BFF. For a separate origin, configure exact-origin credentialed CORS on the gateway and test preflight, cookie security, unauthorized responses, and workspace isolation. The raw backend currently has no CORS support.

## Verification boundary

Backend unit tests verify the authenticated response schema, empty collection, unauthorized access, workspace isolation, and the current absence of permissive CORS headers. Frontend tests verify response parsing, status/error handling, timeout behavior, no browser bearer token, successful record rendering, and empty-state rendering. These tests use synthetic test fixtures only. Until a gateway, backend URL, user identity provider, production service secret, and database access are supplied/configured, deployed end-to-end retrieval and any production record count remain unverified.
