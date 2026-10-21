import { ArrowRight, ArrowUpRight, ClipboardList, FileCheck2, Plus } from 'lucide-react';
import type { Tender } from './api';

type Props = {
  tenders: Tender[];
  query: string;
  onOpen: (path: string) => void;
};

export default function TenderRecordList({ tenders, query, onOpen }: Props) {
  const filtered = tenders.filter((tender) =>
    `${tender.title} ${tender.reference}`.toLowerCase().includes(query.toLowerCase()),
  );

  if (filtered.length === 0) {
    return (
      <div className="empty-state" data-testid="tender-empty-state">
        <span className="empty-icon"><ClipboardList size={20} /></span>
        <b>{query ? 'No tenders match this search' : 'No tenders registered yet'}</b>
        <p>{query
          ? 'Try a different title or reference.'
          : 'This workspace has no tender records yet. Register a real tender to start its source and review record.'}</p>
        {!query && <button className="button dark compact" onClick={() => onOpen('/app/tenders/new')}>
          <Plus size={15} /> Register a tender <ArrowRight size={14} />
        </button>}
      </div>
    );
  }

  return (
    <>
    <p className="record-provenance-note">Workspace entries returned by the connected service. This list does not independently verify publication or source authenticity.</p>
    <div className="tender-table" data-testid="tender-record-list">
      {filtered.map((tender) => (
        <button key={tender.id} className="tender-row" onClick={() => onOpen(`/app/tenders/${encodeURIComponent(tender.id)}`)}>
          <span className="row-file"><FileCheck2 size={17} /></span>
          <span className="row-title"><b>{tender.title}</b><small>{tender.reference}</small></span>
          <span className="row-state"><span className="state-dot" />{tender.status || 'Status not reported'}</span>
          <span className="row-date">{tender.deadline || 'Deadline not reported'}</span>
          <ArrowUpRight size={16} />
        </button>
      ))}
    </div>
    </>
  );
}
