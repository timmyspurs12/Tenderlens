/**
 * Small formatting helpers shared by the workspace views.
 *
 * These exist so counts read correctly in prose. A panel that reports
 * "1 requirements" undercuts the precision the rest of the product claims.
 */

/** Count plus a correctly inflected noun; an unknown count keeps the em dash. */
export function plural(value: number | null | undefined, noun: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return `— ${noun}`;
  return `${value} ${noun}${value === 1 ? '' : 's'}`;
}
