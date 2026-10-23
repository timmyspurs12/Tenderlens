import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { ArrowRight, Check, FileCheck2, FileText, Fingerprint, LockKeyhole, LogOut, Plus, ShieldCheck, TriangleAlert, Upload } from 'lucide-react';
import {
  ApiError,
  MAX_DOCUMENT_UPLOAD_BYTES,
  createTenderAmendment,
  createTenderEvidence,
  createTenderRequirement,
  loadEvaluationSnapshots,
  loadTenderAmendments,
  loadTenderDocuments,
  loadTenderEvidence,
  loadTenderRequirements,
  loadWorkspaceSession,
  loginWorkspaceOwner,
  logoutWorkspaceOwner,
  prepareEvaluationSnapshot,
  uploadTenderDocument,
  type AggregationPolicy,
  type AmendmentRecord,
  type AmendmentRelationship,
  type DocumentRecord,
  type EvaluationSnapshot,
  type EvaluationSnapshotSummary,
  type EvidenceRecord,
  type Requirement,
  type RequirementCategory,
  type RequirementKind,
  type WorkspaceSession,
} from './api';
import { decodeVerifiedTextFile, selectionToByteRange, type NormalizedSource } from './evidence';

const MAX_SOURCE_SIZE = MAX_DOCUMENT_UPLOAD_BYTES;
const MAX_TEXT_PREVIEW_SIZE = 1024 * 1024;
const CATEGORIES: Array<{ value: RequirementCategory; label: string }> = [
  { value: 'eligibility', label: 'Eligibility' },
  { value: 'mandatory_documentation', label: 'Mandatory documentation' },
  { value: 'technical', label: 'Technical' },
  { value: 'interpretive', label: 'Interpretive clause' },
];
const RELATIONSHIPS: Array<{ value: AmendmentRelationship; label: string }> = [
  { value: 'explicit_replacement', label: 'Explicit replacement' },
  { value: 'conflicting', label: 'Potential conflict' },
  { value: 'clarification', label: 'Clarification' },
  { value: 'unresolved', label: 'Unresolved relationship' },
  { value: 'none', label: 'No relationship asserted' },
];
const POLICIES: Array<{ value: AggregationPolicy; label: string; description: string }> = [
  {
    value: 'mandatory_gate_optional_advisory_v1',
    label: 'Mandatory gate · optional advisory',
    description: 'Mandatory failures block; optional failures remain advisory. Unknown or contradictory evidence stays unresolved.',
  },
  {
    value: 'all_requirements_blocking_v1',
    label: 'All requirements blocking',
    description: 'Any non-compliant requirement is blocking. Unknown or contradictory evidence stays unresolved.',
  },
];

type AsyncState = 'loading' | 'ready' | 'error';
type AccessState = {
  status: AsyncState;
  session: WorkspaceSession | null;
  error: string;
  refresh: () => Promise<void>;
};

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.code === 'API_NOT_CONFIGURED') {
    return 'The workspace gateway is not configured in this build.';
  }
  if (error instanceof ApiError && error.code === 'AUTH_REQUIRED') {
    return 'This action needs the workspace owner session. Sign in above and try again.';
  }
  if (error instanceof ApiError && error.code === 'HTTP_ERROR' && error.status === 413) {
    return 'The upload exceeds the configured backend or hosting-gateway body limit. Try a smaller source file.';
  }
  return error instanceof Error ? error.message : fallback;
}

function useWorkspaceAccess(): AccessState {
  const [status, setStatus] = useState<AsyncState>('loading');
  const [session, setSession] = useState<WorkspaceSession | null>(null);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    setStatus('loading');
    setError('');
    try {
      setSession(await loadWorkspaceSession());
      setStatus('ready');
    } catch (problem) {
      setSession(null);
      setError(messageFor(problem, 'Workspace access could not be checked.'));
      setStatus('error');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { status, session, error, refresh };
}

function OwnerAccessPanel({ access }: { access: AccessState }) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      await loginWorkspaceOwner(password);
      setPassword('');
      await access.refresh();
    } catch (problem) {
      setError(problem instanceof ApiError && problem.code === 'AUTH_REQUIRED'
        ? 'The passphrase was not accepted.'
        : messageFor(problem, 'Owner sign-in failed.'));
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setError('');
    setBusy(true);
    try {
      await logoutWorkspaceOwner();
      await access.refresh();
    } catch (problem) {
      setError(messageFor(problem, 'Sign-out could not be confirmed.'));
    } finally {
      setBusy(false);
    }
  }

  if (access.status === 'loading') {
    return <div className="owner-access" role="status"><span className="source-loader" /> Checking workspace access…</div>;
  }
  if (access.status === 'error' || !access.session) {
    return <div className="owner-access owner-access-warning" role="status">
      <TriangleAlert size={16} />
      <span>{access.error || 'Workspace access is unavailable.'}</span>
      <button className="text-action" type="button" onClick={() => void access.refresh()}>Retry</button>
    </div>;
  }

  if (access.session.can_write_privileged) {
    return <div className="owner-access owner-access-ready" role="status">
      <ShieldCheck size={16} />
      <span><b>Owner session active.</b> Privileged source and snapshot writes are available.</span>
      <button className="text-action" type="button" disabled={busy} onClick={() => void signOut()}>
        <LogOut size={14} /> {busy ? 'Signing out…' : 'Sign out'}
      </button>
      {error && <span className="owner-access-error" role="alert">{error}</span>}
    </div>;
  }

  if (!access.session.owner_sign_in) {
    return <div className="owner-access owner-access-warning" role="status">
      <LockKeyhole size={16} />
      <span><b>Public read-only access.</b> Owner sign-in is not configured for this deployment; uploads and record changes stay disabled.</span>
    </div>;
  }

  return <div className="owner-access owner-access-login">
    <div className="owner-access-copy"><LockKeyhole size={16} /><span><b>Owner access required for changes.</b> Records remain readable in public mode.</span></div>
    <form onSubmit={signIn} className="owner-login-form">
      <label className="visually-hidden" htmlFor="owner-passphrase">Workspace owner passphrase</label>
      <input
        id="owner-passphrase"
        type="password"
        autoComplete="current-password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        placeholder="Owner passphrase"
        required
        minLength={1}
      />
      <button className="button dark compact" type="submit" disabled={busy || !password}>
        {busy ? 'Signing in…' : 'Sign in'} <ArrowRight size={14} />
      </button>
    </form>
    {error && <div className="owner-access-error" role="alert">{error}</div>}
  </div>;
}

