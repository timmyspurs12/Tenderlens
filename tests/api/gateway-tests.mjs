import { Buffer } from 'node:buffer';
import process from 'node:process';
import { afterEach, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import handler, { __test } from '../../api/_gateway.js';

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
function response() {
  const headers = {};
  return { headers, statusCode: 200, body: undefined, setHeader(k, v) { headers[k.toLowerCase()] = v; }, end(body) { this.body = body; } };
}
function request(path, { method = 'GET', headers = {}, body = '' } = {}) {
  return { url: path, method, headers, async *[Symbol.asyncIterator]() { if (body) yield Buffer.from(body); } };
}
function cookie() { return `__Host-tenderlens_session=${__test.issueSession(secret)}; Path=/`; }

afterEach(() => {
  for (const key of envKeys) {
    if (original[key] === undefined) delete process.env[key];
    else process.env[key] = original[key];
  }
  globalThis.fetch = originalFetch;
});
const originalFetch = globalThis.fetch;

describe('Vercel same-origin workspace gateway', () => {
  it('fails closed when secrets/configuration are missing', async () => {
    for (const key of envKeys) delete process.env[key];
    const res = response();
    await handler(request('/api/auth/session'), res);
    assert.equal(res.statusCode, 503);
    assert.equal(JSON.parse(res.body).error.code, 'gateway_not_configured');
  });

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

  it('does not proxy private requests without a valid session', async () => {
    configure();
    const res = response();
    await handler(request('/api/tenders'), res);
    assert.equal(res.statusCode, 401);
  });

  it('proxies workspace requests with server-only bearer auth and no browser credentials', async () => {
    configure();
    let captured;
    globalThis.fetch = async (url, options) => {
      captured = { url: String(url), options };
      return new globalThis.Response(JSON.stringify({ items: [] }), { status: 200, headers: { 'content-type': 'application/json' } });
    };
    const res = response();
    await handler(request('/api/tenders?sort=recent', { headers: { cookie: cookie(), authorization: 'Bearer attacker', accept: 'application/json' } }), res);
    assert.equal(res.statusCode, 200);
    assert.deepEqual(JSON.parse(res.body), { items: [] });
    assert.equal(captured.url, 'https://tenderlens-bv66.onrender.com/api/tenders?sort=recent');
    assert.equal(captured.options.headers.get('authorization'), `Bearer ${'t'.repeat(48)}`);
    assert.equal(captured.options.headers.get('cookie'), null);
  });

  it('limits proxy paths and enforces origin on writes', async () => {
    configure();
    const deniedWrite = response();
    await handler(request('/api/tenders', { method: 'POST', headers: { cookie: cookie(), origin: 'https://evil.example' }, body: '{}' }), deniedWrite);
    assert.equal(deniedWrite.statusCode, 403);
    const deniedPath = response();
    await handler(request('/healthz', { headers: { cookie: cookie() } }), deniedPath);
    assert.equal(deniedPath.statusCode, 404);
    assert.equal(__test.ALLOWED_BACKEND_PATH.test('/api/tenders/123/requirements'), true);
    assert.equal(__test.ALLOWED_BACKEND_PATH.test('/api/admin'), false);
  });
});

describe('explicit route bindings', () => {
  it('binds every public route to the shared gateway handler', async () => {
    const routes = ['auth/session.js', 'auth/login.js', 'auth/logout.js', 'tenders.js', '[...path].js'];
    for (const route of routes) {
      const mod = await import(`../../api/${route}`);
      assert.equal(mod.default, handler, `${route} must export the shared handler`);
    }
  });
});
