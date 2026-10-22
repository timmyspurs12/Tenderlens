import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
  buildProbeSubmission,
  deployGenLayerContract,
  extractContractAddress,
  readConfig,
  readGenLayerStatus,
  readRuntimeReference,
  validateSubmissionForDeployment,
  NETWORKS,
} from './_genlayer.js';

const OWNER = '0x' + 'a'.repeat(40);
const CONTRACT = '0x' + 'b'.repeat(40);

function fakeSdk(overrides = {}) {
  const state = { deployed: false, status: 'FINALIZED' };
  const chain = {
    id: overrides.chainId ?? 61999,
    rpcUrls: { default: { http: [overrides.endpoint ?? 'https://studio.genlayer.com/api'] } },
  };
  const client = {
    async getChainId() { return overrides.observedChainId ?? 61999; },
    async getBlockNumber() { return 4242; },
    async getContractCode() { return overrides.code ?? 'class ProcurementEvaluation: pass'; },
    async readContract({ functionName }) {
      if (functionName === 'get_lifecycle_status') return overrides.lifecycle ?? 'prepared';
      if (functionName === 'get_result') {
        return JSON.stringify({ overall_status: 'inconclusive', decisions: [{}, {}], unresolved_requirements: [{}], disclaimer: 'Evidence interpretation only.' });
      }
      throw new Error('unknown function');
    },
    async deployContract({ code, args }) {
      state.deployed = { code, args };
      return '0x' + 'c'.repeat(64);
    },
    async getTransaction() {
      return {
        status: state.status,
        statusName: state.status,
        txDataDecoded: { contractAddress: CONTRACT },
      };
    },
  };
  return {
    state,
    chains: { studionet: chain },
    version: '1.1.8',
    createAccount() { return { address: OWNER }; },
    createClient() { return client; },
  };
}

const enabledEnv = () => ({
  TENDERLENS_GENLAYER_NETWORK: 'studionet',
  TENDERLENS_GENLAYER_DEPLOY_ENABLED: '1',
  TENDERLENS_GENLAYER_PRIVATE_KEY: '0x' + 'd'.repeat(64),
});

describe('GenLayer live status connector', () => {
  it('defaults to the pinned Studionet definition and never exposes a signer key', () => {
    const config = readConfig({ TENDERLENS_GENLAYER_PRIVATE_KEY: '0x' + 'd'.repeat(64) });
    assert.equal(config.network, 'studionet');
    assert.equal(config.pinned.chainId, 61999);
    assert.equal(config.hasPrivateKey, true);
    assert.equal(Object.hasOwn(config, 'privateKey'), false);
  });

  it('reports a verified chain identity and contract lifecycle', async () => {
    const status = await readGenLayerStatus({ env: { TENDERLENS_GENLAYER_CONTRACT_ADDRESS: CONTRACT }, sdkLoader: async () => fakeSdk() });
    assert.equal(status.reachable, true);
    assert.equal(status.chain_identity_verified, true);
    assert.equal(status.expected_chain_id, 61999);
    assert.equal(status.observed_chain_id, 61999);
    assert.equal(status.contract.code_present, true);
    assert.equal(status.contract.lifecycle_status, 'prepared');
    assert.equal(status.contract.result.overall_status, 'inconclusive');
  });

  it('fails closed on chain-id mismatch, SDK drift and unpinned endpoints', async () => {
    const mismatch = await readGenLayerStatus({ env: {}, sdkLoader: async () => fakeSdk({ observedChainId: 4221 }) });
    assert.equal(mismatch.reachable, false);
    assert.equal(mismatch.error.code, 'chain_identity_mismatch');

    const drift = await readGenLayerStatus({ env: {}, sdkLoader: async () => fakeSdk({ endpoint: 'https://evil.example/api' }) });
    assert.equal(drift.error.code, 'sdk_chain_definition_mismatch');

    const unpinned = await readGenLayerStatus({ env: { TENDERLENS_GENLAYER_RPC_URL: 'https://evil.example/api' }, sdkLoader: async () => fakeSdk() });
    assert.equal(unpinned.error.code, 'rpc_endpoint_not_pinned');
  });

  it('rejects unknown networks and malformed addresses', async () => {
    const unknown = await readGenLayerStatus({ env: { TENDERLENS_GENLAYER_NETWORK: 'mainnet' }, sdkLoader: async () => fakeSdk() });
    assert.equal(unknown.error.code, 'unsupported_network');
    assert.equal(unknown.configured, false);

    const bad = await readGenLayerStatus({ env: { TENDERLENS_GENLAYER_CONTRACT_ADDRESS: '0x1234' }, sdkLoader: async () => fakeSdk() });
    assert.equal(bad.error.code, 'invalid_contract_address');
  });
});