function DataState({ state, error, empty, onRetry }: {
  state: AsyncState;
  error: string;
  empty?: { title: string; detail: string };
  onRetry: () => void;
}) {
  if (state === 'loading') {
    return <div className="source-state" role="status"><span className="source-loader" /> Reading the tender record…</div>;
  }
  if (state === 'error') {
    return <div className="source-state source-state-error" role="alert">
      <TriangleAlert size={16} /><span>{error}</span>
      <button className="text-action" type="button" onClick={onRetry}>Retry</button>
    </div>;
  }
  if (empty) {
    return <div className="source-empty"><FileText size={20} /><b>{empty.title}</b><p>{empty.detail}</p></div>;
  }
  return null;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function shortHash(value: string): string {
  return `${value.slice(0, 12)}…${value.slice(-8)}`;
}

function findDocument(documents: DocumentRecord[], id: string | null | undefined): DocumentRecord | undefined {
  return id ? documents.find((document) => document.id === id) : undefined;
}

function canWrite(access: AccessState): boolean {
  return access.status === 'ready' && access.session?.can_write_privileged === true;
}

type SourcePreview = {
  document: DocumentRecord;
  source: string;
  normalized: NormalizedSource;
  bomPrefixBytes: number;
};

export function DocumentWorkspace({ tenderId }: { tenderId: string }) {
  const access = useWorkspaceAccess();
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [evidence, setEvidence] = useState<EvidenceRecord[]>([]);
  const [dataState, setDataState] = useState<AsyncState>('loading');
  const [dataError, setDataError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [file, setFile] = useState<File | null>(null);
  const [group, setGroup] = useState('tender-documents');
  const [predecessorChoice, setPredecessorChoice] = useState('auto');
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [uploadNotice, setUploadNotice] = useState('');
  const [sourceTarget, setSourceTarget] = useState<DocumentRecord | null>(null);
  const [sourcePreview, setSourcePreview] = useState<SourcePreview | null>(null);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceError, setSourceError] = useState('');
  const [evidenceRequirement, setEvidenceRequirement] = useState('');
  const [evidenceBusy, setEvidenceBusy] = useState(false);
  const [evidenceError, setEvidenceError] = useState('');
  const [evidenceNotice, setEvidenceNotice] = useState('');
  const textArea = useRef<HTMLTextAreaElement>(null);
  const uploadFileInput = useRef<HTMLInputElement>(null);
  const localSourceInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let live = true;
    setDataState('loading');
    Promise.all([
      loadTenderDocuments(tenderId),
      loadTenderRequirements(tenderId),
      loadTenderEvidence(tenderId),
    ]).then(([loadedDocuments, loadedRequirements, loadedEvidence]) => {
      if (!live) return;
      setDocuments(loadedDocuments);
      setRequirements(loadedRequirements);
      setEvidence(loadedEvidence);
      setDataError('');
      setDataState('ready');
    }).catch((problem: unknown) => {
      if (!live) return;
      setDataError(messageFor(problem, 'Tender source records could not be loaded.'));
      setDataState('error');
    });
    return () => { live = false; };
  }, [tenderId, reloadKey]);

  const sameGroup = documents
    .filter((document) => document.group_id === group.trim())
    .sort((left, right) => right.version - left.version);
  const latestInGroup = sameGroup[0];
  const supersedesDocumentId = predecessorChoice === 'none'
    ? undefined
    : predecessorChoice === 'auto'
      ? latestInGroup?.id
      : predecessorChoice;

  async function showVerifiedText(document: DocumentRecord, selectedFile: File) {
    setSourceError('');
    setSourcePreview(null);
    setSourceTarget(document);
    if (document.content_type !== 'text/plain') {
      setSourceError('Evidence byte ranges are currently supported for text/plain only. PDF and DOCX files are stored with their hash; no text is extracted or invented.');
      return;
    }
    if (selectedFile.size > MAX_TEXT_PREVIEW_SIZE) {
      setSourceError('Text preview is limited to 1 MiB so the exact passage can be reviewed comfortably. The full source remains stored and hashed.');
      return;
    }
    if (selectedFile.size !== document.size) {
      setSourceError('The selected local file size does not match this stored document version.');
      return;
    }
    setSourceBusy(true);
    try {
      const decoded = await decodeVerifiedTextFile(selectedFile, document.sha256);
      setSourcePreview({
        document,
        source: decoded.source,
        normalized: decoded.normalized,
        bomPrefixBytes: decoded.bomPrefixBytes,
      });
      setEvidenceError('');
      setEvidenceNotice('');
    } catch (problem) {
      setSourceError(messageFor(problem, 'The selected file could not be verified.'));
    } finally {
      setSourceBusy(false);
    }
  }

  function chooseExistingSource(document: DocumentRecord) {
    setSourceError('');
    setSourcePreview(null);
    setSourceTarget(document);
    if (document.content_type !== 'text/plain') {
      setSourceError('Evidence byte ranges are currently supported for text/plain only. PDF and DOCX files are stored with their hash; no text is extracted or invented.');
      return;
    }
    localSourceInput.current?.click();
  }

  async function onLocalSourceChange(event: ChangeEvent<HTMLInputElement>) {
    const selectedFile = event.currentTarget.files?.[0];
    event.currentTarget.value = '';
    if (selectedFile && sourceTarget) await showVerifiedText(sourceTarget, selectedFile);
  }

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setUploadError('');
    setUploadNotice('');
    if (!canWrite(access)) {
      setUploadError('Sign in with the workspace owner session before uploading a source.');
      return;
    }
    if (!file) {
      setUploadError('Choose a source file first.');
      return;
    }
    if (file.size > MAX_SOURCE_SIZE || file.size < 1) {
      setUploadError('Source files must be between 1 byte and 20 MiB.');
      return;
    }
    setUploadBusy(true);
    try {
      const created = await uploadTenderDocument(tenderId, file, group, supersedesDocumentId);
      setReloadKey((value) => value + 1);
      setUploadNotice(`Version ${created.version} saved. SHA-256 ${created.sha256}.`);
      setFile(null);
      if (uploadFileInput.current) uploadFileInput.current.value = '';
      if (created.content_type === 'text/plain') {
        await showVerifiedText(created, file);
      } else {
        setSourceTarget(created);
        setSourcePreview(null);
        setSourceError('This version is stored and hashed. Passage evidence is not extracted from PDF or DOCX files.');
      }
    } catch (problem) {
      setUploadError(messageFor(problem, 'The source upload failed.'));
    } finally {
      setUploadBusy(false);
    }
  }

  async function captureEvidence(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setEvidenceError('');
    setEvidenceNotice('');
    if (!canWrite(access)) {
      setEvidenceError('Sign in with the workspace owner session before saving evidence.');
      return;
    }
    if (!sourcePreview || !textArea.current) {
      setEvidenceError('Load and verify the exact text source before capturing evidence.');
      return;
    }
    try {
      const range = selectionToByteRange(
        sourcePreview.source,
        sourcePreview.normalized,
        textArea.current.selectionStart,
        textArea.current.selectionEnd,
        sourcePreview.bomPrefixBytes,
      );
      setEvidenceBusy(true);
      const created = await createTenderEvidence(tenderId, {
        document_id: sourcePreview.document.id,
        requirement_id: evidenceRequirement || null,
        start_byte: range.startByte,
        end_byte: range.endByte,
      });
      setEvidence((current) => [...current, created]);
      setEvidenceNotice(`Evidence saved at UTF-8 bytes ${created.start_byte}–${created.end_byte}. The server verified the source hash and passage range.`);
      textArea.current?.focus();
    } catch (problem) {
      setEvidenceError(messageFor(problem, 'Evidence could not be verified and saved.'));
    } finally {
      setEvidenceBusy(false);
    }
  }

  const pageState = <DataState state={dataState} error={dataError} onRetry={() => setReloadKey((value) => value + 1)} />;
  const sourceEvidence = sourcePreview ? evidence.filter((item) => item.document_id === sourcePreview.document.id) : [];

  return <div className="source-workspace">
    <OwnerAccessPanel access={access} />
    <div className="source-intro-card">
      <div className="source-intro-icon"><Fingerprint size={18} /></div>
      <div><b>Immutable source versions with verifiable provenance</b><p>Every upload receives a server-calculated SHA-256 and a new version record. Revisions do not overwrite earlier documents. This workspace is public-read: use published materials only, never confidential bids or private attachments.</p></div>
      <span className="source-limit">Backend max · 20 MiB</span>
    </div>

    <section className="source-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">01 / REGISTER SOURCE</span><h3>Add a document version</h3><p>Supported files: TXT, PDF, DOCX. The app does not extract passages from PDF or DOCX.</p></div><Upload size={17} /></header>
      <form className="source-form" onSubmit={upload}>
        <label>Source file<input ref={uploadFileInput} type="file" accept=".txt,.pdf,.docx,text/plain,application/pdf" onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)} /></label>
        <div className="source-form-row">
          <label>Document group<input value={group} onChange={(event) => { setGroup(event.target.value); setPredecessorChoice('auto'); }} maxLength={120} required placeholder="e.g. tender-pack" /></label>
          <label>Version predecessor<select value={predecessorChoice} onChange={(event) => setPredecessorChoice(event.target.value)}>
            <option value="auto">Link latest in this group{latestInGroup ? ` · v${latestInGroup.version}` : ' · none yet'}</option>
            <option value="none">Start a new chain in this group</option>
            {sameGroup.map((document) => <option key={document.id} value={document.id}>v{document.version} · {document.filename}</option>)}
          </select></label>
        </div>
        {file && <div className="selected-file"><FileText size={15} /><span><b>{file.name}</b><small>{formatBytes(file.size)} · {file.type || 'type inferred from filename'}</small></span></div>}
        {uploadError && <div className="inline-alert" role="alert"><TriangleAlert size={15} />{uploadError}</div>}
        {uploadNotice && <div className="inline-success" role="status"><Check size={15} />{uploadNotice}</div>}
        <div className="source-form-footer"><span>Uploads require the optional owner session. The backend stores file bytes outside the browser and locks each version against edits.</span>
          <button className="button dark" type="submit" disabled={!canWrite(access) || !file || uploadBusy}>{uploadBusy ? 'Saving version…' : 'Save source version'} <ArrowRight size={14} /></button>
        </div>
      </form>
    </section>

    <section className="source-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">02 / VERSION HISTORY</span><h3>Registered source files</h3><p>Version labels and predecessor links come from the records service; source authenticity is not independently verified.</p></div><FileCheck2 size={17} /></header>
      {pageState}
      {dataState === 'ready' && documents.length === 0 && <DataState state="ready" error="" empty={{ title: 'No source versions yet', detail: 'Upload a published tender document to begin a source record.' }} onRetry={() => setReloadKey((value) => value + 1)} />}
      {dataState === 'ready' && documents.length > 0 && <div className="source-document-list">
        {documents.map((document) => {
          const predecessor = findDocument(documents, document.supersedes_document_id);
          return <article className="source-document-row" key={document.id}>
            <div className="document-file-icon"><FileText size={17} /></div>
            <div className="source-document-info"><div className="source-document-title"><b>{document.filename}</b><span className="document-version">v{document.version}</span></div>
              <small>{document.group_id}{predecessor ? ` · follows v${predecessor.version} ${predecessor.filename}` : ' · no predecessor linked'}</small>
              <code title={document.sha256}>SHA-256 {shortHash(document.sha256)}</code>
            </div>
            <div className="source-document-meta"><span>{formatBytes(document.size)}</span><small>{formatDate(document.created_at)}</small>
              {document.content_type === 'text/plain'
                ? <button className="text-action" type="button" onClick={() => chooseExistingSource(document)}>Review text &amp; attach evidence</button>
                : <span className="muted-action">No text extraction</span>}
            </div>
          </article>;
        })}
      </div>}
    </section>

    <input ref={localSourceInput} className="visually-hidden" type="file" accept=".txt,text/plain" onChange={(event) => void onLocalSourceChange(event)} aria-label="Choose the exact uploaded text source" />
    {(sourceBusy || sourceError || sourcePreview) && <section className="source-panel source-review-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">03 / REVIEW EXACT TEXT</span><h3>{sourceTarget?.filename || 'Source passage'}</h3><p>{sourcePreview ? `Verified SHA-256 ${shortHash(sourcePreview.document.sha256)} · version ${sourcePreview.document.version}` : sourceTarget?.content_type === 'text/plain' ? 'Select the exact local TXT file for the stored version. It is checked against the server hash before display.' : 'TXT passage extraction only; PDF and DOCX versions are stored and hashed without text parsing.'}</p></div><ShieldCheck size={17} /></header>
      {sourceBusy && <div className="source-state" role="status"><span className="source-loader" />Checking file hash and decoding UTF-8 text…</div>}
      {sourceError && <div className="inline-alert" role="alert"><TriangleAlert size={15} />{sourceError}</div>}
      {sourcePreview && <>
        <p className="source-review-hint">Select a passage in the read-only text below. Byte offsets are calculated against the original UTF-8 file, including line endings and multi-byte characters. The service rechecks the range against its stored file.</p>
        <textarea ref={textArea} className="verified-source-text" readOnly value={sourcePreview.normalized.text} aria-label="Verified source text; select a passage to cite" />
        <form className="evidence-capture-form" onSubmit={captureEvidence}>
          <label>Attach passage to a requirement<select value={evidenceRequirement} onChange={(event) => setEvidenceRequirement(event.target.value)}>
            <option value="">General context · no single requirement</option>
            {requirements.map((requirement) => <option key={requirement.id} value={requirement.id}>{requirement.code} · {requirement.text.slice(0, 100)}</option>)}
          </select></label>
          {evidenceError && <div className="inline-alert" role="alert"><TriangleAlert size={15} />{evidenceError}</div>}
          {evidenceNotice && <div className="inline-success" role="status"><Check size={15} />{evidenceNotice}</div>}
          <button className="button dark" type="submit" disabled={!canWrite(access) || evidenceBusy}>{evidenceBusy ? 'Verifying passage…' : 'Save selected passage as evidence'} <ArrowRight size={14} /></button>
        </form>
        {sourceEvidence.length > 0 && <div className="source-evidence-list"><h4>Evidence already attached to this version</h4>{sourceEvidence.map((item) => <EvidenceCard key={item.id} evidence={item} documents={documents} requirements={requirements} />)}</div>}
      </>}
    </section>}
  </div>;
}

