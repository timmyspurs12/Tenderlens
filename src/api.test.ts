import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError, asArray, createTender, createTenderAmendment, createTenderEvidence, createTenderRequirement, isRecord, isStartupRetryable,
  loadEvaluationSnapshots, loadTender, loadTenderAmendments, loadTenderDocuments, loadTenderEvidence, loadTenderRequirements,
  loadTenderWithWakeup, loadTenders, loadTendersWithWakeup, loadWorkspaceSession, loginWorkspaceOwner, logoutWorkspaceOwner,
  prepareEvaluationSnapshot, request, statusForApiError, uploadTenderDocument, WAKE_BUDGETS_MS,
} from './api';

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

  it('classifies only cold-start-shaped failures as retryable', () => {
    expect(isStartupRetryable(new ApiError('TIMEOUT', 'slow'))).toBe(true);
    expect(isStartupRetryable(new ApiError('NETWORK_ERROR', 'offline'))).toBe(true);
    expect(isStartupRetryable(new ApiError('HTTP_ERROR', 'gateway', 502))).toBe(true);
    expect(isStartupRetryable(new ApiError('HTTP_ERROR', 'gateway', 503))).toBe(true);
    expect(isStartupRetryable(new ApiError('HTTP_ERROR', 'gateway', 504))).toBe(true);
    expect(isStartupRetryable(new ApiError('HTTP_ERROR', 'missing', 404))).toBe(false);
    expect(isStartupRetryable(new ApiError('AUTH_REQUIRED', 'refused', 401))).toBe(false);
    expect(isStartupRetryable(new ApiError('INVALID_RESPONSE', 'bad json'))).toBe(false);
    expect(isStartupRetryable(new Error('unrelated'))).toBe(false);
  });

  it('keeps every waking budget under the gateway upstream ceiling', () => {
    expect(WAKE_BUDGETS_MS.length).toBeGreaterThan(1);
    for (const budget of WAKE_BUDGETS_MS) expect(budget).toBeLessThanOrEqual(50_000);
  });

  it('retries a sleeping records host and reports the wake before the second attempt', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('fetch failed'))
      .mockResolvedValueOnce(jsonResponse({ items: [record] }));
    vi.stubGlobal('fetch', fetchMock);
    const onWake = vi.fn();
    await expect(loadTendersWithWakeup('https://records.example', { onWake })).resolves.toEqual([record]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onWake).toHaveBeenCalledWith(1);
  });

  it('does not retry a refusal or a missing record', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}, 401));
    vi.stubGlobal('fetch', fetchMock);
    const onWake = vi.fn();
    await expect(loadTendersWithWakeup('https://records.example', { onWake })).rejects.toMatchObject({ code: 'AUTH_REQUIRED' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onWake).not.toHaveBeenCalled();
  });

  it('gives up after the budgets are exhausted and rethrows the last failure', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    vi.stubGlobal('fetch', fetchMock);
    const onWake = vi.fn();
    await expect(loadTendersWithWakeup('https://records.example', { onWake, budgets: [1, 1] })).rejects.toMatchObject({ code: 'NETWORK_ERROR' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onWake).toHaveBeenCalledTimes(1);
  });

  it('wakes before reading a single tender record', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('fetch failed'))
      .mockResolvedValueOnce(jsonResponse(record));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadTenderWithWakeup('tender-1', 'https://records.example')).resolves.toEqual(record);
    expect(fetchMock.mock.calls[1][0]).toBe('https://records.example/api/tenders/tender-1');
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

  it('loads strict source, requirement, evidence, and amendment collections', async () => {
    const document = {
      id: 'doc-1', group_id: 'tender-pack', version: 2, filename: 'notice.txt', content_type: 'text/plain',
      size: 12, sha256: 'a'.repeat(64), supersedes_document_id: 'doc-0', created_at: '2026-10-23T00:00:00Z',
    };
    const requirement = {
      id: 'req-1', code: 'ELIG-01', text: 'Hold a current license.', category: 'eligibility',
      mandatory: true, kind: 'objective', created_at: '2026-10-23T00:00:00Z',
    };
    const evidence = {
      id: 'evidence-1', requirement_id: 'req-1', document_id: 'doc-1', document_sha256: 'a'.repeat(64),
      start_byte: 0, end_byte: 6, excerpt: 'License', created_at: '2026-10-23T00:00:00Z',
      provenance: {
        document_id: 'doc-1', document_sha256: 'a'.repeat(64), document_version: 2, filename: 'notice.txt',
        start_byte: 0, end_byte: 6, retrieved_at: '2026-10-23T00:00:00Z', integrity_check: 'sha256_match',
        source_authenticity: 'not_independently_verified',
      },
    };
    const amendment = {
      id: 'amendment-1', document_id: 'doc-1', predecessor_id: 'doc-0', relationship: 'explicit_replacement',
      created_at: '2026-10-23T00:00:00Z',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ items: [document] }))
      .mockResolvedValueOnce(jsonResponse({ items: [requirement] }))
      .mockResolvedValueOnce(jsonResponse({ items: [evidence] }))
      .mockResolvedValueOnce(jsonResponse({ items: [amendment], comparison_status: 'unavailable_without_verified_comparison_engine' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadTenderDocuments('tender-1', 'https://records.example')).resolves.toEqual([document]);
    await expect(loadTenderRequirements('tender-1', 'https://records.example')).resolves.toEqual([requirement]);
    await expect(loadTenderEvidence('tender-1', 'https://records.example')).resolves.toEqual([evidence]);
    await expect(loadTenderAmendments('tender-1', 'https://records.example')).resolves.toEqual([amendment]);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      'https://records.example/api/tenders/tender-1/documents',
      'https://records.example/api/tenders/tender-1/requirements',
      'https://records.example/api/tenders/tender-1/evidence',
      'https://records.example/api/tenders/tender-1/amendments',
    ]);
  });

  it('uses the owner session cookie flow without adding a browser bearer token', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, mode: 'public', owner_sign_in: true, can_read: true, can_register: true, can_write_privileged: false }))
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, mode: 'owner' }))
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, mode: 'public' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadWorkspaceSession('https://records.example')).resolves.toMatchObject({ mode: 'public', owner_sign_in: true });
    await expect(loginWorkspaceOwner('private passphrase', 'https://records.example')).resolves.toBeUndefined();
    await expect(logoutWorkspaceOwner('https://records.example')).resolves.toBeUndefined();
    for (const [, options] of fetchMock.mock.calls) {
      const headers = new Headers(options.headers);
      expect(options.credentials).toBe('include');
      expect(headers.has('Authorization')).toBe(false);
    }
    expect(fetchMock.mock.calls[1][1].body).toBe(JSON.stringify({ password: 'private passphrase' }));
  });

  it('uploads raw file bytes with encoded metadata and refuses unsupported files', async () => {
    const uploaded = {
      id: 'doc-1', group_id: 'tender pack', version: 1, filename: 'notice café.txt', content_type: 'text/plain',
      size: 5, sha256: 'b'.repeat(64), supersedes_document_id: null, created_at: '2026-10-23T00:00:00Z',
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(uploaded, 201));
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['hello'], 'notice café.txt', { type: 'text/plain' });
    await expect(uploadTenderDocument('tender-1', file, 'tender pack', undefined, 'https://records.example')).resolves.toEqual(uploaded);
    const [url, options] = fetchMock.mock.calls[0];
    const headers = new Headers(options.headers);
    expect(url).toBe('https://records.example/api/tenders/tender-1/documents');
    expect(headers.get('Content-Type')).toBe('text/plain');
    expect(headers.get('X-Filename')).toBe(encodeURIComponent('notice café.txt'));
    expect(headers.get('X-Document-Group')).toBe(encodeURIComponent('tender pack'));
    expect(options.body).toBe(file);
    expect(options.credentials).toBe('include');
    await expect(uploadTenderDocument('tender-1', new File(['x'], 'virus.exe'), 'group', undefined, 'https://records.example')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
  });

  it('writes requirement and evidence records, and prepares snapshots without submission', async () => {
    const requirement = {
      id: 'req-1', code: 'ELIG-01', text: 'Hold a license', category: 'eligibility', mandatory: true,
      kind: 'objective', created_at: '2026-10-23T00:00:00Z',
    };
    const evidence = {
      id: 'ev-1', requirement_id: 'req-1', document_id: 'doc-1', document_sha256: 'c'.repeat(64),
      start_byte: 0, end_byte: 7, excerpt: 'License', created_at: '2026-10-23T00:00:00Z',
      provenance: {
        document_id: 'doc-1', document_sha256: 'c'.repeat(64), document_version: 1, filename: 'notice.txt',
        start_byte: 0, end_byte: 7, retrieved_at: '2026-10-23T00:00:00Z', integrity_check: 'sha256_match',
        source_authenticity: 'not_independently_verified',
      },
    };
    const amendment = {
      id: 'am-1', document_id: 'doc-1', predecessor_id: 'doc-0', relationship: 'clarification', created_at: '2026-10-23T00:00:00Z',
    };
    const snapshot = {
      snapshot_id: 'snapshot-1', input_sha256: 'd'.repeat(64), created_at: '2026-10-23T00:00:00Z',
      status: 'prepared_not_submitted', submission: { schema_version: 'tenderlens.evaluation-submission.v1' },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(requirement, 201))
      .mockResolvedValueOnce(jsonResponse(evidence, 201))
      .mockResolvedValueOnce(jsonResponse(amendment, 201))
      .mockResolvedValueOnce(jsonResponse(snapshot, 201))
      .mockResolvedValueOnce(jsonResponse({ items: [{ id: snapshot.snapshot_id, input_sha256: snapshot.input_sha256, created_at: snapshot.created_at, status: snapshot.status }] }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(createTenderRequirement('tender-1', {
      code: 'ELIG-01', text: 'Hold a license', category: 'eligibility', mandatory: true, kind: 'objective',
    }, 'https://records.example')).resolves.toEqual(requirement);
    await expect(createTenderEvidence('tender-1', {
      document_id: 'doc-1', requirement_id: 'req-1', start_byte: 0, end_byte: 7,
    }, 'https://records.example')).resolves.toEqual(evidence);
    await expect(createTenderAmendment('tender-1', {
      document_id: 'doc-1', predecessor_id: 'doc-0', relationship: 'clarification',
    }, 'https://records.example')).resolves.toEqual(amendment);
    await expect(prepareEvaluationSnapshot('tender-1', ['ev-1'], 'mandatory_gate_optional_advisory_v1', 'https://records.example')).resolves.toEqual(snapshot);
    await expect(loadEvaluationSnapshots('tender-1', 'https://records.example')).resolves.toMatchObject([{ id: 'snapshot-1', status: 'prepared_not_submitted' }]);
    expect(fetchMock.mock.calls.map(([, options]) => options.method)).toEqual(['POST', 'POST', 'POST', 'POST', undefined]);
    expect(fetchMock.mock.calls[3][1].body).toBe(JSON.stringify({ evidence_ids: ['ev-1'], aggregation_policy: 'mandatory_gate_optional_advisory_v1' }));
  });
});
