import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import TenderRecordList from './TenderRecordList';

const tender = { id: 'tender/1', title: 'Published water supply tender', reference: 'PROC-2026-04' };

describe('TenderRecordList', () => {
  it('renders returned tender identity and opens the correct encoded record path', () => {
    const onOpen = vi.fn();
    render(<TenderRecordList tenders={[tender]} query="" onOpen={onOpen} />);
    expect(screen.getByText(tender.title)).toBeTruthy();
    expect(screen.getByText(tender.reference)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Published water supply tender/ }));
    expect(onOpen).toHaveBeenCalledWith('/app/tenders/tender%2F1');
  });

  it('renders a truthful empty state and register action for an empty workspace', () => {
    const onOpen = vi.fn();
    render(<TenderRecordList tenders={[]} query="" onOpen={onOpen} />);
    expect(screen.getByText('No tenders registered yet')).toBeTruthy();
    expect(screen.getByText(/This workspace has no tender records yet/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Register a tender/ }));
    expect(onOpen).toHaveBeenCalledWith('/app/tenders/new');
  });

  it('does not misrepresent a filtered-out list as an empty workspace', () => {
    render(<TenderRecordList tenders={[tender]} query="unmatched" onOpen={vi.fn()} />);
    expect(screen.getByText('No tenders match this search')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Register a tender/ })).toBeNull();
  });
});
