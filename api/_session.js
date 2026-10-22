import { Buffer } from 'node:buffer';
import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

// Single-owner workspace session shared by the record gateway and the GenLayer
// route. This is a prototype owner gate, not multi-user identity.

export const COOKIE = '__Host-tenderlens_session';
export const SESSION_SECONDS = 8 * 60 * 60;

export function sign(value, secret) {
  return createHmac('sha256', secret).update(value).digest('base64url');
}

export function issueSession(secret, now = Date.now()) {
  const payload = Buffer.from(JSON.stringify({ exp: Math.floor(now / 1000) + SESSION_SECONDS, nonce: randomBytes(24).toString('hex') })).toString('base64url');
  return `${payload}.${sign(payload, secret)}`;
}

export function validSession(cookieHeader, secret, now = Date.now()) {
  if (typeof secret !== 'string' || secret.length < 32) return false;
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

export function setSessionCookie(res, value, maxAge = SESSION_SECONDS) {
  res.setHeader('Set-Cookie', `${COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`);
}

export function passwordMatches(candidate, expected) {
  if (typeof candidate !== 'string' || candidate.length > 1024) return false;
  const left = createHmac('sha256', 'tenderlens-login-compare').update(candidate).digest();
  const right = createHmac('sha256', 'tenderlens-login-compare').update(expected).digest();
  return timingSafeEqual(left, right);
}
