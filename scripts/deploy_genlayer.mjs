#!/usr/bin/env node
/**
 * One-shot GenLayer deployment for CI (or any host with GenLayer network access).
 *
 * The reviewed contract source and the deployment gates live in api/_genlayer.js;
 * this script only supplies the confirmation and the signer. It never prints a
 * private key. Without a configured signer it generates an ephemeral testnet key,
 * which is appropriate only for a gasless testnet and for a contract with no
 * owner-only functions.
 */
import { appendFileSync, writeFileSync } from 'node:fs';
import process from 'node:process';
import { buildProbeSubmission, deployGenLayerContract, readGenLayerStatus, submitGenLayerEvaluation } from '../api/_genlayer.js';

const network = process.env.TENDERLENS_GENLAYER_NETWORK || 'studionet';
const confirmation = process.env.GENLAYER_DEPLOY_CONFIRM || '';
const mode = process.env.GENLAYER_DEPLOY_MODE || 'deploy';
const submissionPath = process.env.GENLAYER_SUBMISSION_FILE || '';

if (confirmation !== 'deploy') {
  console.error('Refusing to deploy: set GENLAYER_DEPLOY_CONFIRM=deploy to confirm this is intentional.');
  process.exit(2);
}

if (mode === 'probe') {
  process.env.TENDERLENS_GENLAYER_NETWORK = network;
  const status = await readGenLayerStatus();
  writeFileSync('deployment.json', `${JSON.stringify({ schema_version: 'tenderlens.genlayer-status.v1', outcome: status.reachable ? 'probe_reachable' : 'probe_unreachable', checked_at: new Date().toISOString(), ...status }, null, 2)}\n`);
  console.log(JSON.stringify(status, null, 2));
  process.exit(status.reachable ? 0 : 3);
}

// Asking the validators to interpret the submission the contract already
// holds. This is a real broadcast, so it needs the server-side authorization
// flag; without it the connector refuses before any key is used.
if (mode === 'evaluate') {
  process.env.TENDERLENS_GENLAYER_NETWORK = network;
  process.env.TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED = process.env.TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED || '1';
  process.env.TENDERLENS_GENLAYER_FINALITY_WAIT_MS = process.env.TENDERLENS_GENLAYER_FINALITY_WAIT_MS || '180000';
  // The contract has no owner check and Studionet charges no gas, so a CI-run
  // evaluation does not need a stored key. Sign with an ephemeral one when no
  // secret is configured and say so in the record.
  let ephemeralSigner = false;
  if (!process.env.TENDERLENS_GENLAYER_PRIVATE_KEY) {
    const { generatePrivateKey } = await import('genlayer-js');
    process.env.TENDERLENS_GENLAYER_PRIVATE_KEY = generatePrivateKey();
    ephemeralSigner = true;
    console.error('No GENLAYER_PRIVATE_KEY secret configured: signing with an ephemeral key (its address is recorded).');
  }
  let evaluation;
  try {
    evaluation = await submitGenLayerEvaluation({ env: process.env });
  } catch (error) {
    const failure = {
      schema_version: 'tenderlens.genlayer-evaluation.v1',
      outcome: 'error',
      network,
      checked_at: new Date().toISOString(),
      contract_address: process.env.TENDERLENS_GENLAYER_CONTRACT_ADDRESS || null,
      code: error?.code ?? 'evaluation_failed',
      hint: error?.hint ?? String(error?.message ?? error).slice(0, 400),
    };
    writeFileSync('deployment.json', `${JSON.stringify(failure, null, 2)}\n`);
    console.error(JSON.stringify(failure, null, 2));
    process.exit(1);
  }
  evaluation.signer_ephemeral = ephemeralSigner;
  writeFileSync('deployment.json', `${JSON.stringify(evaluation, null, 2)}\n`);
  console.log(JSON.stringify(evaluation, null, 2));
  process.exit(evaluation.outcome === 'finalized' ? 0 : 2);
}

process.env.TENDERLENS_GENLAYER_NETWORK = network;
process.env.TENDERLENS_GENLAYER_DEPLOY_ENABLED = '1';
process.env.TENDERLENS_GENLAYER_FINALITY_WAIT_MS = process.env.TENDERLENS_GENLAYER_FINALITY_WAIT_MS || '180000';

if (!process.env.TENDERLENS_GENLAYER_PRIVATE_KEY) {
  const { generatePrivateKey } = await import('genlayer-js');
  process.env.TENDERLENS_GENLAYER_PRIVATE_KEY = generatePrivateKey();
  console.log('No signer secret was supplied; generated an ephemeral testnet key. Only its address is reported.');
}

const before = await readGenLayerStatus();
console.log(`GenLayer network ${network}: reachable=${before.reachable} chain=${before.observed_chain_id ?? 'unknown'} error=${before.error?.code ?? 'none'}`);
if (!before.reachable) {
  console.error('The configured GenLayer node did not answer a read-only probe; refusing to sign.');
  process.exit(3);
}

let submission = null;
if (submissionPath) {
  const { readFile } = await import('node:fs/promises');
  submission = JSON.parse(await readFile(submissionPath, 'utf8'));
}

let record;
try {
  record = await deployGenLayerContract({ env: process.env, submission: submission || buildProbeSubmission() });
  // The connector reports whether the caller supplied a submission; this run
  // supplies a synthetic probe whenever no submission file was requested.
  record.probe_submission = !submission;
} catch (error) {
  const failure = {
    schema_version: 'tenderlens.genlayer-deployment.v1',
    outcome: 'error',
    network,
    checked_at: new Date().toISOString(),
    code: error?.code ?? 'deployment_failed',
    hint: error?.hint ?? String(error?.message ?? error).slice(0, 400),
  };
  writeFileSync('deployment.json', `${JSON.stringify(failure, null, 2)}\n`);
  console.error(JSON.stringify(failure, null, 2));
  process.exit(1);
}
writeFileSync('deployment.json', `${JSON.stringify(record, null, 2)}\n`);

const lines = [
  '## GenLayer deployment',
  '',
  `| Field | Value |`,
  `| --- | --- |`,
  `| Outcome | ${record.outcome} |`,
  `| Network | ${record.network} (chain ${record.chain_id}) |`,
  `| Contract address | ${record.contract_address ?? 'not yet finalized'} |`,
  `| Transaction | ${record.transaction_hash} |`,
  `| Status | ${record.status} |`,
  `| Lifecycle | ${record.lifecycle_status ?? 'not reported'} |`,
  `| Runtime | ${record.runtime} |`,
  `| Source SHA-256 | ${record.contract_source_sha256} |`,
  `| Deployed code matches source | ${record.deployed_code_matches_source ?? 'not verified'} |`,
  `| Probe submission | ${record.probe_submission} |`,
];
if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, `${lines.join('\n')}\n`);
console.log(lines.join('\n'));
console.log(JSON.stringify(record, null, 2));

if (record.outcome !== 'finalized') {
  console.error('The transaction was accepted but not finalized within the wait window. Re-run read-only status before doing anything else.');
  process.exit(4);
}
