import { describe, expect, it } from 'vitest';
import { normalizeSourceForSelection, selectionToByteRange, utf8ByteOffset } from './evidence';

describe('source passage byte offsets', () => {
  it('maps LF selections to UTF-8 byte ranges, including multi-byte characters', () => {
    const source = 'Eligibility: café is due.\n';
    const normalized = normalizeSourceForSelection(source);
    const start = normalized.text.indexOf('café');
    const end = start + 'café'.length;
    const range = selectionToByteRange(source, normalized, start, end);
    expect(range.startByte).toBe(new TextEncoder().encode('Eligibility: ').length);
    expect(range.endByte - range.startByte).toBe(new TextEncoder().encode('café').length);
  });

  it('maps normalized CRLF and standalone CR display offsets to original bytes', () => {
    const source = 'First\r\nSecond\rThird';
    const normalized = normalizeSourceForSelection(source);
    expect(normalized.text).toBe('First\nSecond\nThird');
    const start = normalized.text.indexOf('Second');
    const end = start + 'Second'.length;
    const range = selectionToByteRange(source, normalized, start, end);
    expect(range.startByte).toBe(new TextEncoder().encode('First\r\n').length);
    expect(range.endByte - range.startByte).toBe(new TextEncoder().encode('Second').length);
  });

  it('accounts for a stripped UTF-8 BOM and a supplementary Unicode character', () => {
    const source = 'A🚀B';
    expect(utf8ByteOffset(source, 3, 3)).toBe(8);
    const normalized = normalizeSourceForSelection(source);
    const start = normalized.text.indexOf('🚀');
    const range = selectionToByteRange(source, normalized, start, start + '🚀'.length, 3);
    expect(range).toEqual({ startByte: 4, endByte: 8 });
  });

  it('rejects selection boundaries that split a Unicode surrogate pair', () => {
    const source = 'A🚀B';
    expect(() => utf8ByteOffset(source, 2)).toThrow(/splits a character/);
    const normalized = normalizeSourceForSelection(source);
    expect(() => selectionToByteRange(source, normalized, 1, 2)).toThrow(/splits a character/);
  });

  it('rejects empty and out-of-range selections', () => {
    const source = 'source';
    const normalized = normalizeSourceForSelection(source);
    expect(() => selectionToByteRange(source, normalized, 2, 2)).toThrow(/non-empty/);
    expect(() => utf8ByteOffset(source, 99)).toThrow(/outside/);
  });
});
