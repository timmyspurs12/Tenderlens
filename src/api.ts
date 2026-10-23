export type ApiStatus = 'loading' | 'waking' | 'ready' | 'unavailable' | 'invalid' | 'unauthorized' | 'not_configured';
export type Tender = { id: string; title: string; reference: string; deadline?: string | null; created_at?: string; status?: string };
export type DocumentRecord = {
  id: string;
  group_id: string;
  version: number;
  filename: string;
  content_type: string;
  size: number;
  sha256: string;
  supersedes_document_id: string | null;
  created_at: string;
};
export type RequirementCategory = 'eligibility' | 'mandatory_documentation' | 'technical' | 'interpretive';
export type RequirementKind = 'objective' | 'judgment';
export type Requirement = {
  id: string;
  code: string;
  text: string;
  category: RequirementCategory;
  mandatory: boolean;
  kind: RequirementKind;
  created_at: string;
};
export type EvidenceProvenance = {
  document_id: string;
  document_sha256: string;
  document_version: number;
  filename: string;
  start_byte: number;
  end_byte: number;
  retrieved_at: string;
  integrity_check: string;
  source_authenticity: string;
};
export type EvidenceRecord = {
  id: string;
  requirement_id: string | null;
  document_id: string;
  document_sha256: string;
  start_byte: number;
  end_byte: number;
  excerpt: string;
  provenance: EvidenceProvenance;
  created_at: string;
};
export type AmendmentRelationship = 'explicit_replacement' | 'conflicting' | 'clarification' | 'unresolved' | 'none';
export type AmendmentRecord = {
  id: string;
  document_id: string;
  predecessor_id: string | null;
  relationship: AmendmentRelationship;
  comparison_status?: string;
  sha256?: string;
  created_at: string;
};
export type AggregationPolicy = 'mandatory_gate_optional_advisory_v1' | 'all_requirements_blocking_v1';
export type EvaluationSnapshotSummary = {
  id: string;
  input_sha256: string;
  created_at: string;
  status: string;
};
export type EvaluationSnapshot = {
  snapshot_id: string;
  input_sha256: string;
  created_at: string;
  status: string;
  submission: Record<string, unknown>;
};
export type WorkspaceSession = {
  authenticated: boolean;
  mode: 'public' | 'owner';
  owner_sign_in: boolean;
  can_read: boolean;
  can_register: boolean;
  can_write_privileged: boolean;
};

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

/**
 * A failure that is consistent with a sleeping records host rather than a
 * broken one: no answer in time, no connection, or a gateway 502/503/504 while
 * the upstream instance boots. Free compute plans stop an idle service, so the
 * first request after a quiet period can legitimately fail this way.
 */
export function isStartupRetryable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.code === 'TIMEOUT' || error.code === 'NETWORK_ERROR') return true;
  return error.code === 'HTTP_ERROR' && (error.status === 502 || error.status === 503 || error.status === 504);
}

/**
 * Per-attempt budgets for the register load. The first attempt fails fast so a
 * warm service feels instant; the later, longer attempts cover a cold start.
 * The gateway holds an upstream request for up to 50s, so the second budget
 * stays under that ceiling.
 */
export const WAKE_BUDGETS_MS = [12_000, 45_000, 30_000];

export type WakeOptions = { onWake?: (attempt: number) => void; budgets?: number[] };

/**
 * Treat a cold-start failure as "try again" instead of "the service is
 * broken". `onWake` fires before each retry so the caller can explain the
 * wait. A real error (401, 404, malformed payload) is thrown at once and is
 * never retried.
 */
async function withWakeup<T>(run: (timeoutMs: number) => Promise<T>, { onWake, budgets = WAKE_BUDGETS_MS }: WakeOptions = {}): Promise<T> {
  if (budgets.length === 0) throw new ApiError('API_NOT_CONFIGURED', 'API_NOT_CONFIGURED');
  let attempt = 0;
  for (;;) {
    try {
      return await run(budgets[attempt]);
    } catch (error) {
      if (!isStartupRetryable(error) || attempt >= budgets.length - 1) throw error;
      attempt += 1;
      onWake?.(attempt);
    }
  }
}

export function loadTendersWithWakeup(apiBase = base, options: WakeOptions = {}): Promise<Tender[]> {
  return withWakeup((timeoutMs) => loadTenders(apiBase, timeoutMs), options);
}

