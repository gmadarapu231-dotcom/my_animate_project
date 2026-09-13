/**
 * The shared component kit: one set of primitives for iOS, Android and web.
 *
 * Everything is built from `View`/`Text`/`Pressable` so it renders natively on
 * device and through react-native-web in the browser. Layout uses flex and
 * wraps rather than fixed widths, which is what makes the same screens work at
 * 390pt on a phone and 1400px in a browser.
 */
import { ReactNode } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextStyle,
  View,
  ViewStyle,
} from 'react-native';

import { Palette, radius, spacing, type, usePalette } from '../theme';

// ---------------------------------------------------------------------------
export function Screen({ children, scroll = true }: { children: ReactNode; scroll?: boolean }) {
  const p = usePalette();
  if (!scroll) {
    return <View style={{ flex: 1, backgroundColor: p.bg }}>{children}</View>;
  }
  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: p.bg }}
      contentContainerStyle={styles.screenContent}
      keyboardShouldPersistTaps="handled"
    >
      {/* Capped and centred so the same layout reads well on a wide browser. */}
      <View style={styles.centre}>{children}</View>
    </ScrollView>
  );
}

export function Panel({
  children,
  title,
  style,
  accent,
}: {
  children: ReactNode;
  title?: string;
  style?: ViewStyle;
  accent?: string;
}) {
  const p = usePalette();
  return (
    <View
      style={[
        styles.panel,
        { backgroundColor: p.panel, borderColor: p.line },
        accent ? { borderLeftWidth: 4, borderLeftColor: accent } : null,
        style,
      ]}
    >
      {title ? <SectionTitle>{title}</SectionTitle> : null}
      {children}
    </View>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  const p = usePalette();
  return (
    <Text style={[type.tiny, { color: p.muted, marginBottom: spacing.sm, textTransform: 'uppercase' }]}>
      {children}
    </Text>
  );
}

export function Body({
  children,
  muted,
  style,
  numberOfLines,
}: {
  children: ReactNode;
  muted?: boolean;
  style?: TextStyle;
  numberOfLines?: number;
}) {
  const p = usePalette();
  return (
    <Text
      numberOfLines={numberOfLines}
      style={[type.body, { color: muted ? p.muted : p.ink }, style]}
    >
      {children}
    </Text>
  );
}

export function Small({ children, style }: { children: ReactNode; style?: TextStyle }) {
  const p = usePalette();
  return <Text style={[type.small, { color: p.muted }, style]}>{children}</Text>;
}

export function Mono({ children, style }: { children: ReactNode; style?: TextStyle }) {
  const p = usePalette();
  return <Text style={[type.mono, { color: p.muted }, style]}>{children}</Text>;
}

// ---------------------------------------------------------------------------
export function Chip({
  label,
  tone = 'neutral',
}: {
  label: string;
  tone?: 'neutral' | 'accent' | 'ok' | 'warn' | 'bad';
}) {
  const p = usePalette();
  const tones: Record<string, { bg: string; fg: string }> = {
    neutral: { bg: p.chip, fg: p.chipInk },
    accent: { bg: p.accentSoft, fg: p.accent },
    ok: { bg: withAlpha(p.ok), fg: p.ok },
    warn: { bg: withAlpha(p.warn), fg: p.warn },
    bad: { bg: withAlpha(p.bad), fg: p.bad },
  };
  const { bg, fg } = tones[tone];
  return (
    <View style={[styles.chip, { backgroundColor: bg }]}>
      <Text style={{ fontSize: 11.5, color: fg, fontWeight: tone === 'accent' ? '600' : '400' }}>
        {label}
      </Text>
    </View>
  );
}

export function Row({ children, style, gap = spacing.sm }: { children: ReactNode; style?: ViewStyle; gap?: number }) {
  return <View style={[{ flexDirection: 'row', flexWrap: 'wrap', gap, alignItems: 'center' }, style]}>{children}</View>;
}

export function Button({
  label,
  onPress,
  variant = 'default',
  pending,
  disabled,
  small,
}: {
  label: string;
  onPress: () => void;
  variant?: 'default' | 'primary' | 'danger';
  pending?: boolean;
  disabled?: boolean;
  small?: boolean;
}) {
  const p = usePalette();
  const isPrimary = variant === 'primary';
  const inactive = disabled || pending;
  return (
    <Pressable
      onPress={onPress}
      disabled={inactive}
      accessibilityRole="button"
      accessibilityState={{ disabled: !!inactive, busy: !!pending }}
      style={({ pressed }) => [
        styles.button,
        small ? styles.buttonSmall : null,
        {
          backgroundColor: isPrimary ? p.accent : p.panel,
          borderColor: variant === 'danger' ? p.bad : isPrimary ? p.accent : p.line,
          opacity: inactive ? 0.55 : pressed ? 0.8 : 1,
        },
      ]}
    >
      {pending ? (
        <ActivityIndicator size="small" color={isPrimary ? '#fff' : p.accent} />
      ) : (
        <Text
          style={{
            fontSize: small ? 12.5 : 14,
            fontWeight: '600',
            color: isPrimary ? '#fff' : variant === 'danger' ? p.bad : p.ink,
          }}
        >
          {label}
        </Text>
      )}
    </Pressable>
  );
}

export function FilterChip({
  label,
  active,
  onPress,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
}) {
  const p = usePalette();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityState={{ selected: active }}
      style={({ pressed }) => [
        styles.filterChip,
        {
          backgroundColor: active ? p.accentSoft : p.panel,
          borderColor: active ? p.accent : p.line,
          opacity: pressed ? 0.8 : 1,
        },
      ]}
    >
      <Text style={{ fontSize: 12.5, fontWeight: '600', color: active ? p.accent : p.muted }}>
        {label}
      </Text>
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
export function ScoreTile({ value, label }: { value: string; label: string }) {
  const p = usePalette();
  return (
    <View style={{ minWidth: 62 }}>
      <Text style={[type.score, { color: p.ink }]}>{value}</Text>
      <Text style={[type.tiny, { color: p.muted, textTransform: 'uppercase' }]}>{label}</Text>
    </View>
  );
}

export function StatTile({ value, label, note }: { value: string | number; label: string; note?: string }) {
  const p = usePalette();
  return (
    <View style={[styles.statTile, { borderColor: p.line, backgroundColor: p.panelAlt }]}>
      <Text style={{ fontSize: 21, fontWeight: '700', color: p.ink }}>{value}</Text>
      <Text style={[type.tiny, { color: p.muted, textTransform: 'uppercase' }]}>{label}</Text>
      {note ? <Small style={{ marginTop: 2 }}>{note}</Small> : null}
    </View>
  );
}

export function Divider() {
  const p = usePalette();
  return <View style={{ height: StyleSheet.hairlineWidth, backgroundColor: p.line, marginVertical: spacing.md }} />;
}

// ---------------------------------------------------------------------------
export function Loading({ label = 'Loading…' }: { label?: string }) {
  const p = usePalette();
  return (
    <View style={styles.centreBox}>
      <ActivityIndicator color={p.accent} />
      <Small style={{ marginTop: spacing.sm }}>{label}</Small>
    </View>
  );
}

export function ErrorNote({
  message,
  onRetry,
  hint,
}: {
  message: string;
  onRetry?: () => void;
  hint?: string;
}) {
  const p = usePalette();
  return (
    <Panel accent={p.bad}>
      <Body>{message}</Body>
      {hint ? <Small style={{ marginTop: spacing.xs }}>{hint}</Small> : null}
      {onRetry ? (
        <View style={{ marginTop: spacing.md, alignSelf: 'flex-start' }}>
          <Button label="Retry" onPress={onRetry} small />
        </View>
      ) : null}
    </Panel>
  );
}

export function Empty({ message }: { message: string }) {
  return (
    <Panel>
      <Small>{message}</Small>
    </Panel>
  );
}

/**
 * A disclaimer that cannot be dismissed or collapsed.
 *
 * Used for the work-authorization caveat and the hypothesis-vs-fact
 * distinction. These are load-bearing: a user who mistakes a guess for a fact
 * makes a worse decision than one who sees no answer at all.
 */
export function Caveat({ children }: { children: ReactNode }) {
  const p = usePalette();
  return (
    <Text style={[type.small, { color: p.muted, fontStyle: 'italic', marginTop: spacing.xs }]}>
      {children}
    </Text>
  );
}

function withAlpha(hex: string): string {
  // Tints a token colour for chip backgrounds without a colour library.
  const clean = hex.replace('#', '');
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, 0.14)`;
}

/**
 * The shared shape of every list screen: an optional header, then
 * loading / error / empty / items. Keeps the five tab screens consistent
 * without each re-implementing the same four states.
 */
export function ListScreen<T>({
  header,
  items,
  renderItem,
  loading,
  error,
  onRetry,
  emptyMessage,
}: {
  header?: ReactNode;
  items: T[];
  renderItem: (item: T) => ReactNode;
  loading?: boolean;
  error?: string;
  onRetry?: () => void;
  emptyMessage: string;
}) {
  const p = usePalette();
  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: p.bg }}
      contentContainerStyle={styles.screenContent}
      keyboardShouldPersistTaps="handled"
    >
      <View style={styles.centre}>
        {header}
        {error ? <ErrorNote message={error} onRetry={onRetry} /> : null}
        {loading ? <Loading /> : null}
        {!loading && !error && items.length === 0 ? <Empty message={emptyMessage} /> : null}
        {items.map(renderItem)}
      </View>
    </ScrollView>
  );
}

export const styles = StyleSheet.create({
  screenContent: { padding: spacing.lg, paddingBottom: spacing.xxl * 2 },
  centre: { width: '100%', maxWidth: 900, alignSelf: 'center' },
  centreBox: { alignItems: 'center', justifyContent: 'center', padding: spacing.xl },
  panel: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.md,
    padding: spacing.lg,
    marginBottom: spacing.md,
  },
  chip: {
    borderRadius: radius.pill,
    paddingHorizontal: spacing.md - 2,
    paddingVertical: 3,
  },
  button: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.sm + 1,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 38,
  },
  buttonSmall: { paddingHorizontal: spacing.sm + 2, paddingVertical: 5, minHeight: 30 },
  filterChip: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.pill,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
  },
  statTile: {
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.sm,
    padding: spacing.md,
    minWidth: 104,
    flexGrow: 1,
  },
});
