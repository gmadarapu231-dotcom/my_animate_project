/**
 * Design tokens, shared by iOS, Android and web.
 *
 * The palette carries over the deadline language from the original dashboard,
 * because it is the product's core signal: a job's border colour tells you how
 * urgent it is before you read a word. Colour is never the only cue -- every
 * deadline flag pairs its colour with an emoji and a text label.
 */
import { useEffect, useState } from 'react';
import { useColorScheme } from 'react-native';

export type Palette = {
  bg: string;
  panel: string;
  panelAlt: string;
  ink: string;
  muted: string;
  line: string;
  accent: string;
  accentSoft: string;
  ok: string;
  warn: string;
  bad: string;
  chip: string;
  chipInk: string;
};

const light: Palette = {
  bg: '#f6f7f9',
  panel: '#ffffff',
  panelAlt: '#fafbfc',
  ink: '#16181d',
  muted: '#646a75',
  line: '#e3e6eb',
  accent: '#2f5fd0',
  accentSoft: '#eaf0fe',
  ok: '#1f8a53',
  warn: '#b4740b',
  bad: '#c0392b',
  chip: '#eef1f5',
  chipInk: '#4a5160',
};

const dark: Palette = {
  bg: '#14161a',
  panel: '#1c1f25',
  panelAlt: '#22262d',
  ink: '#e8eaee',
  muted: '#9aa1ad',
  line: '#2b2f37',
  accent: '#7aa2f7',
  accentSoft: '#20293d',
  ok: '#4ec98a',
  warn: '#e0a44a',
  bad: '#f0776a',
  chip: '#252a33',
  chipInk: '#b3bac6',
};

export const spacing = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 } as const;
export const radius = { sm: 6, md: 10, lg: 14, pill: 999 } as const;

export const type = {
  h1: { fontSize: 22, fontWeight: '700' as const, letterSpacing: -0.3 },
  h2: { fontSize: 17, fontWeight: '600' as const, letterSpacing: -0.2 },
  title: { fontSize: 16, fontWeight: '600' as const },
  body: { fontSize: 14, fontWeight: '400' as const },
  small: { fontSize: 12.5, fontWeight: '400' as const },
  tiny: { fontSize: 11, fontWeight: '600' as const, letterSpacing: 0.4 },
  score: { fontSize: 19, fontWeight: '700' as const },
  mono: {
    fontSize: 12.5,
    fontFamily: 'SpaceMono',
  },
};

/**
 * The effective colour scheme.
 *
 * `useColorScheme` reads the OS preference, which is right on device and right
 * for a browser at its default setting. But a web host can also stamp an
 * explicit choice on the root element as `data-theme="dark" | "light"` -- and
 * if the app ignored that stamp, the host would paint one theme's ground
 * behind the other theme's content. So an explicit stamp wins, and a
 * MutationObserver keeps up if the viewer toggles it while the page is open.
 */
function useResolvedScheme(): 'light' | 'dark' {
  const system = useColorScheme() === 'dark' ? 'dark' : 'light';
  const [stamped, setStamped] = useState<'light' | 'dark' | null>(readStamp);

  useEffect(() => {
    if (typeof document === 'undefined') return;
    const root = document.documentElement;
    const observer = new MutationObserver(() => setStamped(readStamp()));
    observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);

  return stamped ?? system;
}

function readStamp(): 'light' | 'dark' | null {
  if (typeof document === 'undefined') return null;
  const value = document.documentElement.getAttribute('data-theme');
  return value === 'dark' || value === 'light' ? value : null;
}

export function usePalette(): Palette {
  return useResolvedScheme() === 'dark' ? dark : light;
}

export function useIsDark(): boolean {
  return useResolvedScheme() === 'dark';
}

/** Deadline bucket -> the colour that carries it. */
export function bucketColor(bucket: string | undefined, p: Palette): string {
  switch (bucket) {
    case 'today':
      return p.bad;
    case 'within_48h':
      return '#e67e22';
    case 'within_7d':
      return p.warn;
    case 'future':
      return p.ok;
    case 'expired':
      return p.muted;
    default:
      return p.line;
  }
}

/** Work-authorization verdict -> colour. `unknown` is neutral, never green. */
export function verdictColor(verdict: string | undefined, p: Palette): string {
  switch (verdict) {
    case 'compatible':
      return p.ok;
    case 'potentially_compatible':
      return p.warn;
    case 'not_compatible':
      return p.bad;
    default:
      return p.muted;
  }
}
