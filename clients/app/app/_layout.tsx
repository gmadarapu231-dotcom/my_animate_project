import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useColorScheme } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { SessionContext, useSessionState } from '../src/hooks';
import { usePalette } from '../src/theme';

export { ErrorBoundary } from 'expo-router';

export const unstable_settings = { initialRouteName: '(tabs)' };

export default function RootLayout() {
  const scheme = useColorScheme();
  const session = useSessionState();
  const p = usePalette();

  return (
    <SafeAreaProvider>
      <SessionContext.Provider value={session}>
        <ThemeProvider value={scheme === 'dark' ? DarkTheme : DefaultTheme}>
          <StatusBar style={scheme === 'dark' ? 'light' : 'dark'} />
          <Stack
            screenOptions={{
              headerStyle: { backgroundColor: p.panel },
              headerTitleStyle: { color: p.ink, fontSize: 16 },
              headerTintColor: p.accent,
              contentStyle: { backgroundColor: p.bg },
            }}
          >
            <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
            <Stack.Screen name="job/[id]" options={{ title: 'Job' }} />
            <Stack.Screen name="settings" options={{ title: 'Settings', presentation: 'modal' }} />
          </Stack>
        </ThemeProvider>
      </SessionContext.Provider>
    </SafeAreaProvider>
  );
}
