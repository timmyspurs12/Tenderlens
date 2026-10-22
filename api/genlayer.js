import { Buffer } from 'node:buffer';
import process from 'node:process';
import { validSession } from './_session.js';
import { GenLayerError, readGenLayerStatus, deployGenLayerContract, readConfig } from './_genlayer.js';

/**
 * `/api/genlayer` — same-origin GenLayer surface for the deployed workspace.
 *
 * GET  returns a live, read-only network/contract status. It is intentionally
 *      public because it exposes no documents, no credentials, and no
 *      evaluation content beyond on-chain lifecycle labels that are already
 *      public on the GenLayer network. Contract result counts are returned only
 *      to an authenticated workspace session.
 * POST performs a one-shot deployment and requires the exact request origin,
 *      an authenticated session, the server-side deploy flag, and a server-only
 *      signer key.
 */

const MAX_BODY_BYTES = 300_000;

function json(res, status, value) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.end(JSON.stringify(value));
}

function deploymentBlockers(config) {
  const blockers = [];
  if (!config.network) blockers.push('unsupported_network');
  if (!config.deployEnabled) blockers.push('deployment_flag_off');
  if (!config.hasPrivateKey) blockers.push('signer_key_absent');
  if (config.address && !config.allowRedeploy) blockers.push('contract_address_already_set');
  if (config.addressInvalid) blockers.push('invalid_contract_address');
  if (config.endpointOverride && config.endpointOverride !== config.pinned?.endpoint) blockers.push('rpc_endpoint_not_pinned');
  return blockers;
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error('too_large');
    chunks.push(chunk);
  }
  if (!size) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new Error('invalid_json');
  }
}

export function sessionState(cookieHeader, env = process.env) {
  const secret = env.TENDERLENS_SESSION_SECRET;
  return typeof secret === 'string' && secret.length >= 32 && validSession(cookieHeader, secret);
}

export default async function handler(req, res) {
  const env = process.env;
  const config = readConfig(env);
  const method = req.method || 'GET';
  const origin = env.TENDERLENS_PUBLIC_ORIGIN;

  if (method === 'GET' || method === 'HEAD') {
    const authenticated = sessionState(req.headers.cookie, env);
    const status = await readGenLayerStatus({ env });
    if (!authenticated) {
      // Strip evaluation content for anonymous callers; lifecycle labels remain.
      status.contract = { ...status.contract, result: null };
    }
    return json(res, 200, {
      ...status,
      authenticated,
      deployment: { ...status.deployment, blockers: deploymentBlockers(config) },
      environment_note: status.reachable
        ? 'Live read-only reply from the configured GenLayer node.'
        : 'No live GenLayer reply was obtained from this build.',
    });
  }

  if (method !== 'POST') return json(res, 405, { error: { code: 'method_not_allowed', message: 'Use GET or POST.' } });
  if (!origin || req.headers.origin !== origin) return json(res, 403, { error: { code: 'origin_rejected', message: 'Request origin was not accepted.' } });
  if (!sessionState(req.headers.cookie, env)) return json(res, 401, { error: { code: 'unauthorized', message: 'Workspace sign-in is required.' } });

  let body;
  try {
    body = await readJson(req);
  } catch {
    return json(res, 400, { error: { code: 'invalid_request', message: 'A small JSON body is required.' } });
  }
  if (!body || typeof body !== 'object' || Array.isArray(body) || body.action !== 'deploy') {
    return json(res, 400, { error: { code: 'invalid_request', message: 'Only {"action":"deploy"} is supported.' } });
  }
  if (body.submission !== undefined && (typeof body.submission !== 'object' || body.submission === null)) {
    return json(res, 400, { error: { code: 'invalid_submission', message: 'submission must be a JSON object.' } });
  }

  try {
    const record = await deployGenLayerContract({ env, submission: body.submission || null });
    return json(res, record.outcome === 'finalized' ? 201 : 202, record);
  } catch (error) {
    if (error instanceof GenLayerError) {
      const statusCode = error.code === 'deployment_not_enabled' || error.code === 'signer_not_configured' ? 409
        : error.code === 'contract_address_already_set' ? 409
        : 502;
      return json(res, statusCode, { error: { code: error.code, message: 'The deployment did not complete.', hint: error.hint } });
    }
    return json(res, 502, { error: { code: 'deployment_failed', message: 'The deployment did not complete.' } });
  }
}

export const __test = { deploymentBlockers, sessionState, readConfig };
