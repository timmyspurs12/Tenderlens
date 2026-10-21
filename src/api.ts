export type ApiStatus = 'loading' | 'ready' | 'unavailable' | 'invalid' | 'unauthorized' | 'not_configured';
export type Tender = { id: string; title: string; reference: string; deadline?: string | null; created_at?: string; status?: string };
export type DocumentRecord = { id: string; title: string; version?: string; status?: string };
export type Requirement = { id: string; text: string; category?: string; mandatory?: boolean; status?: string };

const configuredBase = (import.meta as ImportMeta & { env: Record<string, string | undefined> }).env.VITE_API_BASE_URL;
const base = configuredBase?.trim().replace(/\/+$/, '');
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
  if (!apiBase) throw new ApiError('API_NOT_CONFIGURED', 'API_NOT_CONFIGURED');
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

export async function createTender(input: { title: string; reference: string; deadline?: string }, apiBase = base): Promise<Tender> {
  const result = await request<unknown>('/api/tenders', apiBase, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!isTender(result)) throw new ApiError('INVALID_RESPONSE', 'The service returned an invalid tender record.');
  return result;
}