export function loadTenderWithWakeup(id: string, apiBase = base, options: WakeOptions = {}): Promise<Tender> {
  return withWakeup((timeoutMs) => loadTender(id, apiBase, timeoutMs), options);
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

export async function loadTenders(apiBase = base, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<Tender[]> {
  return parseTender(await request<unknown>('/api/tenders', apiBase, {}, timeoutMs));
}

/**
 * Load one tender record.
 *
 * The single-record route is the intended path, but a deployment may not bind
 * the nested function, so a routing 404 falls back to reading the register and
 * picking the record out of it. Any other failure is reported as-is.
 */
export async function loadTender(id: string, apiBase = base, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<Tender> {
  if (!id) throw new ApiError('INVALID_RESPONSE', 'A tender id is required.');
  try {
    const result = await request<unknown>(`/api/tenders/${encodeURIComponent(id)}`, apiBase, {}, timeoutMs);
    if (!isTender(result)) throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid tender record.');
    return result;
  } catch (error) {
    const routeless = error instanceof ApiError && error.status === 404;
    if (!routeless) throw error;
    const rows = await loadTenders(apiBase, timeoutMs);
    const found = rows.find((row) => row.id === id);
    if (!found) throw new ApiError('HTTP_ERROR', 'No tender record matches this identifier.', 404);
    return found;
  }
}

/**
 * Registration is a single attempt on purpose: it is not idempotent, so a
 * blind retry could create a duplicate record. The budget is long enough to
 * absorb a cold start, and a timeout tells the visitor to check the register
 * before submitting again.
 */
export const REGISTRATION_TIMEOUT_MS = 45_000;

export async function createTender(input: { title: string; reference: string; deadline?: string }, apiBase = base): Promise<Tender> {
  const result = await request<unknown>('/api/tenders', apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }, REGISTRATION_TIMEOUT_MS);
  if (!isTender(result)) throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid tender record.');
  return result;
}

export const MAX_DOCUMENT_UPLOAD_BYTES = 20 * 1024 * 1024;
const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const REQUIREMENT_CATEGORIES: RequirementCategory[] = ['eligibility', 'mandatory_documentation', 'technical', 'interpretive'];
const REQUIREMENT_KINDS: RequirementKind[] = ['objective', 'judgment'];
const AMENDMENT_RELATIONSHIPS: AmendmentRelationship[] = ['explicit_replacement', 'conflicting', 'clarification', 'unresolved', 'none'];
const AGGREGATION_POLICIES: AggregationPolicy[] = ['mandatory_gate_optional_advisory_v1', 'all_requirements_blocking_v1'];

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

function isDocumentRecord(value: unknown): value is DocumentRecord {
  return isRecord(value)
    && typeof value.id === 'string'
    && typeof value.group_id === 'string'
    && typeof value.version === 'number'
    && typeof value.filename === 'string'
    && typeof value.content_type === 'string'
    && typeof value.size === 'number'
    && typeof value.sha256 === 'string'
    && isNullableString(value.supersedes_document_id)
    && typeof value.created_at === 'string';
}

function isRequirement(value: unknown): value is Requirement {
  return isRecord(value)
    && typeof value.id === 'string'
    && typeof value.code === 'string'
    && typeof value.text === 'string'
    && REQUIREMENT_CATEGORIES.includes(value.category as RequirementCategory)
    && typeof value.mandatory === 'boolean'
    && REQUIREMENT_KINDS.includes(value.kind as RequirementKind)
    && typeof value.created_at === 'string';
}

function isEvidenceRecord(value: unknown): value is EvidenceRecord {
  return isRecord(value)
    && typeof value.id === 'string'
    && isNullableString(value.requirement_id)
    && typeof value.document_id === 'string'
    && typeof value.document_sha256 === 'string'
    && typeof value.start_byte === 'number'
    && typeof value.end_byte === 'number'
    && typeof value.excerpt === 'string'
    && isRecord(value.provenance)
    && typeof value.provenance.document_id === 'string'
    && typeof value.provenance.document_sha256 === 'string'
    && typeof value.provenance.document_version === 'number'
    && typeof value.provenance.filename === 'string'
    && typeof value.provenance.start_byte === 'number'
    && typeof value.provenance.end_byte === 'number'
    && typeof value.provenance.retrieved_at === 'string'
    && typeof value.provenance.integrity_check === 'string'
    && typeof value.provenance.source_authenticity === 'string'
    && typeof value.created_at === 'string';
}

function isAmendmentRecord(value: unknown): value is AmendmentRecord {
  return isRecord(value)
    && typeof value.id === 'string'
    && typeof value.document_id === 'string'
    && isNullableString(value.predecessor_id)
    && AMENDMENT_RELATIONSHIPS.includes(value.relationship as AmendmentRelationship)
    && typeof value.created_at === 'string';
}

function parseCollection<T>(value: unknown, valid: (item: unknown) => item is T, label: string): T[] {
  const items = isRecord(value) ? value.items : null;
  if (!Array.isArray(items) || !items.every(valid)) {
    throw new ApiError('INVALID_RESPONSE', `The response did not match the expected ${label} collection schema.`);
  }
  return items;
}

function tenderResource(tenderId: string, resource: string): string {
  if (!tenderId) throw new ApiError('INVALID_RESPONSE', 'A tender id is required.');
  return `/api/tenders/${encodeURIComponent(tenderId)}/${resource}`;
}

export async function loadWorkspaceSession(apiBase = base): Promise<WorkspaceSession> {
  const value = await request<unknown>('/api/auth/session', apiBase);
  if (!isRecord(value)
    || value.authenticated !== true
    || (value.mode !== 'public' && value.mode !== 'owner')
    || typeof value.owner_sign_in !== 'boolean'
    || typeof value.can_read !== 'boolean'
    || typeof value.can_register !== 'boolean'
    || typeof value.can_write_privileged !== 'boolean') {
    throw new ApiError('INVALID_RESPONSE', 'The workspace access response did not match the documented session schema.');
  }
  return value as WorkspaceSession;
}

export async function loginWorkspaceOwner(password: string, apiBase = base): Promise<void> {
  const value = await request<unknown>('/api/auth/login', apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (!isRecord(value) || value.authenticated !== true || value.mode !== 'owner') {
    throw new ApiError('INVALID_RESPONSE', 'The workspace did not confirm an owner session.');
  }
}

export async function logoutWorkspaceOwner(apiBase = base): Promise<void> {
  const value = await request<unknown>('/api/auth/logout', apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  if (!isRecord(value) || value.authenticated !== true || value.mode !== 'public') {
    throw new ApiError('INVALID_RESPONSE', 'The workspace did not confirm the sign-out.');
  }
}

export async function loadTenderDocuments(tenderId: string, apiBase = base): Promise<DocumentRecord[]> {
  const value = await request<unknown>(tenderResource(tenderId, 'documents'), apiBase);
  return parseCollection(value, isDocumentRecord, 'document');
}

function documentMime(file: File): string {
  const extension = file.name.split('.').pop()?.toLowerCase();
  if (extension === 'txt') return 'text/plain';
  if (extension === 'pdf') return 'application/pdf';
  if (extension === 'docx') return DOCX_MIME;
  throw new ApiError('INVALID_RESPONSE', 'Choose a .txt, .pdf, or .docx source file.');
}

export async function uploadTenderDocument(
  tenderId: string,
  file: File,
  groupId: string,
  supersedesDocumentId?: string,
  apiBase = base,
): Promise<DocumentRecord> {
  if (file.size < 1 || file.size > MAX_DOCUMENT_UPLOAD_BYTES) {
    throw new ApiError('INVALID_RESPONSE', 'Source files must be between 1 byte and 20 MiB.');
  }
  if (!groupId.trim() || groupId.trim().length > 120) {
    throw new ApiError('INVALID_RESPONSE', 'Enter a document group label of at most 120 characters.');
  }
  const headers = new Headers({
    'Content-Type': documentMime(file),
    'X-Filename': encodeURIComponent(file.name),
    'X-Document-Group': encodeURIComponent(groupId.trim()),
  });
  if (supersedesDocumentId) headers.set('X-Supersedes-Document-Id', supersedesDocumentId);
  const value = await request<unknown>(tenderResource(tenderId, 'documents'), apiBase, {
    method: 'POST',
    headers,
    body: file,
  }, REGISTRATION_TIMEOUT_MS);
  if (!isDocumentRecord(value)) throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid document version.');
  return value;
}

export async function loadTenderRequirements(tenderId: string, apiBase = base): Promise<Requirement[]> {
  const value = await request<unknown>(tenderResource(tenderId, 'requirements'), apiBase);
  return parseCollection(value, isRequirement, 'requirement');
}

export async function createTenderRequirement(
  tenderId: string,
  input: Omit<Requirement, 'id' | 'created_at'>,
  apiBase = base,
): Promise<Requirement> {
  const value = await request<unknown>(tenderResource(tenderId, 'requirements'), apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!isRequirement(value)) throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid requirement.');
  return value;
}

export async function loadTenderEvidence(tenderId: string, apiBase = base): Promise<EvidenceRecord[]> {
  const value = await request<unknown>(tenderResource(tenderId, 'evidence'), apiBase);
  return parseCollection(value, isEvidenceRecord, 'evidence');
}

export async function createTenderEvidence(
  tenderId: string,
  input: { document_id: string; requirement_id: string | null; start_byte: number; end_byte: number },
  apiBase = base,
): Promise<EvidenceRecord> {
  const value = await request<unknown>(tenderResource(tenderId, 'evidence'), apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!isEvidenceRecord(value)) throw new ApiError('INVALID_RESPONSE', 'The service returned invalid evidence provenance.');
  return value;
}

export async function loadTenderAmendments(tenderId: string, apiBase = base): Promise<AmendmentRecord[]> {
  const value = await request<unknown>(tenderResource(tenderId, 'amendments'), apiBase);
  return parseCollection(value, isAmendmentRecord, 'amendment');
}

export async function createTenderAmendment(
  tenderId: string,
  input: { document_id: string; predecessor_id: string | null; relationship: AmendmentRelationship },
  apiBase = base,
): Promise<AmendmentRecord> {
  const value = await request<unknown>(tenderResource(tenderId, 'amendments'), apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!isAmendmentRecord(value)) throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid amendment annotation.');
  return value;
}

export async function loadEvaluationSnapshots(tenderId: string, apiBase = base): Promise<EvaluationSnapshotSummary[]> {
  const value = await request<unknown>(tenderResource(tenderId, 'evaluation-snapshots'), apiBase);
  return parseCollection(value, (item): item is EvaluationSnapshotSummary => isRecord(item)
    && typeof item.id === 'string'
    && typeof item.input_sha256 === 'string'
    && typeof item.created_at === 'string'
    && typeof item.status === 'string', 'evaluation snapshot');
}

export async function prepareEvaluationSnapshot(
  tenderId: string,
  evidenceIds: string[],
  aggregationPolicy: AggregationPolicy,
  apiBase = base,
): Promise<EvaluationSnapshot> {
  if (!AGGREGATION_POLICIES.includes(aggregationPolicy)) {
    throw new ApiError('INVALID_RESPONSE', 'Choose a supported evaluation aggregation policy.');
  }
  const value = await request<unknown>(tenderResource(tenderId, 'evaluation-snapshots'), apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ evidence_ids: evidenceIds, aggregation_policy: aggregationPolicy }),
  }, REGISTRATION_TIMEOUT_MS);
  if (!isRecord(value)
    || typeof value.snapshot_id !== 'string'
    || typeof value.input_sha256 !== 'string'
    || typeof value.created_at !== 'string'
    || typeof value.status !== 'string'
    || !isRecord(value.submission)) {
    throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid evaluation snapshot.');
  }
  return value as EvaluationSnapshot;
}

export type GenLayerContractStatus = {
  address: string | null;
  code_present: boolean;
  lifecycle_status: string | null;
  probe_submission: boolean;
  result_withheld?: boolean;
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
  deployment: { enabled: boolean; signer_configured: boolean; redeploy_allowed: boolean; broadcast_authorized: boolean; blockers: string[] };
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
      probe_submission: contract.probe_submission === true,
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
      broadcast_authorized: deployment.broadcast_authorized === true,
      blockers: Array.isArray(deployment.blockers) ? deployment.blockers.filter((item): item is string => typeof item === 'string') : [],
    },
    error,
    authenticated: result.authenticated === true,
  };
}
