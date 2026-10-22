export type ApiStatus = 'loading' | 'ready' | 'unavailable' | 'invalid' | 'unauthorized' | 'not_configured';
export type Tender = { id: string; title: string; reference: string; deadline?: string | null; created_at?: string; status?: string };
export type DocumentRecord = { id: string; title: string; version?: string; status?: string };
export type Requirement = { id: string; text: string; category?: string; mandatory?: boolean; status?: string };

const configuredBase = (import.meta as ImportMeta & { env: Record<string, string | undefined> }).env.VITE_API_BASE_URL;
const base = configuredBase?.trim().replace(/\/+$/, '') ?? (import.meta.env.PROD ? '/' : undefined);
const DEFAULT_TIMEOUT_MS = 10_000;

export type ApiErrorCode = 'API_NOT_CONFIGURED' | 'AUTH_REQUIRED' | 'HTTP_ERROR' | 'NETWORK_ERROR' | 'TIMEOUT' | 'INVALID_RESPONSE';

export class ApiError extends Error {
  constructor(readonly code: ApiErrorCode, message: string, readonly status?: number) {
    super(message);
    this.name = 'ApiError';
  }
}

export function statusForApiError(error: unknown): ApiStatus {
  if (!(error instanceof ApiError)) return 'unavailable';
  if (error.code === 'API_NOT_CONFIGURED') return 'not_configured';
  if (error.code === 'AUTH_REQUIRED') return 'unauthorized';
  if (error.code === 'INVALID_RESPONSE') return 'invalid';
  return 'unavailable';
}

