// Explicit route file for a single tender record.
//
// Vercel's filesystem router binds /api/tenders.js but not nested paths such as
// /api/tenders/<id>, so vercel.json rewrites those requests here with the id (and
// any trailing resource) carried as query parameters. This file validates both,
// rebuilds the canonical upstream path, and hands the request to the gateway,
// which re-checks it against its own allowlist before forwarding.
import handler from './_gateway.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const RESOURCE = /^(?:documents|amendments|requirements|evidence|report|evaluation|evaluation-snapshots)$/i;

function json(res, status, value) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(value));
}

/** Rebuild /api/tenders[/<id>[/<resource>]] from the rewritten query. */
export function rebuildPath(rawUrl) {
  const url = new URL(rawUrl, 'https://placeholder.invalid');
  const id = (url.searchParams.get('id') || '').trim();
  const rest = (url.searchParams.get('rest') || '').trim().replace(/^\/+|\/+$/g, '');
  if (id === '' ) return { path: '/api/tenders', search: '' };
  if (!UUID.test(id)) return null;
  if (rest === '') return { path: `/api/tenders/${id}`, search: '' };
  if (!RESOURCE.test(rest)) return null;
  return { path: `/api/tenders/${id}/${rest}`, search: '' };
}

export default function tenderRecord(req, res) {
  const rebuilt = rebuildPath(req.url || '');
  if (!rebuilt) return json(res, 404, { error: { code: 'not_found', message: 'Route not found.' } });
  req.url = rebuilt.path + rebuilt.search;
  return handler(req, res);
}
