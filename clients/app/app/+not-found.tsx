import { Link, Stack } from 'expo-router';
import { Platform } from 'react-native';

import { Body, Button, Caveat, Mono, Panel, Screen, Small } from '../src/components/ui';

/**
 * Unmatched route.
 *
 * The common cause is not a bad link: Expo Router resolves the route from
 * `location.pathname`, and a build opened straight off disk (`file://`) has the
 * filesystem path there, which matches nothing. The single-file build
 * normalises the path at boot, but `file://` refuses history writes, so that
 * fix cannot apply there. Rather than show a dead end, say what to do.
 */
export default function NotFoundScreen() {
  const openedFromDisk =
    Platform.OS === 'web' && typeof location !== 'undefined' && location.protocol === 'file:';

  return (
    <Screen>
      <Stack.Screen options={{ title: 'Not found' }} />
      {openedFromDisk ? (
        <Panel title="Serve this file over http">
          <Body>
            This build was opened straight from disk, and browsers block the history API on
            `file://` — so the app cannot resolve its own routes.
          </Body>
          <Small style={{ marginTop: 12 }}>
            Open a terminal in the folder containing this file and run:
          </Small>
          <Mono style={{ marginTop: 6 }}>python3 -m http.server 8000</Mono>
          <Small style={{ marginTop: 6 }}>
            Then visit http://localhost:8000/ and open the file from there.
          </Small>
          <Caveat>
            Any static server works — this is a limitation of the file:// protocol, not of the
            app.
          </Caveat>
        </Panel>
      ) : (
        <Panel>
          <Body>That screen does not exist.</Body>
          <Small>The link may be from an older version of the app.</Small>
          <Link href="/" asChild>
            <Button label="Back to jobs" onPress={() => {}} />
          </Link>
        </Panel>
      )}
    </Screen>
  );
}