export function RequirementWorkspace({ tenderId }: { tenderId: string }) {
  const access = useWorkspaceAccess();
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [state, setState] = useState<AsyncState>('loading');
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [code, setCode] = useState('');
  const [text, setText] = useState('');
  const [category, setCategory] = useState<RequirementCategory>('eligibility');
  const [mandatory, setMandatory] = useState(true);
  const [kind, setKind] = useState<RequirementKind>('objective');
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let live = true;
    setState('loading');
    loadTenderRequirements(tenderId).then((items) => {
      if (!live) return;
      setRequirements(items);
      setError('');
      setState('ready');
    }).catch((problem: unknown) => {
      if (!live) return;
      setError(messageFor(problem, 'Requirement records could not be loaded.'));
      setState('error');
    });
    return () => { live = false; };
  }, [tenderId, reloadKey]);

  async function addRequirement(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError('');
    setNotice('');
    if (!canWrite(access)) {
      setFormError('Sign in with the workspace owner session before registering requirements.');
      return;
    }
    setBusy(true);
    try {
      await createTenderRequirement(tenderId, { code: code.trim(), text: text.trim(), category, mandatory, kind });
      setCode('');
      setText('');
      setNotice('Requirement registered. Existing requirements remain immutable.');
      setReloadKey((value) => value + 1);
    } catch (problem) {
      setFormError(messageFor(problem, 'Requirement could not be registered.'));
    } finally {
      setBusy(false);
    }
  }

  return <div className="source-workspace">
    <OwnerAccessPanel access={access} />
    <div className="source-intro-card"><div className="source-intro-icon"><ShieldCheck size={18} /></div><div><b>Register the criteria before assessing evidence</b><p>Requirements are fixed into a later snapshot. This tool records the criterion as provided; it does not interpret procurement rules or decide an award.</p></div></div>
    <section className="source-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">01 / REQUIREMENT REGISTER</span><h3>Registered requirements</h3><p>Code, category, mandatory flag, and assessment kind are returned by the tender service.</p></div><FileCheck2 size={17} /></header>
      <DataState state={state} error={error} onRetry={() => setReloadKey((value) => value + 1)} />
      {state === 'ready' && requirements.length === 0 && <DataState state="ready" error="" empty={{ title: 'No requirements registered', detail: 'Add the published criterion exactly as written, with an internal code and category.' }} onRetry={() => setReloadKey((value) => value + 1)} />}
      {state === 'ready' && requirements.length > 0 && <div className="requirement-list">{requirements.map((requirement) => <RequirementCard key={requirement.id} requirement={requirement} />)}</div>}
    </section>
    <section className="source-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">02 / ADD CRITERION</span><h3>Register a requirement</h3><p>The service assigns an immutable identifier. Duplicate codes for this tender are rejected.</p></div><Plus size={17} /></header>
      <form className="source-form requirement-form" onSubmit={addRequirement}>
        <div className="source-form-row">
          <label>Requirement code<input required maxLength={80} value={code} onChange={(event) => setCode(event.target.value)} placeholder="e.g. ELIG-01" /></label>
          <label>Category<select value={category} onChange={(event) => setCategory(event.target.value as RequirementCategory)}>{CATEGORIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        </div>
        <label>Requirement text<textarea required maxLength={4000} rows={4} value={text} onChange={(event) => setText(event.target.value)} placeholder="Transcribe the published requirement; retain its source wording." /></label>
        <div className="source-form-row source-form-options">
          <label>Assessment kind<select value={kind} onChange={(event) => setKind(event.target.value as RequirementKind)}><option value="objective">Objective · factual criterion</option><option value="judgment">Judgment · interpretive criterion</option></select></label>
          <label className="checkbox-label"><input type="checkbox" checked={mandatory} onChange={(event) => setMandatory(event.target.checked)} /> Mandatory requirement</label>
        </div>
        {formError && <div className="inline-alert" role="alert"><TriangleAlert size={15} />{formError}</div>}
        {notice && <div className="inline-success" role="status"><Check size={15} />{notice}</div>}
        <div className="source-form-footer"><span>Changes require the optional owner session; no existing requirement is edited or deleted.</span><button className="button dark" type="submit" disabled={!canWrite(access) || busy}>{busy ? 'Saving…' : 'Register requirement'} <ArrowRight size={14} /></button></div>
      </form>
    </section>
  </div>;
}

