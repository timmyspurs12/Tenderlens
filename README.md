# TenderLens

TenderLens is a source-first procurement intelligence workspace. It presents amendment review, requirement assessment, and evaluation lifecycle as evidence-dependent work: data absent from the connected backend is explicitly unavailable rather than simulated.

## Run locally

- Node.js 20+
- `npm install`
- Set `VITE_API_BASE_URL` to the backend origin (for example, `https://api.example.test`).
- `npm run dev`
- `npm run build`, `npm run typecheck`, `npm test`, and `npm run lint`

Never put private credentials, signing keys, or privileged API tokens in `VITE_*` variables. This is a Vite SPA and does not provide server-side authentication or authorization; deploy behind an authenticated backend or identity-aware gateway before exposing tender records.

## Backend

A small standard-library API and SQLite/content-addressed document store is implemented under `backend/`. It supports authenticated tender registration, immutable uploads, server-calculated SHA-256, explicit document links/amendment annotations, requirement registration, and byte-range evidence provenance for plain-text files. The API requires a workspace-scoped bearer credential; the browser must never receive that privileged token. Therefore the current SPA is not yet connected directly: deploy a same-origin authenticated BFF/identity-aware gateway before wiring protected records into browser views.

Set `TENDERLENS_API_TOKEN` to a random secret of at least 32 characters, `TENDERLENS_WORKSPACE` to the isolated workspace identifier, and optionally `TENDERLENS_DB_PATH` / `TENDERLENS_BLOB_DIR` (defaults under `./var/`). Run `python backend/app.py` for local development only; the built-in WSGI server is not suitable for production. Use TLS, an authenticated gateway, and durable encrypted storage for deployments. Endpoint schemas, error codes, route authorization, and unimplemented transaction lifecycle states are documented in [`docs/api-contracts.md`](docs/api-contracts.md).

No GenLayer SDK, tested Intelligent Contract, evaluation submission API, network credentials, or deployment configuration is present. Do not submit or assert an on-chain evaluation. Amendment annotations are user-provided, and semantic comparison is explicitly unavailable. PDFs and DOCX files are retained and hashed; text passage extraction is only supported for plain text. No demo records are generated.

`VITE_API_BASE_URL` is the frontend's optional service origin. No private key or privileged API token belongs in a `VITE_*` variable.

## Design system

Shared colors, type roles, and component patterns are centralized in `src/style.css`: warm porcelain canvas, ink text, restrained forest/amber/red semantic colors, 8px-oriented spacing, DM Sans UI, Newsreader editorial headings, and DM Mono metadata. Breakpoints cover tablet/mobile; reduced-motion and print styles are included.
