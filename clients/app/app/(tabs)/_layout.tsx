import { Link, Tabs } from 'expo-router';
import { ColorValue, Pressable, Text } from 'react-native';

import { spacing, usePalette } from '../../src/theme';

/**
 * Tab icons are emoji rather than an icon font: they render identically on
 * iOS, Android and web with no asset pipeline, and they carry meaning on their
 * own for anyone who has the labels turned off.
 */
function TabIcon({ glyph, color }: { glyph: string; color: ColorValue }) {
  return <Text style={{ fontSize: 18, color }}>{glyph}</Text>;
}

export default function TabLayout() {
  const p = usePalette();
  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: p.accent,
        tabBarInactiveTintColor: p.muted,
        tabBarStyle: { backgroundColor: p.panel, borderTopColor: p.line },
        headerStyle: { backgroundColor: p.panel },
        headerTitleStyle: { color: p.ink, fontSize: 16 },
        sceneStyle: { backgroundColor: p.bg },
        headerRight: () => (
          <Link href="/settings" asChild>
            <Pressable
              accessibilityLabel="Settings"
              accessibilityRole="button"
              style={{ paddingHorizontal: spacing.lg }}
            >
              <Text style={{ fontSize: 17 }}>⚙︎</Text>
            </Pressable>
          </Link>
        ),
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: 'Jobs',
          tabBarIcon: ({ color }) => <TabIcon glyph="◎" color={color} />,
        }}
      />
      <Tabs.Screen
        name="today"
        options={{
          title: 'Today',
          tabBarIcon: ({ color }) => <TabIcon glyph="★" color={color} />,
        }}
      />
      <Tabs.Screen
        name="applications"
        options={{
          title: 'Pipeline',
          tabBarIcon: ({ color }) => <TabIcon glyph="≡" color={color} />,
        }}
      />
      <Tabs.Screen
        name="insights"
        options={{
          title: 'Insights',
          tabBarIcon: ({ color }) => <TabIcon glyph="◑" color={color} />,
        }}
      />
      <Tabs.Screen
        name="sources"
        options={{
          title: 'Sources',
          tabBarIcon: ({ color }) => <TabIcon glyph="⇄" color={color} />,
        }}
      />
      <Tabs.Screen
        name="agent"
        options={{
          title: 'Agent',
          tabBarIcon: ({ color }) => <TabIcon glyph="✦" color={color} />,
        }}
      />
    </Tabs>
  );
}