export function AmendmentWorkspace({ tenderId }: { tenderId: string }) {
  const access = useWorkspaceAccess();
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [amendments, setAmendments] = useState<AmendmentRecord[]>([]);
  const [state, setState] = useState<AsyncState>('loading');
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [documentId, setDocumentId] = useState('');
  const [predecessorId, setPredecessorId] = useState('');
  const [relationship, setRelationship] = useState<AmendmentRelationship>('unresolved');
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let live = true;
    setState('loading');
    Promise.all([loadTenderDocuments(tenderId), loadTenderAmendments(tenderId)]).then(([loadedDocuments, loadedAmendments]) => {
      if (!live) return;
      setDocuments(loadedDocuments);
      setAmendments(loadedAmendments);
      setError('');
      setState('ready');
    }).catch((problem: unknown) => {
      if (!live) return;
      setError(messageFor(problem, 'Amendment records could not be loaded.'));
      setState('error');
    });
    return () => { live = false; };
  }, [tenderId, reloadKey]);

  function chooseAmendmentDocument(value: string) {
    setDocumentId(value);
    setPredecessorId(findDocument(documents, value)?.supersedes_document_id || '');
  }

  const chosenDocument = findDocument(documents, documentId);
  const predecessorOptions = documents.filter((document) => document.id !== documentId
    && (!chosenDocument || document.group_id === chosenDocument.group_id));

  async function addAmendment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError('');
    setNotice('');
    if (!canWrite(access)) {
      setFormError('Sign in with the workspace owner session before adding an amendment annotation.');
      return;
    }
    if (!documentId) {
      setFormError('Choose the amendment document version first.');
      return;
    }
    if (documentId === predecessorId) {
      setFormError('An amendment version cannot be its own predecessor.');
      return;
    }
    setBusy(true);
    try {
      await createTenderAmendment(tenderId, {
        document_id: documentId,
        predecessor_id: predecessorId || null,
        relationship,
      });
      setNotice('Amendment relationship recorded as a reviewer annotation. No semantic comparison was generated.');
      setReloadKey((value) => value + 1);
    } catch (problem) {
      setFormError(messageFor(problem, 'Amendment annotation could not be saved.'));
    } finally {
      setBusy(false);
    }
  }

  return <div className="source-workspace">
    <OwnerAccessPanel access={access} />
    <div className="source-intro-card"><div className="source-intro-icon"><TriangleAlert size={18} /></div><div><b>Trace amendment relationships without inventing a comparison</b><p>Relationship labels are manually recorded. TenderLens does not infer changed clauses, affected requirements, or legal effect.</p></div></div>
    <section className="source-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">01 / RELATIONSHIP REGISTER</span><h3>Recorded amendments</h3><p>The source service identifies the document versions and reviewer-declared relationship only.</p></div><FileCheck2 size={17} /></header>
      <DataState state={state} error={error} onRetry={() => setReloadKey((value) => value + 1)} />
      {state === 'ready' && amendments.length === 0 && <DataState state="ready" error="" empty={{ title: 'No amendment relationships recorded', detail: 'Upload each published amendment as a new document version, then record its relationship to the predecessor.' }} onRetry={() => setReloadKey((value) => value + 1)} />}
      {state === 'ready' && amendments.length > 0 && <div className="amendment-list">{amendments.map((amendment) => {
        const document = findDocument(documents, amendment.document_id);
        const predecessor = findDocument(documents, amendment.predecessor_id);
        return <article className="amendment-row" key={amendment.id}>
          <div className="amendment-badge"><FileText size={16} /></div>
          <div className="amendment-details"><b>{document ? `${document.filename} · v${document.version}` : amendment.document_id}</b><small>{predecessor ? `Compared to predecessor record ${predecessor.filename} · v${predecessor.version}` : 'No predecessor linked'}</small><span className="relationship-chip">{relationshipLabel(amendment.relationship)}</span></div>
          <div className="amendment-status"><span className="state neutral">COMPARISON UNAVAILABLE</span><small>Not inferred</small></div>
        </article>;
      })}</div>}
    </section>
    <section className="source-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">02 / ADD ANNOTATION</span><h3>Record a relationship</h3><p>Both selected documents must already exist in this tender's immutable version register.</p></div><Plus size={17} /></header>
      {state === 'ready' && documents.length === 0 && <div className="source-inline-note">Register the source and amendment document versions on the <b>Documents</b> tab first.</div>}
      <form className="source-form" onSubmit={addAmendment}>
        <div className="source-form-row">
          <label>Amendment document<select required value={documentId} onChange={(event) => chooseAmendmentDocument(event.target.value)}><option value="">Select a source version</option>{documents.map((document) => <option key={document.id} value={document.id}>{document.group_id} · v{document.version} · {document.filename}</option>)}</select></label>
          <label>Predecessor<select value={predecessorId} onChange={(event) => setPredecessorId(event.target.value)}><option value="">No predecessor linked</option>{predecessorOptions.map((document) => <option key={document.id} value={document.id}>{document.group_id} · v{document.version} · {document.filename}</option>)}</select></label>
        </div>
        <label>Declared relationship<select value={relationship} onChange={(event) => setRelationship(event.target.value as AmendmentRelationship)}>{RELATIONSHIPS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        {formError && <div className="inline-alert" role="alert"><TriangleAlert size={15} />{formError}</div>}
        {notice && <div className="inline-success" role="status"><Check size={15} />{notice}</div>}
        <div className="source-form-footer"><span>Annotation is immutable and does not calculate inserted or removed passages.</span><button className="button dark" type="submit" disabled={!canWrite(access) || busy || documents.length === 0}>{busy ? 'Saving…' : 'Save relationship'} <ArrowRight size={14} /></button></div>
      </form>
    </section>
  </div>;
}

