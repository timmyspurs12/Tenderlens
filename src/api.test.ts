import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, asArray, createTender, isRecord, loadTender, loadTenders, request, statusForApiError } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const record = { id: 'tender-1', title: 'Published tender', reference: 'REF-2026-01' };
const jsonResponse = (body: unknown, status = 200): Response => ({
  ok: status >= 200 && status < 300,
  status,
  json: vi.fn().mockResolvedValue(body),
}) as unknown as Response;

describe('workspace API client', () => {
  it('recognizes only non-array records and arrays', () => {
    expect(isRecord({ id: 'x' })).toBe(true);
    expect(isRecord(null)).toBe(false);
    expect(isRecord([])).toBe(false);
    expect(asArray([1])).toEqual([1]);
    expect(asArray({ items: [] })).toBeNull();
  });

  it('loads the documented items envelope and normalizes the backend origin', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [record] }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadTenders('https://records.example///')).resolves.toEqual([record]);
    expect(fetchMock).toHaveBeenCalledWith('https://records.example/api/tenders', expect.objectContaining({
      credentials: 'include',
    }));
    const [, options] = fetchMock.mock.calls[0];
    const headers = new Headers(options.headers);
    expect(headers.get('Accept')).toBe('application/json');
    expect(headers.has('Authorization')).toBe(false);
  });

  it('accepts the documented raw array alternative and an empty collection', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(jsonResponse([record])).mockResolvedValueOnce(jsonResponse({ items: [] })));
    await expect(loadTenders('https://records.example')).resolves.toEqual([record]);
    await expect(loadTenders('https://records.example')).resolves.toEqual([]);
  });

  it('rejects malformed backend record responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ items: [{ id: '1', title: 'Missing reference' }] })));
    await expect(loadTenders('https://records.example')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
  });

  it('rejects malformed JSON responses', async () => {
    const malformed = { ok: true, status: 200, json: vi.fn().mockRejectedValue(new SyntaxError('bad json')) } as unknown as Response;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(malformed));
    await expect(loadTenders('https://records.example')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
  });

  it('maps unauthorized and other HTTP responses without retrying anonymously', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({}, 401)).mockResolvedValueOnce(jsonResponse({}, 503));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadTenders('https://records.example')).rejects.toMatchObject({ code: 'AUTH_REQUIRED', status: 401 });
    await expect(loadTenders('https://records.example')).rejects.toMatchObject({ code: 'HTTP_ERROR', status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('reports network failures and request timeouts distinctly', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await expect(request('/api/tenders', 'https://records.example')).rejects.toMatchObject({ code: 'NETWORK_ERROR' });

    vi.stubGlobal('fetch', vi.fn((_url: string, options: RequestInit) => new Promise((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    })));
    await expect(request('/api/tenders', 'https://records.example', {}, 5)).rejects.toMatchObject({ code: 'TIMEOUT' });
  });

  it('requires a configured gateway and maps API states', async () => {
    await expect(request('/api/tenders', '')).rejects.toMatchObject({ code: 'API_NOT_CONFIGURED' });
    expect(statusForApiError(new ApiError('AUTH_REQUIRED', 'sign in'))).toBe('unauthorized');
    expect(statusForApiError(new ApiError('INVALID_RESPONSE', 'bad data'))).toBe('invalid');
    expect(statusForApiError(new ApiError('API_NOT_CONFIGURED', 'configure'))).toBe('not_configured');
  });

  it('loads a single tender by id from the allowlisted route', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(record));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadTender('tender-1', 'https://records.example')).resolves.toEqual(record);
    expect(fetchMock.mock.calls[0][0]).toBe('https://records.example/api/tenders/tender-1');
  });

  it('falls back to the register when the single-record route is not bound', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ error: { code: 'not_found' } }, 404))
      .mockResolvedValueOnce(jsonResponse({ items: [record, { id: 'other', title: 'B', reference: 'R2' }] }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadTender('tender-1', 'https://records.example')).resolves.toEqual(record);
    expect(fetchMock.mock.calls[1][0]).toBe('https://records.example/api/tenders');
  });

  it('reports a missing record when neither route returns it', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ error: { code: 'not_found' } }, 404))
      .mockResolvedValueOnce(jsonResponse({ items: [] }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadTender('tender-9', 'https://records.example')).rejects.toMatchObject({ status: 404 });
  });

  it('encodes the id and rejects a malformed single-tender payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'x' }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadTender('a/b c', 'https://records.example')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
    expect(fetchMock.mock.calls[0][0]).toBe('https://records.example/api/tenders/a%2Fb%20c');
  });

  it('registers a tender through the public gateway', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(record, 201));
    vi.stubGlobal('fetch', fetchMock);
    await expect(createTender({ title: record.title, reference: record.reference }, 'https://records.example/')).resolves.toEqual(record);
    expect(fetchMock).toHaveBeenCalledWith('https://records.example/api/tenders', expect.objectContaining({
      method: 'POST',
      credentials: 'include',
      body: JSON.stringify({ title: record.title, reference: record.reference }),
    }));
  });
});
