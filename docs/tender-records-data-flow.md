# Tender records: current data flow and safe integration boundary

## Confirmed backend contract

`GET /api/tenders` is implemented by `backend/app.py`. It authenticates all `/api/*` requests before routing and requires `Authorization: Bearer <TENDERLENS_API_TOKEN>`. A missing or bad token receives HTTP 401 and:

```json
{"error":{"code":"unauthorized","message":"Valid bearer authentication is required."}}
```

With valid credentials, the endpoint selects `id`, `title`, `reference`, `deadline`, and `created_at` only where `workspace` equals the server-configured `TENDERLENS_WORKSPACE`, ordered newest first. It returns HTTP 200 with `{ "items": [...] }`; an empty workspace is `{ "items": [] }`. These records are SQLite-backed and are never seeded with demo procurement data. The list API tests exercise both a new/empty store and two isolated workspace instances sharing the same test database.

The frontend contract accepts either that `{ items: Tender[] }` envelope or a top-level array. Each row must contain string `id`, `title`, and `reference`; optional display fields are not used as authentication or provenance claims.

## Authentication and browser boundary

The SPA's browser code never receives the API bearer token. The same-origin Vercel BFF now serves the workspace publicly — anonymous reads and tender registration — with an optional single-owner passphrase login (signed HttpOnly cookie) that unlocks privileged writes; the Render API still uses its server-side bearer token. The BFF fixes the workspace by proxying only to the configured backend, whose `TENDERLENS_WORKSPACE` determines the scope. Anonymous access is limited by a fixed path allowlist, an exact-origin check on writes, JSON-only small bodies, and per-client rate limits, so the public surface cannot reach arbitrary backend routes or forward the bearer token. There is no per-user membership or multi-tenant identity; the owner session (when configured) is a single shared credential, not enterprise authentication.

The safe intended flow is:

`browser (HttpOnly signed owner session) → same-origin Vercel BFF → fixed Render backend + server-only TENDERLENS_API_TOKEN → backend-selected TENDERLENS_WORKSPACE → validated JSON response → browser`

The BFF enforces exact `Origin` on all writes and uses `SameSite=Strict`, `Secure`, `HttpOnly` cookies. It does not add CORS to the raw backend; browser calls remain same-origin. Its passphrase gate is single-owner only and has no account recovery or per-user membership. For multiple users or real multi-tenant procurement data, replace it with a managed identity provider plus explicit workspace membership and add robust rate limiting/operational monitoring before production use.

The Vercel project was previously inspected as a Vite frontend with no configured environment variables. This repository now adds a Vercel Node function BFF, but it is not live until the five server-side variables above are configured and the project is redeployed. The user verified the Render health, unauthorized response, and authorized empty `items` response; that does not establish persistent storage or non-empty records. Do not interpret local temporary SQLite test data as production data.

## Frontend behavior

- Production builds default to same-origin `/api` routes served by the Vercel BFF. For local UI-only work, leave `VITE_API_BASE_URL` unset and the workspace reports the records connection as unconfigured. If using a separate authenticated gateway, `VITE_API_BASE_URL` may point to that public origin, never to a secret or the raw privileged API.
- Browser calls send `credentials: include` for user-session cookies, never a privileged service token. The client distinguishes missing configuration, 401/403 authentication, HTTP errors, network failures, timeouts, malformed JSON, and malformed tender schemas.
- When the authorized API returns an empty `items` collection, the UI explicitly says no tenders are registered and offers **Register a tender**. It does not insert illustrative or fabricated records.
- Returned tender rows show the API-provided title/reference and open that tender's workspace. Search-empty is distinguished from an empty workspace.
- GenLayer evaluation remains labeled unavailable; records visibility does not imply a live contract or an on-chain evaluation.

## Configuration required when infrastructure is ready

1. The repository now contains a Vercel same-origin BFF in `api/_gateway.js`, bound by explicit route files (`api/tenders.js`, `api/auth/session.js`, `api/auth/login.js`, `api/auth/logout.js`) with a public workspace, an optional owner session and a fixed upstream workspace. The explicit files exist because this project's Vercel filesystem router did not bind the bracketed `api/[...path].js` catch-all for nested paths such as `/api/auth/session` (verified in production: `/api/tenders` reached the function while `/api/auth/session` returned the platform 404 page); `api/[...path].js` is retained as a fallback binding. Configure these **server-side Vercel Environment Variables** for Preview/Production as appropriate: `TENDERLENS_PUBLIC_ORIGIN` (exact HTTPS origin, no trailing slash), `TENDERLENS_BACKEND_URL` (`https://tenderlens-bv66.onrender.com` for the user-verified service), and `TENDERLENS_API_TOKEN` (the Render service token; enter it only in the Vercel secret field, never in chat or a `VITE_*` variable). `TENDERLENS_GATEWAY_PASSWORD` (at least 24 characters) and `TENDERLENS_SESSION_SECRET` (at least 32 random characters, stable across deploys) are **optional**: with them, the owner can sign in and perform privileged writes; without them the workspace is public read + register and every privileged route returns 401. Then redeploy Vercel and verify `/api/auth/session` returns `mode` `public` or `owner`, `/api/tenders` returns records without a passphrase, and a privileged write such as `POST /api/tenders/{id}/evaluation-snapshots` is refused for anonymous callers. Every signed-in owner is mapped to the single workspace selected by Render's `TENDERLENS_WORKSPACE`; this is not multi-user identity, has no account recovery, and is not enterprise-grade authentication. Vercel function upload-size limits are lower than the backend's 20 MiB limit; large document uploads need a separate upload/storage design. Do not share this single-workspace token across tenants or put it in Vite variables.
2. Deploy the backend with TLS and durable encrypted database/blob storage. Set backend-only `TENDERLENS_API_TOKEN` (random, at least 32 characters), `TENDERLENS_WORKSPACE` (trusted workspace identity), and appropriate `TENDERLENS_DB_PATH` / `TENDERLENS_BLOB_DIR`; optionally set `TENDERLENS_SNAPSHOT_HMAC_KEY` (stable, at least 32 characters). The backend's built-in WSGI server is development-only.
3. Production's frontend client uses same-origin API paths by default; do not set `VITE_API_BASE_URL` for the same-origin BFF. If using a separately hosted gateway, set only its public origin in `VITE_API_BASE_URL` and apply exact-origin CORS/cookie protections there. Keep all service credentials out of `VITE_*` variables.
4. Add a managed identity provider, account recovery, robust rate limiting, monitoring, and explicit membership enforcement before treating the prototype owner gate as production authentication. The current BFF supports one owner credential and one fixed workspace; it is not a general multi-tenant gateway.

## Verification boundary

Backend unit tests verify the authenticated response schema, empty collection, unauthorized access, workspace isolation, and the current absence of permissive CORS headers. Frontend tests verify response parsing, status/error handling, timeout behavior, no browser bearer token, successful record rendering, and empty-state rendering. These tests use synthetic test fixtures only. Until a gateway, backend URL, user identity provider, production service secret, and database access are supplied/configured, deployed end-to-end retrieval and any production record count remain unverified.
