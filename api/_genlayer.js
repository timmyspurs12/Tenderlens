import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { Buffer } from 'node:buffer';

/**
 * Server-only GenLayer connector for the live TenderLens build.
 *
 * This module runs inside the Vercel Node function, never in the browser. It
 * performs read-only network identity checks by default and only signs or
 * broadcasts when the server-side deployment gate and a server-only key are
 * present. Secrets are never returned, logged, or included in errors.
 *
 * Trust boundary: this proves what the configured GenLayer node answered at the
 * time of the check. It is not an audit of validator behaviour, and a deployed
 * address is not evidence that an evaluation is legally correct.
 */

export const STATUS_SCHEMA = 'tenderlens.genlayer-status.v1';
export const DEPLOYMENT_SCHEMA = 'tenderlens.genlayer-deployment.v1';
export const PINNED_RUNTIME = 'py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6';
export const RUNTIME_PATTERN = /^py-genlayer:[0-9a-z]{52}$/;
export const CONTRACT_PATH = 'tests/genlayer_direct/contracts/procurement_evaluation.py';
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const DECIDED_FAILURE_STATES = new Set(['UNDETERMINED', 'CANCELED', 'VALIDATORS_TIMEOUT', 'LEADER_TIMEOUT']);

// Chain ids and endpoints are pinned here and re-checked against the installed
// SDK definition at call time. A mismatch fails closed instead of silently
// following a moved endpoint.
export const NETWORKS = {
  studionet: { chainId: 61999, endpoint: 'https://studio.genlayer.com/api', explorer: 'https://explorer-studio.genlayer.com' },
  testnetBradbury: { chainId: 4221, endpoint: 'https://rpc-bradbury.genlayer.com', explorer: 'https://explorer-bradbury.genlayer.com' },
  testnetAsimov: { chainId: 4221, endpoint: 'https://rpc-asimov.genlayer.com', explorer: null },
};
export const DEFAULT_NETWORK = 'studionet';

export class GenLayerError extends Error {
  constructor(code, hint = null) {
    super(code);
    this.name = 'GenLayerError';
    this.code = code;
    this.hint = hint;
  }
}

