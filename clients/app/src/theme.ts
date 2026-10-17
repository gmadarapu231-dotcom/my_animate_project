/**
 * Design tokens, shared by iOS, Android and web.
 *
 * The palette carries over the deadline language from the original dashboard,
 * because it is the product's core signal: a job's border colour tells you how
 * urgent it is before you read a word. Colour is never the only cue -- every
 * deadline flag pairs its colour with an emoji and a text label.
 */
import { useEffect, useState } from 'react';
import { Platform, useColorScheme } from 'react-native';

export type Palette = {
  bg: string;
  panel: string;
  panelAlt: string;
  ink: string;
  muted: string;
  line: string;
  /** The one action colour: near-black on light, near-white on dark. */
  accent: string;
  accentSoft: string;
  /** Text drawn on top of `accent`. */
  onAccent: string;
  /** The signal colour for the agent being at work: live dots, step numbers. */
  signal: string;
  ok: string;
  warn: string;
  bad: string;
  chip: string;
  chipInk: string;
};

/*
 * A quiet, editorial palette: warm paper, ink-black type and controls, and a
 * single green signal for "the agent is working". The deadline and verdict
 * colours below stay saturated on purpose -- they are the only loud thing on
 * the screen, which is what makes them readable at a glance.
 */
const light: Palette = {
  bg: '#f7f6f2',
  panel: '#ffffff',
  panelAlt: '#f2f1ec',
  ink: '#0e0e10',
  muted: '#6b6a66',
  line: '#e4e2db',
  accent: '#0e0e10',
  accentSoft: '#ecebe5',
  onAccent: '#ffffff',
  signal: '#14a35b',
  ok: '#1f8a53',
  warn: '#b4740b',
  bad: '#c0392b',
  chip: '#efeee8',
  chipInk: '#45443f',
};

const dark: Palette = {
  bg: '#0c0c0d',
  panel: '#161618',
  panelAlt: '#1d1d20',
  ink: '#f2f1ec',
  muted: '#9b9a94',
  line: '#2a2a2d',
  accent: '#f2f1ec',
  accentSoft: '#26262a',
  onAccent: '#0e0e10',
  signal: '#3ddc84',
  ok: '#4ec98a',
  warn: '#e0a44a',
  bad: '#f0776a',
  chip: '#232326',
  chipInk: '#c4c3bd',
};

export const spacing = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 } as const;
export const radius = { sm: 8, md: 14, lg: 20, pill: 999 } as const;

export const type = {
  display: { fontSize: 34, fontWeight: '700' as const, letterSpacing: -1.1, lineHeight: 38 },
  h1: { fontSize: 24, fontWeight: '700' as const, letterSpacing: -0.6 },
  h2: { fontSize: 18, fontWeight: '600' as const, letterSpacing: -0.3 },
  title: { fontSize: 16, fontWeight: '600' as const },
  body: { fontSize: 14, fontWeight: '400' as const },
  small: { fontSize: 12.5, fontWeight: '400' as const },
  tiny: { fontSize: 11, fontWeight: '600' as const, letterSpacing: 0.6 },
  score: { fontSize: 19, fontWeight: '700' as const },
  mono: {
    fontSize: 12.5,
    // On web a stack, so text never falls back to the browser's serif while
    // the bundled face is still loading.
    fontFamily: Platform.select({
      web: 'SpaceMono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      default: 'SpaceMono',
    }),
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
