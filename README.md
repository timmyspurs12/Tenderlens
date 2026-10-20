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

## GenLayer development container

On GitHub, switch to branch `arena/fa6458cf-tenderlens`, choose **Code → Codespaces → Create codespace on branch**, then wait for `.devcontainer/setup.sh` to finish. For an existing Codespace, run **Codespaces: Rebuild Container** from the command palette. The checked-in `.devcontainer/Dockerfile` uses the published Microsoft Python image `mcr.microsoft.com/devcontainers/python:3.12-bookworm` (official `devcontainers/images` lists `3.12-bookworm` as an available published variant); the post-create script asserts Python 3.12 and reports pip and GenLayer versions before succeeding. Node.js is pinned by the devcontainer Node feature to 22.14.0 for the existing Vite regression suite. The actual image pull/build is not performed by this repository setup and must succeed in Codespaces; the setup script fails if its runtime checks do not match.

The project Python environment is isolated in `.venv`. `requirements-genlayer.lock.txt` locks the resolved runtime dependency graph with hashes, while `requirements-genlayer.in` pins the intended roots. The official suite's PEP 517 backend is constrained separately in `requirements-genlayer-build.lock.txt`. Rebuild the locks with `uv pip compile requirements-genlayer.in --python-version 3.12 --generate-hashes --output-file requirements-genlayer.lock.txt` and `uv pip compile requirements-genlayer-build.in --python-version 3.12 --generate-hashes --output-file requirements-genlayer-build.lock.txt`; install with `uv venv --seed --python 3.12 .venv && uv pip sync --build-constraints requirements-genlayer-build.lock.txt --python .venv/bin/python requirements-genlayer.lock.txt`.

**Important upstream compatibility discrepancy:** the official `genlayer-test` GitHub source at signed commit `343e3a358f9e235a93b49c60721ce7676585ff07` declares version 0.29.2 and `genlayer-py>=0.18.0,<0.20.0`. The PyPI wheel also reports 0.29.2 but its published metadata instead requires `genlayer-py>=0.13.0,<0.17.0`. Those constraints cannot be installed together with SDK 0.18.0. To satisfy the requested compatible pair without overriding dependency metadata, this environment pins the official upstream source commit (package version 0.29.2) and SDK 0.18.0; it deliberately does not install the incompatible PyPI wheel. Both alternatives must be rechecked upstream before changing these pins.

The `.devcontainer/setup.sh` performs version/source/runner checks and fails closed if the installed packages differ. The GitHub Actions workflow now has an isolated GenLayer job that invokes the same setup with `--genlayer-only`, then runs a small official Direct Mode storage read/write smoke test. This fixture checks runner startup and basic contract execution only; it is not a procurement assessment, consensus test, or deployed contract. The new job must pass in Actions before this SDK runtime is considered verified.

## Design system

Shared colors, type roles, and component patterns are centralized in `src/style.css`: warm porcelain canvas, ink text, restrained forest/amber/red semantic colors, 8px-oriented spacing, DM Sans UI, Newsreader editorial headings, and DM Mono metadata. Breakpoints cover tablet/mobile; reduced-motion and print styles are included.
