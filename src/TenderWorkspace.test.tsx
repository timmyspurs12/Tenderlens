import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DocumentWorkspace, EvaluationWorkspace, RequirementWorkspace } from './TenderWorkspace';
import type { EvidenceRecord, Requirement, WorkspaceSession } from './api';

const api = vi.hoisted(() => ({
  loadWorkspaceSession: vi.fn(),
  loginWorkspaceOwner: vi.fn(),
  logoutWorkspaceOwner: vi.fn(),
  loadTenderDocuments: vi.fn(),
  loadTenderRequirements: vi.fn(),
  loadTenderEvidence: vi.fn(),
  loadTenderAmendments: vi.fn(),
  loadEvaluationSnapshots: vi.fn(),
  createTenderRequirement: vi.fn(),
  createTenderEvidence: vi.fn(),
  createTenderAmendment: vi.fn(),
  prepareEvaluationSnapshot: vi.fn(),
  uploadTenderDocument: vi.fn(),
}));

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api');
  return { ...actual, ...api };
});

const publicSession: WorkspaceSession = {
  authenticated: true,
  mode: 'public',
  owner_sign_in: false,
  can_read: true,
  can_register: true,
  can_write_privileged: false,
};
const ownerSession: WorkspaceSession = { ...publicSession, mode: 'owner', owner_sign_in: true, can_write_privileged: true };
const requirement: Requirement = {
  id: 'req-1', code: 'ELIG-01', text: 'Hold a current published licence.', category: 'eligibility',
  mandatory: true, kind: 'objective', created_at: '2026-10-23T00:00:00Z',
};
const evidence: EvidenceRecord = {
  id: 'evidence-1', requirement_id: 'req-1', document_id: 'doc-1', document_sha256: 'a'.repeat(64),
  start_byte: 0, end_byte: 7, excerpt: 'licence', created_at: '2026-10-23T00:00:00Z',
  provenance: {
    document_id: 'doc-1', document_sha256: 'a'.repeat(64), document_version: 1, filename: 'notice.txt',
    start_byte: 0, end_byte: 7, retrieved_at: '2026-10-23T00:00:00Z', integrity_check: 'sha256_match',
    source_authenticity: 'not_independently_verified',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  api.loadWorkspaceSession.mockResolvedValue(publicSession);
  api.loadTenderDocuments.mockResolvedValue([]);
  api.loadTenderRequirements.mockResolvedValue([]);
  api.loadTenderEvidence.mockResolvedValue([]);
  api.loadTenderAmendments.mockResolvedValue([]);
  api.loadEvaluationSnapshots.mockResolvedValue([]);
  api.createTenderRequirement.mockResolvedValue(requirement);
  api.prepareEvaluationSnapshot.mockResolvedValue({
    snapshot_id: 'snapshot-1',
    input_sha256: 'b'.repeat(64),
    created_at: '2026-10-23T00:00:00Z',
    status: 'prepared_not_submitted',
    submission: { schema_version: 'tenderlens.evaluation-submission.v1' },
  });
});

afterEach(() => {
  cleanup();
});

describe('tender source workspaces', () => {
  it('keeps source uploads disabled when owner sign-in is not configured', async () => {
    api.loadTenderDocuments.mockResolvedValue([{
      id: 'doc-1', group_id: 'tender-pack', version: 1, filename: 'notice.pdf', content_type: 'application/pdf',
      size: 512, sha256: 'a'.repeat(64), supersedes_document_id: null, created_at: '2026-10-23T00:00:00Z',
    }]);
    render(<DocumentWorkspace tenderId="tender-1" />);

    expect(await screen.findByText(/Public read-only access/)).toBeTruthy();
    expect(screen.getByText('notice.pdf')).toBeTruthy();
    expect((screen.getByRole('button', { name: /Save source version/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.uploadTenderDocument).not.toHaveBeenCalled();
  });

  it('uploads a version only after owner sign-in and keeps PDF extraction explicitly unavailable', async () => {
    api.loadWorkspaceSession.mockResolvedValue(ownerSession);
    const document = {
      id: 'doc-1', group_id: 'tender-documents', version: 1, filename: 'notice.pdf', content_type: 'application/pdf',
      size: 9, sha256: 'c'.repeat(64), supersedes_document_id: null, created_at: '2026-10-23T00:00:00Z',
    };
    api.uploadTenderDocument.mockResolvedValue(document);
    render(<DocumentWorkspace tenderId="tender-1" />);

    const file = new File(['pdf bytes'], 'notice.pdf', { type: 'application/pdf' });
    fireEvent.change(await screen.findByLabelText('Source file'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: /Save source version/ }));

    await waitFor(() => expect(api.uploadTenderDocument).toHaveBeenCalledWith('tender-1', file, 'tender-documents', undefined));
    expect(await screen.findByText(/Version 1 saved/)).toBeTruthy();
    expect(screen.getByText(/Passage evidence is not extracted from PDF or DOCX/)).toBeTruthy();
  });

  it('registers a requirement through the owner session and confirms immutable creation', async () => {
    api.loadWorkspaceSession.mockResolvedValue(ownerSession);
    render(<RequirementWorkspace tenderId="tender-1" />);

    fireEvent.change(await screen.findByLabelText('Requirement code'), { target: { value: 'ELIG-01' } });
    fireEvent.change(screen.getByLabelText('Requirement text'), { target: { value: 'Hold a current published licence.' } });
    fireEvent.click(screen.getByRole('button', { name: /Register requirement/ }));

    await waitFor(() => expect(api.createTenderRequirement).toHaveBeenCalledWith('tender-1', {
      code: 'ELIG-01',
      text: 'Hold a current published licence.',
      category: 'eligibility',
      mandatory: true,
      kind: 'objective',
    }));
    expect(await screen.findByText(/Requirement registered/)).toBeTruthy();
  });

  it('prepares an immutable snapshot from explicitly selected evidence without submitting it', async () => {
    api.loadWorkspaceSession.mockResolvedValue(ownerSession);
    api.loadTenderRequirements.mockResolvedValue([requirement]);
    api.loadTenderEvidence.mockResolvedValue([evidence]);
    render(<EvaluationWorkspace tenderId="tender-1" />);

    fireEvent.click(await screen.findByRole('button', { name: /Prepare evaluation snapshot/ }));
    await waitFor(() => expect(api.prepareEvaluationSnapshot).toHaveBeenCalledWith(
      'tender-1', ['evidence-1'], 'mandatory_gate_optional_advisory_v1',
    ));
    expect(await screen.findByText(/Snapshot prepared · not submitted/)).toBeTruthy();
    expect(screen.getByText(/No deployment, GenLayer call, model evaluation, or award decision/)).toBeTruthy();
  });
});