function envValue(env, key) {
  const value = env[key];
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

export function readConfig(env = process.env) {
  const rawNetwork = envValue(env, 'TENDERLENS_GENLAYER_NETWORK') || DEFAULT_NETWORK;
  const network = NETWORKS[rawNetwork] ? rawNetwork : null;
  const endpoint = envValue(env, 'TENDERLENS_GENLAYER_RPC_URL');
  const address = envValue(env, 'TENDERLENS_GENLAYER_CONTRACT_ADDRESS');
  const privateKey = envValue(env, 'TENDERLENS_GENLAYER_PRIVATE_KEY');
  return {
    network,
    requestedNetwork: rawNetwork,
    pinned: network ? NETWORKS[network] : null,
    endpointOverride: endpoint,
    address: address && ADDRESS.test(address) ? address : null,
    addressInvalid: Boolean(address && !ADDRESS.test(address)),
    hasPrivateKey: Boolean(privateKey && /^(?:0x)?[0-9a-fA-F]{64}$/.test(privateKey)),
    deployEnabled: envValue(env, 'TENDERLENS_GENLAYER_DEPLOY_ENABLED') === '1',
    allowRedeploy: envValue(env, 'TENDERLENS_GENLAYER_ALLOW_REDEPLOY') === '1',
    maxFinalizeMs: Math.min(Math.max(Number.parseInt(envValue(env, 'TENDERLENS_GENLAYER_FINALITY_WAIT_MS') || '40000', 10) || 40000, 5000), 240000),
  };
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

/** Deterministic, clearly-labelled, non-tender constructor input. */
export function buildProbeSubmission(now = new Date()) {
  const stamp = now.toISOString().replace(/[-:.]/g, '').slice(0, 14);
  const base = {
    schema_version: 'tenderlens.evaluation-submission.v1',
    evaluation_id: '20000000-0000-4000-8000-000000000001',
    snapshot_id: '20000000-0000-4000-8000-000000000002',
    tender_id: '20000000-0000-4000-8000-000000000003',
    aggregation_policy: 'mandatory_gate_optional_advisory_v1',
    requirements: [{
      id: '20000000-0000-4000-8000-000000000004',
      code: `DEPLOYMENT-PROBE-${stamp}`,
      text: 'Deployment probe only. This is not a real tender requirement and carries no procurement meaning.',
      category: 'eligibility',
      mandatory: true,
      kind: 'judgment',
    }],
    evidence: [],
  };
  return { ...base, snapshot_sha256: sha256(canonicalJson(base)) };
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function canonicalDigest(submission) {
  const body = {};
  for (const [key, value] of Object.entries(submission)) if (key !== 'snapshot_sha256') body[key] = value;
  return sha256(canonicalJson(body));
}

export function validateSubmissionForDeployment(submission) {
  if (!submission || typeof submission !== 'object' || Array.isArray(submission)) {
    throw new GenLayerError('invalid_submission', 'The constructor input must be a JSON object.');
  }
  const expected = ['aggregation_policy', 'evaluation_id', 'evidence', 'requirements', 'schema_version', 'snapshot_id', 'snapshot_sha256', 'tender_id'];
  const keys = Object.keys(submission).sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new GenLayerError('invalid_submission', 'The constructor input did not match the documented submission schema.');
  }
  if (submission.schema_version !== 'tenderlens.evaluation-submission.v1') throw new GenLayerError('invalid_submission');
  if (!Array.isArray(submission.requirements) || submission.requirements.length < 1 || submission.requirements.length > 50) throw new GenLayerError('invalid_submission');
  if (!Array.isArray(submission.evidence) || submission.evidence.length > 100) throw new GenLayerError('invalid_submission');
  if (!/^[0-9a-f]{64}$/.test(String(submission.snapshot_sha256))) throw new GenLayerError('invalid_submission');
  const digest = canonicalDigest(submission);
  return { submission, digest, digestMatches: digest === submission.snapshot_sha256 };
}

export async function loadContractSource(root = process.cwd()) {
  const sourcePath = path.join(root, CONTRACT_PATH);
  let source;
  try {
    source = await readFile(sourcePath, 'utf8');
  } catch {
    throw new GenLayerError('contract_source_unavailable', `Expected the contract source at ${CONTRACT_PATH}.`);
  }
  return { source, sourcePath, sha256: sha256(Buffer.from(source, 'utf8')) };
}

export function readRuntimeReference(source) {
  const firstLine = String(source).split('\n', 1)[0] || '';
  const match = firstLine.match(/"Depends"\s*:\s*"py-genlayer:([^"]+)"/);
  if (!match) throw new GenLayerError('contract_runtime_reference_missing', 'Line 1 must pin a py-genlayer runtime.');
  const reference = `py-genlayer:${match[1]}`;
  if (!RUNTIME_PATTERN.test(reference)) throw new GenLayerError('mutable_contract_runtime_reference', 'Pin an immutable runner hash, never :test or :latest.');
  if (reference !== PINNED_RUNTIME) throw new GenLayerError('unexpected_contract_runtime_reference', `The reviewed runtime is ${PINNED_RUNTIME}.`);
  return reference;
}

function assertSdkDefinition(sdk, network) {
  const pinned = NETWORKS[network];
  const chain = sdk.chains?.[network];
  if (!chain) throw new GenLayerError('sdk_chain_definition_missing');
  const chainId = Number(chain.id);
  const endpoint = chain.rpcUrls?.default?.http?.[0];
  if (chainId !== pinned.chainId || endpoint !== pinned.endpoint) {
    throw new GenLayerError('sdk_chain_definition_mismatch', 'The installed genlayer-js chain definition moved; review before use.');
  }
  return chain;
}

function describe(error) {
  const parts = [];
  for (let current = error, depth = 0; current && depth < 3; current = current.cause, depth += 1) {
    const text = typeof current.message === 'string' ? current.message : String(current);
    if (text && !parts.includes(text)) parts.push(text.slice(0, 300));
  }
  return parts.join(' <- ').slice(0, 400) || null;
}

function sanitize(error) {
  if (error instanceof GenLayerError) return { code: error.code, hint: error.hint };
  const name = error && typeof error.name === 'string' ? error.name : '';
  if (/timeout|timedout|abort/i.test(`${name} ${error?.code || ''}`)) return { code: 'genlayer_request_timeout', hint: describe(error) };
  return { code: 'genlayer_request_failed', hint: describe(error) };
}

async function loadSdk() {
  try {
    return await import('genlayer-js');
  } catch {
    throw new GenLayerError('sdk_unavailable', 'genlayer-js is not installed in this deployment.');
  }
}

async function readClientStatus(client, config) {
  const chainId = Number(await client.getChainId());
  if (chainId !== config.pinned.chainId) throw new GenLayerError('chain_identity_mismatch');
  // Independent reads run together: this status route is the first thing the
  // workspace fetches, and each serial round trip is a visible second of
  // "Checking GenLayer" on a cold load.
  const [rawBlockNumber, code] = await Promise.all([
    client.getBlockNumber().catch(() => null),
    config.address ? client.getContractCode(config.address).catch(() => null) : Promise.resolve(null),
  ]);
  const blockNumber = rawBlockNumber == null ? null : Number(rawBlockNumber);
  const summary = {
    chain_identity_verified: true,
    observed_chain_id: chainId,
    block_number: Number.isFinite(blockNumber) ? blockNumber : null,
  };
  if (!config.address) {
    return { ...summary, contract: { address: null, code_present: false, lifecycle_status: null, explorer_url: null } };
  }
  const codePresent = typeof code === 'string' ? code.trim().length > 0 : Boolean(code);
  let lifecycle = null;
  let result = null;
  if (codePresent) {
    const [rawLifecycle, rawResult] = await Promise.all([
      client.readContract({ address: config.address, functionName: 'get_lifecycle_status', args: [] }).catch(() => null),
      client.readContract({ address: config.address, functionName: 'get_result', args: [] }).catch(() => null),
    ]);
    lifecycle = typeof rawLifecycle === 'string' ? rawLifecycle : rawLifecycle == null ? null : String(rawLifecycle);
    try {
      const text = typeof rawResult === 'string' ? rawResult : String(rawResult ?? '');
      if (text) {
        const parsed = JSON.parse(text);
        result = {
          overall_status: typeof parsed.overall_status === 'string' ? parsed.overall_status : null,
          requirement_count: Array.isArray(parsed.decisions) ? parsed.decisions.length : null,
          unresolved_count: Array.isArray(parsed.unresolved_requirements) ? parsed.unresolved_requirements.length : null,
          disclaimer: typeof parsed.disclaimer === 'string' ? parsed.disclaimer : null,
        };
      }
    } catch {
      result = null;
    }
  }
  return {
    ...summary,
    contract: {
      address: config.address,
      code_present: codePresent,
      lifecycle_status: lifecycle,
      result,
      explorer_url: config.pinned.explorer ? `${config.pinned.explorer}/address/${config.address}` : null,
    },
  };
}

export async function readGenLayerStatus({ env = process.env, sdkLoader = loadSdk } = {}) {
  const config = readConfig(env);
  const checkedAt = new Date().toISOString();
  const base = {
    schema_version: STATUS_SCHEMA,
    checked_at: checkedAt,
    configured: false,
    read_only: true,
    network: config.network,
    requested_network: config.requestedNetwork,
    expected_chain_id: config.pinned?.chainId ?? null,
    observed_chain_id: null,
    chain_identity_verified: false,
    endpoint: null,
    endpoint_pinned: true,
    sdk_version: null,
    reachable: false,
    block_number: null,
    contract: { address: config.address, code_present: false, lifecycle_status: null, result: null, explorer_url: null },
    deployment: {
      enabled: config.deployEnabled,
      signer_configured: config.hasPrivateKey,
      redeploy_allowed: config.allowRedeploy,
    },
    error: null,
  };
  if (!config.network) {
    return { ...base, error: { code: 'unsupported_network', hint: `Known networks: ${Object.keys(NETWORKS).join(', ')}.` } };
  }
  if (config.addressInvalid) {
    return { ...base, error: { code: 'invalid_contract_address', hint: 'TENDERLENS_GENLAYER_CONTRACT_ADDRESS must be a 20-byte hex address.' } };
  }
  if (config.endpointOverride && config.endpointOverride !== config.pinned.endpoint) {
    return { ...base, endpoint: config.pinned.endpoint, endpoint_pinned: false, error: { code: 'rpc_endpoint_not_pinned', hint: `The reviewed endpoint is ${config.pinned.endpoint}.` } };
  }
  base.configured = true;
  base.endpoint = config.pinned.endpoint;
  try {
    const sdk = await sdkLoader();
    const chain = assertSdkDefinition(sdk, config.network);
    base.sdk_version = typeof sdk.version === 'string' ? sdk.version : null;
    const client = sdk.createClient({ chain });
    const status = await readClientStatus(client, config);
    return { ...base, reachable: true, ...status };
  } catch (error) {
    return { ...base, error: sanitize(error) };
  }
}

const sleep = (ms) => new Promise((resolve) => globalThis.setTimeout(resolve, ms));

/**
 * The SDK exposes a deploy's ghost-contract address as the transaction
 * recipient, and normalizes it into txDataDecoded.contractAddress only on some
 * code paths. Check every documented shape before concluding it is missing.
 */
export function extractContractAddress(transaction) {
  if (!transaction || typeof transaction !== 'object') return null;
  const decoded = transaction.txDataDecoded || transaction.tx_data_decoded || {};
  const candidates = [
    decoded.contractAddress,
    decoded.contract_address,
    transaction.recipient,
    transaction.to_address,
    transaction.to,
    transaction.data && transaction.data.recipient,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && ADDRESS.test(candidate) && !/^0x0{40}$/i.test(candidate)) return candidate;
  }
  return null;
}

