// Vercel's filesystem router does not reliably bind a bracketed catch-all
// nested under /api (see README "Live deployment"). Every first-class route
// therefore has an explicit function file; this catch-all remains as a
// fallback for any additional nested records path.
export { default } from './_gateway.js';