function endpoint(path: string, apiBase?: string): string {
  if (apiBase === undefined || apiBase.trim() === '') throw new ApiError('API_NOT_CONFIGURED', 'API_NOT_CONFIGURED');
  const normalizedBase = apiBase.trim().replace(/\/+$/, '');
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

export async function request<T>(path: string, apiBase = base, init: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const url = endpoint(path, apiBase);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new DOMException('Request timed out', 'TimeoutError')), timeoutMs);
  try {
    const headers = new Headers(init.headers);
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    let response: Response;
    try {
      response = await fetch(url, {
        ...init,
        headers,
        // User-session cookies may be sent to an identity-aware gateway. No
        // privileged service bearer token is ever attached by browser code.
        credentials: 'include',
        signal: controller.signal,
      });
    } catch (error) {
      if (controller.signal.aborted || (error instanceof DOMException && (error.name === 'AbortError' || error.name === 'TimeoutError'))) {
        throw new ApiError('TIMEOUT', 'The workspace service did not respond in time.');
      }
      throw new ApiError('NETWORK_ERROR', 'The workspace service could not be reached.');
    }

    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        throw new ApiError('AUTH_REQUIRED', 'Workspace authentication is required.', response.status);
      }
      throw new ApiError('HTTP_ERROR', `The workspace service returned HTTP ${response.status}.`, response.status);
    }
    try {
      return await response.json() as T;
    } catch {
      throw new ApiError('INVALID_RESPONSE', 'The workspace service returned malformed JSON.');
    }
  } finally {
    clearTimeout(timeout);
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isTender(value: unknown): value is Tender {
  return isRecord(value) && typeof value.id === 'string' && typeof value.title === 'string' && typeof value.reference === 'string';
}

export function asArray<T>(value: unknown): T[] | null {
  return Array.isArray(value) ? value as T[] : null;
}

function parseTender(value: unknown): Tender[] {
  const rows = Array.isArray(value) ? value : isRecord(value) ? value.items : null;
  if (!Array.isArray(rows) || !rows.every(isTender)) {
    throw new ApiError('INVALID_RESPONSE', 'The response did not match the expected tender record schema.');
  }
  return rows as Tender[];
}

export async function loadTenders(apiBase = base): Promise<Tender[]> {
  return parseTender(await request<unknown>('/api/tenders', apiBase));
}

export async function checkGatewaySession(apiBase = base): Promise<void> {
  const result = await request<unknown>('/api/auth/session', apiBase);
  if (!isRecord(result) || result.authenticated !== true) {
    throw new ApiError('INVALID_RESPONSE', 'The gateway did not confirm an authenticated session.');
  }
}

export async function signIn(password: string, apiBase = base): Promise<void> {
  const result = await request<unknown>('/api/auth/login', apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (!isRecord(result) || result.authenticated !== true) {
    throw new ApiError('INVALID_RESPONSE', 'The gateway did not confirm sign-in.');
  }
}

export async function createTender(input: { title: string; reference: string; deadline?: string }, apiBase = base): Promise<Tender> {
  const result = await request<unknown>('/api/tenders', apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!isTender(result)) throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid tender record.');
  return result;
}

export type GenLayerContractStatus = {
  address: string | null;
  code_present: boolean;
  lifecycle_status: string | null;
  result: { overall_status: string | null; requirement_count: number | null; unresolved_count: number | null; disclaimer: string | null } | null;
  explorer_url: string | null;
};

export type GenLayerStatus = {
  schema_version: string;
  checked_at: string;
  configured: boolean;
  network: string | null;
  expected_chain_id: number | null;
  observed_chain_id: number | null;
  chain_identity_verified: boolean;
  endpoint: string | null;
  endpoint_pinned: boolean;
  sdk_version: string | null;
  reachable: boolean;
  block_number: number | null;
  contract: GenLayerContractStatus;
  deployment: { enabled: boolean; signer_configured: boolean; redeploy_allowed: boolean; blockers: string[] };
  error: { code: string; hint: string | null } | null;
  authenticated: boolean;
};

function optionalString(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

export async function loadGenLayerStatus(apiBase = base): Promise<GenLayerStatus> {
  const result = await request<unknown>('/api/genlayer', apiBase, {}, 20_000);
  if (!isRecord(result) || result.schema_version !== 'tenderlens.genlayer-status.v1' || typeof result.reachable !== 'boolean') {
    throw new ApiError('INVALID_RESPONSE', 'The GenLayer status response did not match the documented schema.');
  }
  const contract = isRecord(result.contract) ? result.contract : {};
  const deployment = isRecord(result.deployment) ? result.deployment : {};
  const error = isRecord(result.error) && typeof result.error.code === 'string' ? { code: result.error.code, hint: optionalString(result.error.hint) } : null;
  const resultSummary = isRecord(contract.result) ? contract.result : null;
  return {
    schema_version: 'tenderlens.genlayer-status.v1',
    checked_at: optionalString(result.checked_at) ?? '',
    configured: result.configured === true,
    network: optionalString(result.network),
    expected_chain_id: typeof result.expected_chain_id === 'number' ? result.expected_chain_id : null,
    observed_chain_id: typeof result.observed_chain_id === 'number' ? result.observed_chain_id : null,
    chain_identity_verified: result.chain_identity_verified === true,
    endpoint: optionalString(result.endpoint),
    endpoint_pinned: result.endpoint_pinned !== false,
    sdk_version: optionalString(result.sdk_version),
    reachable: result.reachable === true,
    block_number: typeof result.block_number === 'number' ? result.block_number : null,
    contract: {
      address: optionalString(contract.address),
      code_present: contract.code_present === true,
      lifecycle_status: optionalString(contract.lifecycle_status),
      result: resultSummary ? {
        overall_status: optionalString(resultSummary.overall_status),
        requirement_count: typeof resultSummary.requirement_count === 'number' ? resultSummary.requirement_count : null,
        unresolved_count: typeof resultSummary.unresolved_count === 'number' ? resultSummary.unresolved_count : null,
        disclaimer: optionalString(resultSummary.disclaimer),
      } : null,
      explorer_url: optionalString(contract.explorer_url),
    },
    deployment: {
      enabled: deployment.enabled === true,
      signer_configured: deployment.signer_configured === true,
      redeploy_allowed: deployment.redeploy_allowed === true,
      blockers: Array.isArray(deployment.blockers) ? deployment.blockers.filter((item): item is string => typeof item === 'string') : [],
    },
    error,
    authenticated: result.authenticated === true,
  };
}
