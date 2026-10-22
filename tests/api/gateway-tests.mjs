import { Buffer } from 'node:buffer';
import process from 'node:process';
import { afterEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import handler, { __test } from '../../api/_gateway.js';
import { resetRateLimits } from '../../api/_gateway.js';

const envKeys = ['TENDERLENS_PUBLIC_ORIGIN', 'TENDERLENS_GATEWAY_PASSWORD', 'TENDERLENS_SESSION_SECRET', 'TENDERLENS_BACKEND_URL', 'TENDERLENS_API_TOKEN'];
const original = Object.fromEntries(envKeys.map((key) => [key, process.env[key]]));
const origin = 'https://tenderlens.example';
const secret = 's'.repeat(48);
const password = 'owner-passphrase-with-at-least-24-chars';

function configure() {
  process.env.TENDERLENS_PUBLIC_ORIGIN = origin;
  process.env.TENDERLENS_GATEWAY_PASSWORD = password;
  process.env.TENDERLENS_SESSION_SECRET = secret;
  process.env.TENDERLENS_BACKEND_URL = 'https://tenderlens-bv66.onrender.com';
  process.env.TENDERLENS_API_TOKEN = 't'.repeat(48);
}

/** A build with the backend configured but no owner passphrase. */
function configurePublicOnly() {
  configure();
  delete process.env.TENDERLENS_GATEWAY_PASSWORD;
  delete process.env.TENDERLENS_SESSION_SECRET;
}

function response() {
  const headers = {};
  return { headers, statusCode: 200, body: undefined, setHeader(k, v) { headers[k.toLowerCase()] = v; }, end(body) { this.body = body; } };
}

function request(path, { method = 'GET', headers = {}, body = '' } = {}) {
  const stream = {
    url: path,
    method,
    headers,
    async *[Symbol.asyncIterator]() { if (body) yield Buffer.from(body); },
    pipe(target) { (async () => { try { for await (const chunk of this) target.write(chunk); target.end(); } catch { target.destroy(); } })(); return target; },
  };
  return stream;
}

function cookie() { return `__Host-tenderlens_session=${__test.issueSession(secret)}; Path=/`; }
const jsonHeaders = (extra = {}) => ({ origin, 'content-type': 'application/json', ...extra });

afterEach(() => {
  for (const key of envKeys) {
    if (original[key] === undefined) delete process.env[key];
    else process.env[key] = original[key];
  }
  globalThis.fetch = originalFetch;
  resetRateLimits();
});
const originalFetch = globalThis.fetch;

describe('Public workspace access', () => {
  it('refuses encoded path separators and dot segments before forwarding', async () => {
    configure();
    const calls = [];
    globalThis.fetch = async (url) => { calls.push(String(url)); return new globalThis.Response('{}', { status: 200 }); };
    for (const path of ['/api/tenders/%2e%2e%2fadmin', '/api/tenders/..%2fadmin', '/api/tenders/..%5cadmin', '/api/admin', '/api/tenders/x/../../admin']) {
      const res = response();
      await handler(request(path), res);
      assert.equal(res.statusCode, 404, path);
    }
    assert.deepEqual(calls, []);
  });

  it('fails closed when the backend configuration is missing', async () => {
    for (const key of envKeys) delete process.env[key];
    const res = response();
    await handler(request('/api/auth/session'), res);
    assert.equal(res.statusCode, 503);
    assert.equal(JSON.parse(res.body).error.code, 'gateway_not_configured');
  });

  it('reports public access without a passphrase', async () => {
    configurePublicOnly();
    const res = response();
    await handler(request('/api/auth/session'), res);
    assert.equal(res.statusCode, 200);
    const body = JSON.parse(res.body);
    assert.equal(body.mode, 'public');
    assert.equal(body.owner_sign_in, false);
    assert.equal(body.can_read, true);
    assert.equal(body.can_register, true);
    assert.equal(body.can_write_privileged, false);
  });

  it('serves records anonymously with the server-only bearer token', async () => {
    configurePublicOnly();
    let captured;
    globalThis.fetch = async (url, options) => {
      captured = { url: String(url), options };
      return new globalThis.Response(JSON.stringify({ items: [] }), { status: 200, headers: { 'content-type': 'application/json' } });
    };
    const res = response();
    await handler(request('/api/tenders?sort=recent', { headers: { authorization: 'Bearer attacker', cookie: 'x=1' } }), res);
    assert.equal(res.statusCode, 200);
    assert.deepEqual(JSON.parse(res.body), { items: [] });
    assert.equal(captured.url, 'https://tenderlens-bv66.onrender.com/api/tenders?sort=recent');
    assert.equal(captured.options.headers.get('authorization'), `Bearer ${'t'.repeat(48)}`);
    assert.equal(captured.options.headers.get('cookie'), null);
  });

  it('lets anyone register a tender, and still refuses privileged writes', async () => {
    configurePublicOnly();
    globalThis.fetch = async () => new globalThis.Response(JSON.stringify({ id: 'x' }), { status: 201, headers: { 'content-type': 'application/json' } });
    const created = response();
    await handler(request('/api/tenders', { method: 'POST', headers: jsonHeaders(), body: JSON.stringify({ title: 'T', reference: 'R' }) }), created);
    assert.equal(created.statusCode, 201);

    const snapshot = response();
    await handler(request('/api/tenders/abc/evaluation-snapshots', { method: 'POST', headers: jsonHeaders(), body: '{}' }), snapshot);
    assert.equal(snapshot.statusCode, 401);
    assert.equal(JSON.parse(snapshot.body).error.code, 'owner_session_required');

    const deleted = response();
    await handler(request('/api/tenders/abc', { method: 'DELETE', headers: jsonHeaders() }), deleted);
    assert.equal(deleted.statusCode, 401);
  });

  it('rejects anonymous writes from another origin and non-JSON bodies', async () => {
    configurePublicOnly();
    let called = false;
    globalThis.fetch = async () => { called = true; return new globalThis.Response('{}', { status: 201 }); };

    const crossOrigin = response();
    await handler(request('/api/tenders', { method: 'POST', headers: jsonHeaders({ origin: 'https://evil.example' }), body: '{}' }), crossOrigin);
    assert.equal(crossOrigin.statusCode, 403);

    const wrongType = response();
    await handler(request('/api/tenders', { method: 'POST', headers: { origin, 'content-type': 'application/octet-stream' }, body: 'binary' }), wrongType);
    assert.equal(wrongType.statusCode, 415);

    assert.equal(called, false);
  });

  it('caps the declared body size for anonymous writes', async () => {
    configurePublicOnly();
    let called = false;
    globalThis.fetch = async () => { called = true; return new globalThis.Response('{}', { status: 201 }); };
    const res = response();
    await handler(request('/api/tenders', { method: 'POST', headers: jsonHeaders({ 'content-length': String(200 * 1024) }), body: '{}' }), res);
    assert.equal(res.statusCode, 413);
    assert.equal(called, false);
  });

  it('rate limits anonymous writes separately from reads', async () => {
    configurePublicOnly();
    globalThis.fetch = async (_url, options) => new globalThis.Response('{}', {
      status: options?.method === 'POST' ? 201 : 200,
      headers: { 'content-type': 'application/json' },
    });
    const headers = jsonHeaders({ 'x-forwarded-for': '203.0.113.9' });
    let status;
    for (let attempt = 0; attempt <= __test.RATE_LIMITS.write; attempt += 1) {
      const res = response();
      await handler(request('/api/tenders', { method: 'POST', headers, body: '{}' }), res);
      status = res.statusCode;
    }
    assert.equal(status, 429);

    const read = response();
    await handler(request('/api/tenders', { headers: { 'x-forwarded-for': '203.0.113.9' } }), read);
    assert.equal(read.statusCode, 200);
  });

  it('keeps the backend path allowlist narrow', async () => {
    configurePublicOnly();
    assert.equal(__test.ALLOWED_BACKEND_PATH.test('/api/tenders/123/requirements'), true);
    assert.equal(__test.ALLOWED_BACKEND_PATH.test('/api/admin'), false);
    const res = response();
    await handler(request('/api/admin', { headers: {} }), res);
    assert.equal(res.statusCode, 404);
    assert.equal(JSON.parse(res.body).error.code, 'not_found');
  });
});

describe('Optional owner session', () => {
  it('issues secure HttpOnly sessions only after exact-origin password verification', async () => {
    configure();
    const rejected = response();
    await handler(request('/api/auth/login', { method: 'POST', headers: { origin: 'https://evil.example' }, body: JSON.stringify({ password }) }), rejected);
    assert.equal(rejected.statusCode, 403);

    const res = response();
    await handler(request('/api/auth/login', { method: 'POST', headers: { origin }, body: JSON.stringify({ password }) }), res);
    assert.equal(res.statusCode, 200);
    assert.match(res.headers['set-cookie'], /HttpOnly; Secure; SameSite=Strict/);
    assert.equal(__test.validSession(res.headers['set-cookie'], secret), true);
  });

  it('rejects incorrect passwords and expired or tampered sessions', async () => {
    configure();
    const rejected = response();
    await handler(request('/api/auth/login', { method: 'POST', headers: { origin }, body: JSON.stringify({ password: 'wrong' }) }), rejected);
    assert.equal(rejected.statusCode, 401);
    const expired = __test.issueSession(secret, Date.now() - 9 * 60 * 60 * 1000);
    assert.equal(__test.validSession(`__Host-tenderlens_session=${expired}`, secret), false);
    assert.equal(__test.validSession(`__Host-tenderlens_session=${expired}x`, secret), false);
  });

  it('lets the owner perform privileged writes', async () => {
    configure();
    let captured;
    globalThis.fetch = async (url, options) => {
      captured = { url: String(url), options };
      return new globalThis.Response('{}', { status: 202, headers: { 'content-type': 'application/json' } });
    };
    const res = response();
    await handler(request('/api/tenders/abc/evaluation-snapshots', { method: 'POST', headers: jsonHeaders({ cookie: cookie() }), body: '{}' }), res);
    assert.equal(res.statusCode, 202);
    assert.equal(captured.url, 'https://tenderlens-bv66.onrender.com/api/tenders/abc/evaluation-snapshots');
    assert.equal(captured.options.headers.get('cookie'), null);
  });

  it('disables owner sign-in when no passphrase is configured', async () => {
    configurePublicOnly();
    const res = response();
    await handler(request('/api/auth/login', { method: 'POST', headers: { origin }, body: JSON.stringify({ password }) }), res);
    assert.equal(res.statusCode, 503);
    assert.equal(JSON.parse(res.body).error.code, 'owner_sign_in_disabled');
  });
});

describe('Explicit route bindings', () => {
  it('binds every public route to the shared gateway handler', async () => {
    const routes = ['auth/session.js', 'auth/login.js', 'auth/logout.js', 'tenders.js', '[...path].js'];
    for (const route of routes) {
      const mod = await import(`../../api/${route}`);
      assert.equal(mod.default, handler, `${route} must export the shared handler`);
    }
  });
});
