import { Buffer } from 'node:buffer';
import process from 'node:process';
import { Transform } from 'node:stream';
import { issueSession, passwordMatches, setSessionCookie, validSession } from './_session.js';

/**
 * Same-origin gateway for the public TenderLens workspace.
 *
 * Access model: the workbench is open. Anyone may read records and register a
 * tender without a passphrase. Privileged operations — document uploads,
 * evaluation snapshots, submission/reconciliation and deletion — still need the
 * owner session, which remains available when a passphrase and session secret
 * are configured.
 *
 * Safety therefore comes from limits, not from a shared secret:
 *  - a fixed path allowlist, so the privileged backend token can only reach
 *    tender and evaluation-request routes;
 *  - exact-origin checks on every non-safe method;
 *  - per-client rate limits (reads and writes counted separately);
 *  - body caps, with anonymous writes restricted to small JSON bodies;
 *  - the backend bearer token stays server-side and is never forwarded from,
 *    or returned to, the browser.
 *
 * The in-memory limiter is per function instance. It raises the cost of casual
 * abuse; it is not a distributed quota. Put platform-level limits in front of
 * this route before treating it as a hard guarantee.
 */

const MAX_LOGIN_BYTES = 4096;
const MAX_PUBLIC_BODY_BYTES = 128 * 1024;
const MAX_OWNER_BODY_BYTES = 20 * 1024 * 1024;
const ALLOWED_BACKEND_PATH = /^\/api\/(?:tenders(?:\/[^?#]*)?|evaluation-requests\/[0-9a-f-]+)$/i;
// The only write the public workspace UI performs.
const PUBLIC_WRITE_PATH = /^\/api\/tenders$/i;
const ALLOWED_METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'];
const RATE_WINDOW_MS = 60_000;
const RATE_LIMITS = { read: 120, write: 8 };
const MAX_BUCKETS = 5000;

function json(res, status, value) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.end(JSON.stringify(value));
}

function config() {
  const origin = process.env.TENDERLENS_PUBLIC_ORIGIN;
  const backend = process.env.TENDERLENS_BACKEND_URL;
  const token = process.env.TENDERLENS_API_TOKEN;
  // Owner sign-in is optional: without it the workspace stays public read +
  // register, and every privileged route is refused.
  const password = process.env.TENDERLENS_GATEWAY_PASSWORD || '';
  const secret = process.env.TENDERLENS_SESSION_SECRET || '';
  if (!origin || !backend || !token || token.length < 32) return null;
  let publicUrl; let backendUrl;
  try { publicUrl = new URL(origin); backendUrl = new URL(backend); } catch { return null; }
  if (publicUrl.protocol !== 'https:' || publicUrl.origin !== origin) return null;
  if (backendUrl.protocol !== 'https:' || backendUrl.username || backendUrl.password || backendUrl.search || backendUrl.hash || backendUrl.pathname !== '/') return null;
  return { origin, password, secret, backendUrl, token, ownerSignIn: password.length >= 24 && secret.length >= 32 };
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_LOGIN_BYTES) throw new Error('too_large');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw new Error('invalid_json'); }
}

/** Best-effort client identity for rate limiting. */
export function clientKey(headers) {
  const forwarded = headers['x-forwarded-for'];
  if (typeof forwarded === 'string') {
    const first = forwarded.split(',')[0].trim();
    if (first) return first;
  }
  const real = headers['x-real-ip'];
  if (typeof real === 'string' && real.trim()) return real.trim();
  return 'unknown';
}

const buckets = new Map();

export function resetRateLimits() {
  buckets.clear();
}

/**
 * Sliding-window counter per client and class of request. Reads and writes have
 * separate budgets so a busy page load cannot lock a visitor out of registering.
 */
export function takeToken(key, kind, limit, now = Date.now()) {
  if (buckets.size > MAX_BUCKETS) {
    for (const [existing, entry] of buckets) {
      if (now - entry.touched > RATE_WINDOW_MS) buckets.delete(existing);
    }
    if (buckets.size > MAX_BUCKETS) buckets.clear();
  }
  const entry = buckets.get(key) || { read: null, write: null, touched: now };
  entry.touched = now;
  const slot = entry[kind];
  if (!slot || now - slot.start >= RATE_WINDOW_MS) {
    entry[kind] = { start: now, count: 1 };
    buckets.set(key, entry);
    return { allowed: true, remaining: limit - 1, retryAfter: 0 };
  }
  slot.count += 1;
  buckets.set(key, entry);
  if (slot.count > limit) {
    return { allowed: false, remaining: 0, retryAfter: Math.max(1, Math.ceil((slot.start + RATE_WINDOW_MS - now) / 1000)) };
  }
  return { allowed: true, remaining: limit - slot.count, retryAfter: 0 };
}

/** Enforce a byte ceiling on a streamed request body. */
function bodyGuard(req, limit) {
  let seen = 0;
  let exceeded = false;
  const guard = new Transform({
    transform(chunk, _encoding, callback) {
      seen += chunk.length;
      if (seen > limit) {
        exceeded = true;
        callback(new Error('body_too_large'));
        return;
      }
      callback(null, chunk);
    },
  });
  return { stream: req.pipe(guard), exceeded: () => exceeded };
}

