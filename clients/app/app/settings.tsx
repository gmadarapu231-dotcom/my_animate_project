/**
 * Settings -- where the server is and the token to reach it.
 *
 * A phone cannot assume `localhost`, so this screen exists on every platform.
 * It also reports whether the server requires a token, which is the fastest
 * way to diagnose a 401.
 */
import { useEffect, useState } from 'react';
import { View } from 'react-native';

import { Field } from '../src/components/Field';
import { Body, Button, Caveat, Chip, Divider, Panel, Row, Screen, Small } from '../src/components/ui';
import { useSession } from '../src/hooks';
import { defaultBaseUrl, tokenStorageNote } from '../src/settings';
import { spacing, type } from '../src/theme';

export default function SettingsScreen() {
  const { settings, update, health, healthError, recheck, ready, demo } = useSession();
  const [baseUrl, setBaseUrl] = useState(settings.baseUrl);
  const [token, setToken] = useState(settings.token ?? '');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (ready) {
      setBaseUrl(settings.baseUrl);
      setToken(settings.token ?? '');
    }
  }, [ready, settings.baseUrl, settings.token]);

  const save = async () => {
    await update({ baseUrl: baseUrl.trim().replace(/\/+$/, ''), token: token.trim() || null });
    setSaved(true);
    recheck();
    setTimeout(() => setSaved(false), 2500);
  };

  if (demo) {
    return (
      <Screen>
        <Panel title="Demo build">
          <Body>This build has no server to configure.</Body>
          <Small style={{ marginTop: spacing.sm }}>
            Every score, verdict, ATS keyword and factuality result you can see was produced by
            the real engines against the sample profile and job set, then frozen into the bundle.
          </Small>
          <Small style={{ marginTop: spacing.sm }}>
            What is not live: natural-language search answers only the recorded queries, the agent
            replays a transcript instead of reasoning, and status changes are lost on reload.
          </Small>
          <Caveat>
            Run it for real with: careeros serve — then this screen takes a server address and an
            API token.
          </Caveat>
        </Panel>
      </Screen>
    );
  }

  return (
    <Screen>
      <Panel title="Server">
        <Field
          label="API base URL"
          value={baseUrl}
          onChangeText={setBaseUrl}
          placeholder="http://192.168.1.20:8000"
          keyboardType="url"
          hint={`Default for this device: ${defaultBaseUrl()}`}
        />
        <View style={{ marginTop: spacing.md }}>
          <Field
            label="API token"
            value={token}
            onChangeText={setToken}
            placeholder="only if CAREEROS_API_TOKEN is set on the server"
            secure
            hint={tokenStorageNote}
          />
        </View>
        <Row style={{ marginTop: spacing.md }} gap={spacing.sm}>
          <Button label="Save" variant="primary" small onPress={save} />
          <Button label="Test connection" small onPress={recheck} />
          {saved ? <Chip label="Saved" tone="ok" /> : null}
        </Row>
      </Panel>

      <Panel title="Connection">
        {health ? (
          <>
            <Row gap={spacing.sm}>
              <Chip label="connected" tone="ok" />
              <Chip label={`auth ${health.auth_required ? 'required' : 'open'}`} tone={health.auth_required ? 'warn' : 'neutral'} />
              <Chip label={`db: ${health.database}`} />
            </Row>
            <Small style={{ marginTop: spacing.sm }}>
              {`Countries: ${health.countries.join(', ')} · ${health.seed_domains} seed domains · ${health.skill_nodes} skills`}
            </Small>
            <Small>
              {`AI layer: ${health.llm_available ? health.llm_provider : 'heuristics only'}`}
            </Small>
            {!health.llm_available ? (
              <Caveat>
                The Agent tab needs model access on the server (ANTHROPIC_API_KEY). Everything
                else — ranking, matching, ATS, resumes — works without it.
              </Caveat>
            ) : null}
          </>
        ) : (
          <>
            <Row gap={spacing.sm}>
              <Chip label="not connected" tone="bad" />
            </Row>
            {healthError ? <Small style={{ marginTop: spacing.sm }}>{healthError}</Small> : null}
            <Divider />
            <Body style={{ ...type.small }}>To connect from a phone:</Body>
            <Small style={{ marginTop: spacing.xs }}>
              {'1. Start the server so it listens on your network:\n   careeros serve --host 0.0.0.0'}
            </Small>
            <Small style={{ marginTop: spacing.xs }}>
              {'2. Set a token first — the API is otherwise open to your LAN:\n   export CAREEROS_API_TOKEN=$(openssl rand -hex 24)'}
            </Small>
            <Small style={{ marginTop: spacing.xs }}>
              {'3. Put your computer’s LAN address above, e.g. http://192.168.1.20:8000'}
            </Small>
          </>
        )}
      </Panel>
    </Screen>
  );
}
