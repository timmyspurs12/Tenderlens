# TenderLens

TenderLens is a source-first procurement intelligence workspace. It presents amendment review, requirement assessment, and evaluation lifecycle as evidence-dependent work: data absent from the connected backend is explicitly unavailable rather than simulated.

## Run locally

- Node.js 20+
- `npm install`
- Set `VITE_API_BASE_URL` to the backend origin (for example, `https://api.example.test`).
- `npm run dev`
- `npm run build`, `npm run typecheck`, `npm test`, and `npm run lint`

Never put private credentials, signing keys, or privileged API tokens in `VITE_*` variables. This is a Vite SPA and does not provide server-side authentication or authorization; deploy behind an authenticated backend or identity-aware gateway before exposing tender records.

## Backend contract expected by this frontend

- `GET /api/tenders` → `Tender[]` or `{ items: Tender[] }`, where every record includes string `id`, `title`, and `reference`; optional `deadline` and `status`.
- `POST /api/tenders` → created tender record including string `id`; request fields `title`, `reference`, optional `deadline`.
- Subsequent tender screens require backend routes for document/version records, requirement and evidence records, amendment comparisons, evaluation lifecycle and audit records, report generation, and authenticated workspace/user operations. The UI marks these capabilities unavailable until those routes are defined and integrated. There is no demo dataset.

`VITE_API_BASE_URL` is the only frontend environment variable currently used. No contract/network configuration or transaction explorer URL is synthesized by the frontend.

## Design system

Shared colors, type roles, and component patterns are centralized in `src/style.css`: warm porcelain canvas, ink text, restrained forest/amber/red semantic colors, 8px-oriented spacing, DM Sans UI, Newsreader editorial headings, and DM Mono metadata. Breakpoints cover tablet/mobile; reduced-motion and print styles are included.
