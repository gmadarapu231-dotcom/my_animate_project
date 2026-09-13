/**
 * Display helpers.
 *
 * The salary rule from the backend carries through to every client: show what
 * the employer actually wrote, with the normalised figure in parentheses. A
 * CTC figure is not a base figure and the UI must not flatten the difference.
 */

export const DEADLINE_LABEL: Record<string, string> = {
  today: 'DEADLINE TODAY',
  within_48h: 'DEADLINE WITHIN 48 HOURS',
  within_7d: 'DEADLINE WITHIN 7 DAYS',
  future: 'FUTURE',
  none: 'NO DEADLINE',
  expired: 'EXPIRED',
};

export const DEADLINE_EMOJI: Record<string, string> = {
  today: '\u{1F534}',
  within_48h: '\u{1F7E0}',
  within_7d: '\u{1F7E1}',
  future: '\u{1F7E2}',
  none: '⚪',
  expired: '⚫',
};

export function deadlineFlag(bucket: string | undefined): string {
  if (!bucket) return '';
  return `${DEADLINE_EMOJI[bucket] ?? ''} ${DEADLINE_LABEL[bucket] ?? bucket}`.trim();
}

export function score(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return String(Math.round(value * 10) / 10);
}

export function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${Math.round(value)}%`;
}

export function titleCase(value: string | null | undefined): string {
  if (!value) return '';
  return value.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function relativeDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const then = new Date(iso + (iso.length === 10 ? 'T00:00:00' : ''));
  if (Number.isNaN(then.getTime())) return iso;
  const days = Math.round((Date.now() - then.getTime()) / 86_400_000);
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days > 0 && days < 30) return `${days}d ago`;
  if (days < 0 && days > -30) return `in ${Math.abs(days)}d`;
  return then.toLocaleDateString();
}

/** Truncate without cutting a word in half. */
export function clamp(value: string, max: number): string {
  if (value.length <= max) return value;
  const cut = value.slice(0, max);
  const space = cut.lastIndexOf(' ');
  return `${space > max * 0.6 ? cut.slice(0, space) : cut}…`;
}