async function awaitFinality(client, hash, maxMs, intervalMs = 3000, wait = sleep) {
  const deadline = Date.now() + maxMs;
  let last;
  for (;;) {
    try {
      last = await client.getTransaction({ hash });
    } catch {
      last = null;
    }
    const status = last && (last.statusName || last.status);
    if (typeof status === 'string' && status === 'FINALIZED') return { transaction: last, status };
    if (typeof status === 'string' && DECIDED_FAILURE_STATES.has(status)) {
      throw new GenLayerError(`deployment_${status.toLowerCase()}`);
    }
    if (Date.now() >= deadline) return { transaction: last, status: typeof status === 'string' ? status : 'UNKNOWN' };
    await wait(intervalMs);
  }
}

export async function deployGenLayerContract({ env = process.env, submission = null, sdkLoader = loadSdk, root = process.cwd() } = {}) {
  const config = readConfig(env);
  if (!config.network) throw new GenLayerError('unsupported_network');
  if (!config.deployEnabled) throw new GenLayerError('deployment_not_enabled', 'Set the server-side TENDERLENS_GENLAYER_DEPLOY_ENABLED=1 flag to allow a deploy.');
  if (!config.hasPrivateKey) throw new GenLayerError('signer_not_configured', 'A server-only TENDERLENS_GENLAYER_PRIVATE_KEY is required to sign.');
  if (config.address && !config.allowRedeploy) throw new GenLayerError('contract_address_already_set', 'Clear the configured address or set TENDERLENS_GENLAYER_ALLOW_REDEPLOY=1.');
  if (config.endpointOverride && config.endpointOverride !== config.pinned.endpoint) throw new GenLayerError('rpc_endpoint_not_pinned');

  const { source, sha256: sourceSha256 } = await loadContractSource(root);
  const runtime = readRuntimeReference(source);
  const candidate = validateSubmissionForDeployment(submission || buildProbeSubmission());
  const constructorArgs = [JSON.stringify(candidate.submission)];

  const sdk = await sdkLoader();
  const chain = assertSdkDefinition(sdk, config.network);
  const privateKey = envValue(env, 'TENDERLENS_GENLAYER_PRIVATE_KEY');
  if (!privateKey) throw new GenLayerError('signer_not_configured');
  const normalizedKey = privateKey.startsWith('0x') ? privateKey : `0x${privateKey}`;
  let account;
  try {
    account = sdk.createAccount(normalizedKey);
  } catch {
    throw new GenLayerError('invalid_signer_key');
  }
  const client = sdk.createClient({ chain, account });

  let hash;
  try {
    hash = await client.deployContract({ code: source, args: constructorArgs });
  } catch (error) {
    const detail = sanitize(error);
    throw new GenLayerError(detail.code, detail.hint || 'The node rejected the deploy before consensus was reached.');
  }
  if (typeof hash !== 'string' || !/^0x[0-9a-fA-F]{64}$/.test(hash)) throw new GenLayerError('deployment_transaction_reference_invalid');

  const { transaction, status } = await awaitFinality(client, hash, config.maxFinalizeMs);
  const address = extractContractAddress(transaction);
  const outcomeDetail = {
    result_name: transaction?.resultName ?? null,
    execution_result: transaction?.txExecutionResultName ?? null,
    receipt: typeof transaction?.txReceipt === 'string' ? transaction.txReceipt : null,
  };
  if (status !== 'FINALIZED') {
    return {
      schema_version: DEPLOYMENT_SCHEMA,
      outcome: 'pending_finality',
      transaction_hash: hash,
      contract_address: ADDRESS.test(String(address)) ? address : null,
      status,
      network: config.network,
      chain_id: config.pinned.chainId,
      signer_address: account.address,
      contract_source_sha256: sourceSha256,
      runtime,
      probe_submission: !submission,
    };
  }
  if (!address) {
    throw new GenLayerError(
      'deployed_contract_address_missing',
      `transaction ${hash} reached ${status} (result=${outcomeDetail.result_name ?? 'unknown'}, execution=${outcomeDetail.execution_result ?? 'unknown'}) but the node did not report a contract recipient. Treat the contract as unverified.`,
    );
  }
  let deployedCode;
  try {
    const code = await client.getContractCode(address);
    deployedCode = typeof code === 'string' ? sha256(Buffer.from(code, 'utf8')) === sourceSha256 : null;
  } catch {
    deployedCode = null;
  }
  let lifecycle;
  try {
    lifecycle = await client.readContract({ address, functionName: 'get_lifecycle_status', args: [] });
  } catch {
    lifecycle = null;
  }
  return {
    schema_version: DEPLOYMENT_SCHEMA,
    outcome: 'finalized',
    transaction_hash: hash,
    contract_address: address,
    status,
    network: config.network,
    chain_id: config.pinned.chainId,
    signer_address: account.address,
    contract_source_sha256: sourceSha256,
    deployed_code_matches_source: deployedCode,
    runtime,
    lifecycle_status: typeof lifecycle === 'string' ? lifecycle : null,
    ...outcomeDetail,
    snapshot_digest: candidate.digest,
    digest_matches: candidate.digestMatches,
    probe_submission: !submission,
    explorer_url: config.pinned.explorer ? `${config.pinned.explorer}/address/${address}` : null,
  };
}