export function EvidenceWorkspace({ tenderId, onOpen }: { tenderId: string; onOpen: (path: string) => void }) {
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [evidence, setEvidence] = useState<EvidenceRecord[]>([]);
  const [state, setState] = useState<AsyncState>('loading');
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let live = true;
    setState('loading');
    Promise.all([loadTenderDocuments(tenderId), loadTenderRequirements(tenderId), loadTenderEvidence(tenderId)]).then(([loadedDocuments, loadedRequirements, loadedEvidence]) => {
      if (!live) return;
      setDocuments(loadedDocuments);
      setRequirements(loadedRequirements);
      setEvidence(loadedEvidence);
      setError('');
      setState('ready');
    }).catch((problem: unknown) => {
      if (!live) return;
      setError(messageFor(problem, 'Evidence records could not be loaded.'));
      setState('error');
    });
    return () => { live = false; };
  }, [tenderId, reloadKey]);

  return <div className="source-workspace">
    <div className="source-intro-card"><div className="source-intro-icon"><Fingerprint size={18} /></div><div><b>Evidence stays attached to an exact source version</b><p>Every excerpt retains a SHA-256, document version, and UTF-8 byte range. Hash verification does not prove who published the source or whether it is true.</p></div></div>
    <section className="source-panel">
      <header className="source-panel-heading"><div><span className="panel-kicker">EVIDENCE REGISTER</span><h3>Saved source passages</h3><p>These are stored backend records, not model-generated findings.</p></div><Fingerprint size={17} /></header>
      <DataState state={state} error={error} onRetry={() => setReloadKey((value) => value + 1)} />
      {state === 'ready' && evidence.length === 0 && <div className="source-empty"><FileText size={20} /><b>No evidence passages saved yet</b><p>Open Documents, load the exact TXT version, select a passage, and save it against a requirement or as general context.</p><button className="button outline compact" type="button" onClick={() => onOpen(`/app/tenders/${encodeURIComponent(tenderId)}/documents`)}>Open source documents <ArrowRight size={14} /></button></div>}
      {state === 'ready' && evidence.length > 0 && <div className="source-evidence-list">{evidence.map((item) => <EvidenceCard key={item.id} evidence={item} documents={documents} requirements={requirements} />)}</div>}
    </section>
  </div>;
}

