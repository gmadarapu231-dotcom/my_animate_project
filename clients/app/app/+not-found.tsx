import { Link, Stack } from 'expo-router';

import { Body, Button, Panel, Screen, Small } from '../src/components/ui';

export default function NotFoundScreen() {
  return (
    <Screen>
      <Stack.Screen options={{ title: 'Not found' }} />
      <Panel>
        <Body>That screen does not exist.</Body>
        <Small>The link may be from an older version of the app.</Small>
        <Link href="/" asChild>
          <Button label="Back to jobs" onPress={() => {}} />
        </Link>
      </Panel>
    </Screen>
  );
}