export default async function handler(req, res) {
  const cfg = config();
  if (!cfg) return json(res, 503, { error: { code: 'gateway_not_configured', message: 'The workspace gateway is not configured.' } });

  const url = new URL(req.url, cfg.origin);
  const path = url.pathname;
  const method = req.method || 'GET';
  const safeMethod = method === 'GET' || method === 'HEAD';
  const owner = cfg.ownerSignIn && validSession(req.headers.cookie, cfg.secret);

  if (path === '/api/auth/login') {
    if (method !== 'POST') return json(res, 405, { error: { code: 'method_not_allowed', message: 'Use POST.' } });
    if (!cfg.ownerSignIn) return json(res, 503, { error: { code: 'owner_sign_in_disabled', message: 'Owner sign-in is not configured for this build.' } });
    if (req.headers.origin !== cfg.origin) return json(res, 403, { error: { code: 'origin_rejected', message: 'Request origin was not accepted.' } });
    let body;
    try { body = await readJson(req); } catch { return json(res, 400, { error: { code: 'invalid_request', message: 'A small JSON login body is required.' } }); }
    if (!body || Object.keys(body).length !== 1 || !passwordMatches(body.password, cfg.password)) return json(res, 401, { error: { code: 'unauthorized', message: 'The workspace passphrase was not accepted.' } });
    setSessionCookie(res, issueSession(cfg.secret));
    return json(res, 200, { authenticated: true, mode: 'owner' });
  }
  if (path === '/api/auth/session') {
    if (!safeMethod) return json(res, 405, { error: { code: 'method_not_allowed', message: 'Use GET.' } });
    // No sign-in is required to use the workspace, so this answers with the
    // current access level instead of refusing anonymous visitors.
    return json(res, 200, {
      authenticated: true,
      mode: owner ? 'owner' : 'public',
      owner_sign_in: cfg.ownerSignIn,
      can_read: true,
      can_register: true,
      can_write_privileged: owner,
    });
  }
  if (path === '/api/auth/logout') {
    if (method !== 'POST') return json(res, 405, { error: { code: 'method_not_allowed', message: 'Use POST.' } });
    setSessionCookie(res, '', 0);
    return json(res, 200, { authenticated: true, mode: 'public' });
  }

  // Reject encoded separators and dot segments outright: the allowlist is
  // matched on the raw pathname, so any path whose meaning could change after
  // upstream decoding must never be forwarded.
  const ambiguous = /%(?:2e|2f|5c)/i.test(path) || path.includes('\\');
  if (ambiguous || !ALLOWED_BACKEND_PATH.test(path) || !ALLOWED_METHODS.includes(method)) {
    return json(res, 404, { error: { code: 'not_found', message: 'Route not found.' } });
  }

  if (!safeMethod && req.headers.origin !== cfg.origin) {
    return json(res, 403, { error: { code: 'origin_rejected', message: 'Request origin was not accepted.' } });
  }

  const publicWrite = !safeMethod && PUBLIC_WRITE_PATH.test(path);
  if (!safeMethod && !owner && !publicWrite) {
    return json(res, 401, {
      error: {
        code: 'owner_session_required',
        message: 'Reading records and registering tenders is open; this operation needs the workspace owner session.',
      },
    });
  }

  const kind = safeMethod ? 'read' : 'write';
  const decision = takeToken(clientKey(req.headers), kind, RATE_LIMITS[kind]);
  if (!decision.allowed) {
    res.setHeader('Retry-After', String(decision.retryAfter));
    return json(res, 429, {
      error: { code: 'rate_limited', message: `Too many ${kind} requests. Retry in ${decision.retryAfter}s.` },
    });
  }

  const maxBytes = owner ? MAX_OWNER_BODY_BYTES : MAX_PUBLIC_BODY_BYTES;
  const hasBody = !safeMethod && method !== 'HEAD';
  if (hasBody) {
    const declared = Number.parseInt(req.headers['content-length'] || '0', 10);
    if (Number.isFinite(declared) && declared > maxBytes) {
      return json(res, 413, { error: { code: 'body_too_large', message: `The request body exceeds ${maxBytes} bytes.` } });
    }
    // Anonymous writes must stay small and structured; the gateway is not a
    // general-purpose file relay for visitors without the owner session.
    if (!owner) {
      const contentType = String(req.headers['content-type'] || '').toLowerCase();
      if (!contentType.startsWith('application/json')) {
        return json(res, 415, { error: { code: 'unsupported_media_type', message: 'Anonymous writes must use application/json.' } });
      }
    }
  }

  const headers = new globalThis.Headers({ Accept: req.headers.accept || 'application/json', Authorization: `Bearer ${cfg.token}` });
  for (const key of ['content-type', 'x-filename', 'x-document-group', 'x-supersedes-document-id']) {
    if (typeof req.headers[key] === 'string') headers.set(key, req.headers[key]);
  }

  const guarded = hasBody ? bodyGuard(req, maxBytes) : null;
  try {
    const upstream = await fetch(new URL(`${path}${url.search}`, cfg.backendUrl), {
      method,
      headers,
      body: guarded ? guarded.stream : undefined,
      ...(guarded ? { duplex: 'half' } : {}),
      redirect: 'manual',
      signal: globalThis.AbortSignal.timeout(25_000),
    });
    if (upstream.status >= 300 && upstream.status < 400) return json(res, 502, { error: { code: 'upstream_redirect_rejected', message: 'The records service returned an unexpected redirect.' } });
    res.statusCode = upstream.status;
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('X-RateLimit-Remaining', String(decision.remaining));
    const contentType = upstream.headers.get('content-type');
    if (contentType) res.setHeader('Content-Type', contentType);
    const body = Buffer.from(await upstream.arrayBuffer());
    res.end(body);
  } catch {
    if (guarded && guarded.exceeded()) {
      return json(res, 413, { error: { code: 'body_too_large', message: `The request body exceeds ${maxBytes} bytes.` } });
    }
    return json(res, 502, { error: { code: 'upstream_unavailable', message: 'The workspace records service could not be reached.' } });
  }
}

export const __test = { config, issueSession, validSession, passwordMatches, ALLOWED_BACKEND_PATH, PUBLIC_WRITE_PATH, RATE_LIMITS };
