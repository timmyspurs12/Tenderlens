import { Buffer } from 'node:buffer';
import process from 'node:process';
import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

const COOKIE = '__Host-tenderlens_session';
const SESSION_SECONDS = 8 * 60 * 60;
const MAX_LOGIN_BYTES = 4096;
const ALLOWED_BACKEND_PATH = /^\/api\/(?:tenders(?:\/[^?#]*)?|evaluation-requests\/[0-9a-f-]+)$/i;

function json(res, status, value) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.end(JSON.stringify(value));
}

function config() {
  const origin = process.env.TENDERLENS_PUBLIC_ORIGIN;
  const password = process.env.TENDERLENS_GATEWAY_PASSWORD;
  const secret = process.env.TENDERLENS_SESSION_SECRET;
  const backend = process.env.TENDERLENS_BACKEND_URL;
  const token = process.env.TENDERLENS_API_TOKEN;
  if (!origin || !password || !secret || !backend || !token || secret.length < 32 || password.length < 24 || token.length < 32) return null;
  let publicUrl; let backendUrl;
  try { publicUrl = new URL(origin); backendUrl = new URL(backend); } catch { return null; }
  if (publicUrl.protocol !== 'https:' || publicUrl.origin !== origin || backendUrl.protocol !== 'https:' || backendUrl.username || backendUrl.password || backendUrl.search || backendUrl.hash || backendUrl.pathname !== '/') return null;
  return { origin, password, secret, backendUrl, token };
}

function sign(value, secret) {
  return createHmac('sha256', secret).update(value).digest('base64url');
}

function issueSession(secret, now = Date.now()) {
  const payload = Buffer.from(JSON.stringify({ exp: Math.floor(now / 1000) + SESSION_SECONDS, nonce: cryptoRandom() })).toString('base64url');
  return `${payload}.${sign(payload, secret)}`;
}

function cryptoRandom() {
  return randomBytes(24).toString('hex');
}

function validSession(cookieHeader, secret, now = Date.now()) {
  const cookie = (cookieHeader || '').split(';').map((item) => item.trim()).find((item) => item.startsWith(`${COOKIE}=`));
  if (!cookie) return false;
  const value = cookie.slice(COOKIE.length + 1);
  const [payload, signature, extra] = value.split('.');
  if (!payload || !signature || extra) return false;
  const expected = sign(payload, secret);
  const actualBytes = Buffer.from(signature);
  const expectedBytes = Buffer.from(expected);
  if (actualBytes.length !== expectedBytes.length || !timingSafeEqual(actualBytes, expectedBytes)) return false;
  try {
    const data = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
    return Number.isInteger(data.exp) && data.exp > Math.floor(now / 1000) && typeof data.nonce === 'string';
  } catch { return false; }
}

function setSessionCookie(res, value, maxAge = SESSION_SECONDS) {
  res.setHeader('Set-Cookie', `${COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`);
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

function passwordMatches(candidate, expected) {
  if (typeof candidate !== 'string' || candidate.length > 1024) return false;
  const left = createHmac('sha256', 'tenderlens-login-compare').update(candidate).digest();
  const right = createHmac('sha256', 'tenderlens-login-compare').update(expected).digest();
  return timingSafeEqual(left, right);
}

export default async function handler(req, res) {
  const cfg = config();
  if (!cfg) return json(res, 503, { error: { code: 'gateway_not_configured', message: 'Workspace sign-in is not configured.' } });
  const url = new URL(req.url, cfg.origin);
  const path = url.pathname;
  const safeMethod = req.method === 'GET' || req.method === 'HEAD';
  if (!safeMethod && req.headers.origin !== cfg.origin) return json(res, 403, { error: { code: 'origin_rejected', message: 'Request origin was not accepted.' } });

  if (path === '/api/auth/login') {
    if (req.method !== 'POST') return json(res, 405, { error: { code: 'method_not_allowed', message: 'Use POST.' } });
    let body;
    try { body = await readJson(req); } catch { return json(res, 400, { error: { code: 'invalid_request', message: 'A small JSON login body is required.' } }); }
    if (!body || Object.keys(body).length !== 1 || !passwordMatches(body.password, cfg.password)) return json(res, 401, { error: { code: 'unauthorized', message: 'The workspace passphrase was not accepted.' } });
    setSessionCookie(res, issueSession(cfg.secret));
    return json(res, 200, { authenticated: true });
  }
  if (path === '/api/auth/session') {
    if (!safeMethod) return json(res, 405, { error: { code: 'method_not_allowed', message: 'Use GET.' } });
    if (!validSession(req.headers.cookie, cfg.secret)) return json(res, 401, { error: { code: 'unauthorized', message: 'Workspace sign-in is required.' } });
    return json(res, 200, { authenticated: true });
  }
  if (path === '/api/auth/logout') {
    if (req.method !== 'POST') return json(res, 405, { error: { code: 'method_not_allowed', message: 'Use POST.' } });
    setSessionCookie(res, '', 0);
    return json(res, 200, { authenticated: false });
  }

  if (!ALLOWED_BACKEND_PATH.test(path) || !['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method)) return json(res, 404, { error: { code: 'not_found', message: 'Route not found.' } });
  if (!validSession(req.headers.cookie, cfg.secret)) return json(res, 401, { error: { code: 'unauthorized', message: 'Workspace sign-in is required.' } });

  const headers = new globalThis.Headers({ Accept: req.headers.accept || 'application/json', Authorization: `Bearer ${cfg.token}` });
  for (const key of ['content-type', 'x-filename', 'x-document-group', 'x-supersedes-document-id']) {
    if (typeof req.headers[key] === 'string') headers.set(key, req.headers[key]);
  }
  const hasBody = !['GET', 'HEAD'].includes(req.method);
  try {
    const upstream = await fetch(new URL(`${path}${url.search}`, cfg.backendUrl), {
      method: req.method,
      headers,
      body: hasBody ? req : undefined,
      ...(hasBody ? { duplex: 'half' } : {}),
      redirect: 'manual',
      signal: globalThis.AbortSignal.timeout(25_000),
    });
    if (upstream.status >= 300 && upstream.status < 400) return json(res, 502, { error: { code: 'upstream_redirect_rejected', message: 'The records service returned an unexpected redirect.' } });
    res.statusCode = upstream.status;
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    const contentType = upstream.headers.get('content-type');
    if (contentType) res.setHeader('Content-Type', contentType);
    const body = Buffer.from(await upstream.arrayBuffer());
    res.end(body);
  } catch {
    return json(res, 502, { error: { code: 'upstream_unavailable', message: 'The workspace records service could not be reached.' } });
  }
}

export const __test = { config, issueSession, validSession, passwordMatches, ALLOWED_BACKEND_PATH };
