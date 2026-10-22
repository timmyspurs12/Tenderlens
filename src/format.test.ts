import { describe, expect, it } from 'vitest';
import { plural } from './format';

describe('plural', () => {
  it('agrees with a singular count', () => {
    expect(plural(1, 'requirement')).toBe('1 requirement');
    expect(plural(1, 'unresolved')).toBe('1 unresolved');
  });

  it('agrees with plural and zero counts', () => {
    expect(plural(2, 'requirement')).toBe('2 requirements');
    expect(plural(0, 'requirement')).toBe('0 requirements');
    expect(plural(17, 'requirement')).toBe('17 requirements');
  });

  it('keeps the em dash when the count is unknown', () => {
    expect(plural(null, 'requirement')).toBe('— requirement');
    expect(plural(undefined, 'unresolved')).toBe('— unresolved');
    expect(plural(Number.NaN, 'requirement')).toBe('— requirement');
  });
});