describe('GenLayer deployment gate', () => {
  it('refuses to sign without the explicit server-side gate, signer and address state', async () => {
    await assert.rejects(
      deployGenLayerContract({ env: { ...enabledEnv(), TENDERLENS_GENLAYER_DEPLOY_ENABLED: '0' }, sdkLoader: async () => fakeSdk() }),
      (error) => error.code === 'deployment_not_enabled',
    );
    await assert.rejects(
      deployGenLayerContract({ env: { TENDERLENS_GENLAYER_DEPLOY_ENABLED: '1' }, sdkLoader: async () => fakeSdk() }),
      (error) => error.code === 'signer_not_configured',
    );
    await assert.rejects(
      deployGenLayerContract({ env: { ...enabledEnv(), TENDERLENS_GENLAYER_CONTRACT_ADDRESS: CONTRACT }, sdkLoader: async () => fakeSdk() }),
      (error) => error.code === 'contract_address_already_set',
    );
    await assert.rejects(
      deployGenLayerContract({ env: { ...enabledEnv(), TENDERLENS_GENLAYER_RPC_URL: 'https://evil.example/api' }, sdkLoader: async () => fakeSdk() }),
      (error) => error.code === 'rpc_endpoint_not_pinned',
    );
  });

  it('deploys the reviewed contract source with a deterministic probe submission', async () => {
    const sdk = fakeSdk();
    const record = await deployGenLayerContract({ env: enabledEnv(), sdkLoader: async () => sdk });
    assert.equal(record.outcome, 'finalized');
    assert.equal(record.contract_address, CONTRACT);
    assert.equal(record.lifecycle_status, 'prepared');
    assert.equal(record.runtime, 'py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6');
    assert.equal(record.probe_submission, true);
    assert.equal(record.signer_address, OWNER);
    assert.match(sdk.state.deployed.args[0], /"schema_version":"tenderlens\.evaluation-submission\.v1"/);
    assert.equal(JSON.parse(sdk.state.deployed.args[0]).evidence.length, 0);
  });

  it('resolves a deploy address from every documented transaction shape', () => {
    assert.equal(extractContractAddress({ txDataDecoded: { contractAddress: CONTRACT } }), CONTRACT);
    assert.equal(extractContractAddress({ recipient: CONTRACT }), CONTRACT);
    assert.equal(extractContractAddress({ to_address: CONTRACT }), CONTRACT);
    assert.equal(extractContractAddress({ recipient: '0x' + '0'.repeat(40) }), null);
    assert.equal(extractContractAddress({}), null);
  });

  it('fails closed with the transaction reference when no address is reported', async () => {
    const sdk = fakeSdk();
    const baseCreate = sdk.createClient.bind(sdk);
    sdk.createClient = () => ({
      ...baseCreate(),
      async getTransaction() { return { status: 'FINALIZED', statusName: 'FINALIZED', resultName: 'MAJORITY_AGREE' }; },
    });
    await assert.rejects(
      deployGenLayerContract({ env: enabledEnv(), sdkLoader: async () => sdk }),
      (error) => error.code === 'deployed_contract_address_missing' && /0xc{64}/.test(error.hint),
    );
  });

  it('reports pending finality without inventing a settled address', async () => {
    const sdk = fakeSdk();
    const baseCreate = sdk.createClient.bind(sdk);
    sdk.createClient = () => ({
      ...baseCreate(),
      async getTransaction() { return { status: 'ACCEPTED', statusName: 'ACCEPTED' }; },
      async getContractCode() { return ''; },
      async readContract() { return 'prepared'; },
    });
    const record = await deployGenLayerContract({ env: { ...enabledEnv(), TENDERLENS_GENLAYER_FINALITY_WAIT_MS: '5000' }, sdkLoader: async () => sdk });
    assert.equal(record.outcome, 'pending_finality');
    assert.equal(record.status, 'ACCEPTED');
  });
});

describe('GenLayer submission fixtures', () => {
  it('builds a probe submission whose canonical digest matches the contract rule', () => {
    const probe = buildProbeSubmission();
    const checked = validateSubmissionForDeployment(probe);
    assert.equal(checked.digestMatches, true);
    assert.equal(probe.evidence.length, 0);
    assert.match(probe.requirements[0].code, /^DEPLOYMENT-PROBE-/);
  });

  it('rejects extra fields, mutable runtimes and unknown networks', () => {
    const probe = buildProbeSubmission();
    assert.throws(() => validateSubmissionForDeployment({ ...probe, extra: true }), (error) => error.code === 'invalid_submission');
    assert.throws(() => readRuntimeReference('# { "Depends": "py-genlayer:test" }'), (error) => error.code === 'mutable_contract_runtime_reference');
    assert.equal(NETWORKS.studionet.chainId, 61999);
    assert.equal(NETWORKS.testnetBradbury.endpoint, 'https://rpc-bradbury.genlayer.com');
  });
});
