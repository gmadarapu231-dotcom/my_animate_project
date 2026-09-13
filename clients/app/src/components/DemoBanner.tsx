/**
 * A persistent, undismissable bar in the demo build.
 *
 * The demo shows real engine output, but a visitor has no way to know which
 * parts are live and which are frozen. Saying so once, permanently, is the
 * difference between a demo and a misleading one.
 */
import { Linking, Pressable, Text, View } from 'react-native';

import { spacing, usePalette } from '../theme';

const REPO = 'https://github.com/gmadarapu231-dotcom/my_animate_project';

export function DemoBanner() {
  const p = usePalette();
  return (
    <View
      style={{
        backgroundColor: p.accentSoft,
        borderBottomWidth: 1,
        borderBottomColor: p.line,
        paddingHorizontal: spacing.lg,
        paddingVertical: spacing.sm,
        flexDirection: 'row',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: spacing.xs,
      }}
    >
      <Text style={{ fontSize: 11.5, fontWeight: '700', color: p.accent, letterSpacing: 0.3 }}>
        DEMO
      </Text>
      <Text style={{ fontSize: 11.5, color: p.muted, flexShrink: 1 }}>
        Real output from the real engines, captured and frozen. Search is limited to recorded
        queries, the agent replays a transcript, and edits are not saved.
      </Text>
      <Pressable onPress={() => Linking.openURL(REPO)} accessibilityRole="link">
        <Text style={{ fontSize: 11.5, color: p.accent, fontWeight: '600' }}>Source →</Text>
      </Pressable>
    </View>
  );
}
