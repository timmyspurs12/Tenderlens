import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { rebuildPath } from '../../api/tender-record.js';

describe('single tender route rewrite', () => {
  it('rebuilds the canonical upstream path from the rewritten query', () => {
    const id = '1a65ccdb-b0bf-4327-afb8-dea98d16af95';
    assert.equal(rebuildPath(`/api/tender-record?id=${id}`).path, `/api/tenders/${id}`);
    assert.equal(rebuildPath(`/api/tender-record?id=${id}&rest=documents`).path, `/api/tenders/${id}/documents`);
    assert.equal(rebuildPath('/api/tender-record?id=').path, '/api/tenders');
  });

  it('refuses anything that is not a uuid or a known resource', () => {
    for (const url of [
      '/api/tender-record?id=../../admin',
      '/api/tender-record?id=1a65ccdb',
      '/api/tender-record?id=1a65ccdb-b0bf-4327-afb8-dea98d16af95&rest=../../admin',
      '/api/tender-record?id=1a65ccdb-b0bf-4327-afb8-dea98d16af95&rest=secrets',
    ]) {
      assert.equal(rebuildPath(url), null, url);
    }
  });

  it('accepts every resource the backend serves', () => {
    const id = '1a65ccdb-b0bf-4327-afb8-dea98d16af95';
    for (const rest of ['documents', 'amendments', 'requirements', 'evidence', 'report', 'evaluation', 'evaluation-snapshots']) {
      assert.ok(rebuildPath(`/api/tender-record?id=${id}&rest=${rest}`), rest);
    }
  });
});