export function EvaluationWorkspace({ tenderId }: { tenderId: string }) {
  const access = useWorkspaceAccess();
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [evidence, setEvidence] = useState<EvidenceRecord[]>([]);
  const [snapshots, setSnapshots] = useState<EvaluationSnapshotSummary[]>([]);
  const [state, setState] = useState<AsyncState>('loading');
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [policy, setPolicy] = useState<AggregationPolicy>('mandatory_gate_optional_advisory_v1');
  const [selectedEvidenceIds, setSelectedEvidenceIds] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState('');
  const [prepared, setPrepared] = useState<EvaluationSnapshot | null>(null);

  useEffect(() => {
    let live = true;
    setState('loading');
    Promise.all([loadTenderRequirements(tenderId), loadTenderEvidence(tenderId), loadEvaluationSnapshots(tenderId)]).then(([loadedRequirements, loadedEvidence, loadedSnapshots]) => {
      if (!live) return;
      setRequirements(loadedRequirements);
      setEvidence(loadedEvidence);
      setSnapshots(loadedSnapshots);
      setSelectedEvidenceIds(new Set(loadedEvidence.map((item) => item.id)));
      setError('');
      setState('ready');
    }).catch((problem: unknown) => {
      if (!live) return;
      setError(messageFor(problem, 'Evaluation preparation records could not be loaded.'));
      setState('error');
    });
    return () => { live = false; };
  }, [tenderId, reloadKey]);

  function toggleEvidence(id: string) {
    setSelectedEvidenceIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function prepare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError('');
    setPrepared(null);
    if (!canWrite(access)) {
      setFormError('Sign in with the workspace owner session before preparing an evaluation snapshot.');
      return;
    }
    if (requirements.length === 0) {
      setFormError('Register at least one requirement before preparing a snapshot.');
      return;
    }
    setBusy(true);
    try {
      const snapshot = await prepareEvaluationSnapshot(tenderId, [...selectedEvidenceIds], policy);
      setPrepared(snapshot);
      setReloadKey((value) => value + 1);
    } catch (problem) {
      setFormError(messageFor(problem, 'The evaluation snapshot could not be prepared.'));
    } finally {
      setBusy(false);
    }
  }

  return <div className="source-workspace">
    <OwnerAccessPanel access={access} />
    <div className="source-intro-card"><div className="source-intro-icon"><Fingerprint size={18} /></div><div><b>Freeze the current review inputs before any evaluation</b><p>A prepared snapshot includes all registered requirements and only the evidence versions you select. Preparing it does not submit a transaction or run a model.</p></div></div>
    <DataState state={state} error={error} onRetry={() => setReloadKey((value) => value + 1)} />
    {state === 'ready' && <>
      <section className="source-panel">
        <header className="source-panel-heading"><div><span className="panel-kicker">01 / SNAPSHOT INPUTS</span><h3>Requirements and selected evidence</h3><p>{requirements.length} requirement{requirements.length === 1 ? '' : 's'} will be included. Choose the source passages to freeze.</p></div><FileCheck2 size={17} /></header>
        {requirements.length === 0
          ? <div className="source-empty"><TriangleAlert size={19} /><b>Snapshot needs at least one requirement</b><p>Add the published criteria on the Requirements tab first.</p></div>
          : <div className="snapshot-input-list">{requirements.map((requirement) => <div className="snapshot-requirement" key={requirement.id}><span className={`requirement-dot ${requirement.mandatory ? 'mandatory' : ''}`} /><div><b>{requirement.code} · {requirement.text}</b><small>{categoryLabel(requirement.category)} · {requirement.mandatory ? 'Mandatory' : 'Optional'} · {kindLabel(requirement.kind)}</small></div></div>)}</div>}
        {evidence.length === 0
          ? <div className="snapshot-no-evidence"><span>No evidence is registered; all requirements will enter the snapshot without linked passages and may remain unresolved.</span></div>
          : <div className="snapshot-evidence-select"><div className="snapshot-section-label">EVIDENCE TO INCLUDE</div>{evidence.map((item) => <label className="snapshot-evidence-row" key={item.id}><input type="checkbox" checked={selectedEvidenceIds.has(item.id)} onChange={() => toggleEvidence(item.id)} /><span><b>{item.provenance.filename} · v{item.provenance.document_version}</b><small>{item.requirement_id ? requirements.find((requirement) => requirement.id === item.requirement_id)?.code || 'Requirement-specific' : 'General context'} · bytes {item.start_byte}–{item.end_byte} · {item.excerpt.slice(0, 150)}{item.excerpt.length > 150 ? '…' : ''}</small></span><code>{shortHash(item.document_sha256)}</code></label>)}</div>}
      </section>
      <section className="source-panel">
        <header className="source-panel-heading"><div><span className="panel-kicker">02 / PREPARE IMMUTABLE INPUT</span><h3>Aggregation policy</h3><p>The policy is included in the hashed payload and cannot be changed after preparation.</p></div><ShieldCheck size={17} /></header>
        <form className="source-form" onSubmit={prepare}>
          <label>Evaluation policy<select value={policy} onChange={(event) => setPolicy(event.target.value as AggregationPolicy)}>{POLICIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <p className="policy-description">{POLICIES.find((item) => item.value === policy)?.description}</p>
          {formError && <div className="inline-alert" role="alert"><TriangleAlert size={15} />{formError}</div>}
          {prepared && <div className="prepared-snapshot" role="status"><Check size={16} /><div><b>Snapshot prepared · not submitted</b><small>ID {prepared.snapshot_id}</small><code>SHA-256 {prepared.input_sha256}</code></div></div>}
          <div className="source-form-footer"><span>Preparation writes an immutable backend snapshot. No deployment, GenLayer call, model evaluation, or award decision is made here.</span><button className="button dark" type="submit" disabled={!canWrite(access) || busy || requirements.length === 0}>{busy ? 'Preparing…' : 'Prepare evaluation snapshot'} <ArrowRight size={14} /></button></div>
        </form>
      </section>
      <section className="source-panel">
        <header className="source-panel-heading"><div><span className="panel-kicker">03 / SNAPSHOT HISTORY</span><h3>Prepared records</h3><p>Re-read from the API and validated by the backend integrity checks.</p></div><FileCheck2 size={17} /></header>
        {snapshots.length === 0 ? <div className="source-empty"><FileCheck2 size={20} /><b>No snapshots prepared</b><p>Preparing a snapshot creates a stable review input; it does not begin the evaluation lifecycle.</p></div> : <div className="snapshot-history">{snapshots.map((snapshot) => <SnapshotCard key={snapshot.id} snapshot={snapshot} />)}</div>}
      </section>
    </>}
  </div>;
}

function RequirementCard({ requirement }: { requirement: Requirement }) {
  return <article className="requirement-card">
    <div className="requirement-card-top"><b>{requirement.code}</b><span className={`requirement-flag ${requirement.mandatory ? 'is-mandatory' : ''}`}>{requirement.mandatory ? 'MANDATORY' : 'OPTIONAL'}</span><span className="requirement-flag">{kindLabel(requirement.kind)}</span></div>
    <p>{requirement.text}</p>
    <small>{categoryLabel(requirement.category)} · Registered {formatDate(requirement.created_at)}</small>
  </article>;
}

function EvidenceCard({ evidence, documents, requirements }: {
  evidence: EvidenceRecord;
  documents: DocumentRecord[];
  requirements: Requirement[];
}) {
  const document = findDocument(documents, evidence.document_id);
  const requirement = requirements.find((item) => item.id === evidence.requirement_id);
  return <article className="evidence-card">
    <div className="evidence-card-top"><span className="source-integrity"><ShieldCheck size={14} /> SHA-256 range verified</span><code>bytes {evidence.start_byte}–{evidence.end_byte}</code></div>
    <blockquote>{evidence.excerpt}</blockquote>
    <div className="evidence-card-meta"><span><b>Source</b> {document ? `${document.filename} · v${document.version}` : evidence.provenance.filename}</span><span><b>Requirement</b> {requirement?.code || 'General context'}</span></div>
    <code className="evidence-hash">Document SHA-256 {shortHash(evidence.document_sha256)}</code>
    <small className="provenance-disclaimer">Integrity check: {evidence.provenance.integrity_check}. Source authenticity: {evidence.provenance.source_authenticity.replaceAll('_', ' ')}.</small>
  </article>;
}

function SnapshotCard({ snapshot }: { snapshot: EvaluationSnapshotSummary }) {
  return <article className="snapshot-history-row"><span className="snapshot-status-dot" /><div><b>{snapshot.status.replaceAll('_', ' ')}</b><small>{formatDate(snapshot.created_at)}</small><code>{snapshot.id}</code></div><code className="snapshot-hash">{shortHash(snapshot.input_sha256)}</code></article>;
}

function relationshipLabel(relationship: AmendmentRelationship): string {
  return RELATIONSHIPS.find((item) => item.value === relationship)?.label ?? relationship;
}

function categoryLabel(category: RequirementCategory): string {
  return CATEGORIES.find((item) => item.value === category)?.label ?? category;
}

function kindLabel(kind: RequirementKind): string {
  return kind === 'objective' ? 'Objective' : 'Judgment';
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}
