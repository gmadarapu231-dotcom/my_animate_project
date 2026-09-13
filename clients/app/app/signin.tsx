/**
 * Sign in with an email identity.
 *
 * Two routes, and neither is a password: Google's consent screen, or a code
 * emailed to the address. The screen asks the server which it supports rather
 * than assuming, so a server with only one configured shows only that one, and
 * a server with neither says exactly which variables an operator has to set
 * instead of failing silently.
 *
 * Google's redirect cannot come back into a native app, so the flow ends on a
 * page that shows the session token. On web an opener window posts it back
 * automatically; everywhere else it is pasted. That is deliberately clumsy and
 * deliberately honest -- it beats a fake success screen.
 */
import * as Linking from 'expo-linking';
import { router } from 'expo-router';
import { useEffect, useState } from 'react';
import { Platform, View } from 'react-native';

import { Field } from '../src/components/Field';
import {
  Body,
  Button,
  Caveat,
  Divider,
  ErrorNote,
  Loading,
  Mono,
  Panel,
  Row,
  Screen,
  SectionTitle,
  Small,
} from '../src/components/ui';
import { ApiError, AuthDescription, SignInResult } from '../src/api';
import { useSession } from '../src/hooks';
import { spacing, usePalette } from '../src/theme';

type Stage = 'choose' | 'code_sent' | 'awaiting_paste' | 'done';

export default function SignIn() {
  const session = useSession();
  const p = usePalette();

  const [describe, setDescribe] = useState<AuthDescription | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [stage, setStage] = useState<Stage>('choose');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [pasted, setPasted] = useState('');
  const [includeGmail, setIncludeGmail] = useState(false);
  const [signedIn, setSignedIn] = useState<SignInResult['user'] | null>(null);

  useEffect(() => {
    let live = true;
    session.client
      .authDescribe()
      .then((d) => live && setDescribe(d))
      .catch((e: ApiError) => live && setLoadError(e.message));
    return () => {
      live = false;
    };
  }, [session.client]);

  // The callback page posts the token to its opener; on web that is us.
  useEffect(() => {
    if (Platform.OS !== 'web' || typeof window === 'undefined') return;
    function onMessage(event: MessageEvent) {
      const data = event.data as { type?: string; token?: string };
      if (data?.type === 'careeros-auth' && typeof data.token === 'string') {
        void adopt(data.token);
      }
    }
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  });

  async function adopt(token: string) {
    setError(null);
    setBusy('adopt');
    try {
      await session.update({ ...session.settings, token: token.trim() });
      setStage('done');
      setNotice('Signed in.');
      session.recheck();
      setTimeout(() => router.replace('/'), 600);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function startGoogle() {
    setError(null);
    setBusy('google');
    try {
      const started = await session.client.googleStart(includeGmail);
      setStage('awaiting_paste');
      setNotice(
        Platform.OS === 'web'
          ? 'Approve in the Google window, then come back here.'
          : 'Approve in the browser, then copy the token it shows and paste it below.',
      );
      if (Platform.OS === 'web' && typeof window !== 'undefined') {
        window.open(started.authorization_url, 'careeros-google', 'width=520,height=680');
      } else {
        await Linking.openURL(started.authorization_url);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
      setStage('choose');
    } finally {
      setBusy(null);
    }
  }

  async function sendCode() {
    setError(null);
    setBusy('code');
    try {
      const started = await session.client.emailCodeStart(email.trim());
      setStage('code_sent');
      const minutes = Math.round(started.expires_in_seconds / 60);
      setNotice(`A ${started.code_length}-digit code is on its way to ${started.sent_to}. It expires in ${minutes} minutes.`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function verifyCode() {
    setError(null);
    setBusy('verify');
    try {
      const result = await session.client.emailCodeVerify(email.trim(), code.trim());
      setSignedIn(result.user);
      await adopt(result.token);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (loadError) {
    return (
      <Screen>
        <ErrorNote message={`Cannot ask the server how to sign in. ${loadError}`} />
        <Panel title="Where is the server?">
          <Body muted>
            The app is pointed at <Mono>{session.settings.baseUrl}</Mono>. Change it in Settings if
            that is not right for this device.
          </Body>
          <Row style={{ marginTop: spacing.md }}>
            <Button label="Settings" onPress={() => router.push('/settings')} />
          </Row>
        </Panel>
      </Screen>
    );
  }

  if (!describe) return <Loading label="Asking the server how to sign in…" />;

  const googleReady = describe.google.available;
  const codeReady = describe.email_code.available;
  const nothingConfigured = !googleReady && !codeReady;

  return (
    <Screen>
      <Panel>
        <SectionTitle>CareerOS</SectionTitle>
        <Body style={{ fontSize: 17, fontWeight: '600' }}>Sign in with your email</Body>
        <Body muted style={{ marginTop: spacing.xs }}>
          {describe.mode === 'open'
            ? 'This server does not require sign-in, so you can browse without one. Signing in still gives you your own account and profile.'
            : 'Your account, your evidence, your applications. Nothing is shared between accounts.'}
        </Body>
        <Caveat>No passwords: this server has nowhere to store one.</Caveat>
      </Panel>

      {notice ? (
        <Panel accent={p.accent}>
          <Body>{notice}</Body>
          {signedIn ? <Small>{signedIn.email}</Small> : null}
        </Panel>
      ) : null}

      {error ? <ErrorNote message={error} /> : null}

      {nothingConfigured ? (
        <Panel title="Sign-in is not set up on this server">
          <Body muted>
            An operator needs to configure one of the two methods. Until then the API is
            {describe.mode === 'open' ? ' open on this machine and you can carry on.' : ' closed.'}
          </Body>
          <Divider />
          <SectionTitle>For Google sign-in, set</SectionTitle>
          {describe.google.missing_env.map((name) => (
            <Mono key={name}>{name}</Mono>
          ))}
          <Divider />
          <SectionTitle>For emailed codes, set</SectionTitle>
          {describe.email_code.missing_env.map((name) => (
            <Mono key={name}>{name}</Mono>
          ))}
          {describe.mode === 'open' ? (
            <Row style={{ marginTop: spacing.md }}>
              <Button label="Browse without signing in" variant="primary" onPress={() => router.replace('/')} />
            </Row>
          ) : null}
        </Panel>
      ) : null}

      {googleReady ? (
        <Panel title="Sign in with Google">
          <Body muted>
            Google confirms the address; this server never sees a Google password.
          </Body>
          <View style={{ marginTop: spacing.md }}>
            <Row>
              <Button
                label={includeGmail ? '☑  Also connect Gmail' : '☐  Also connect Gmail'}
                small
                onPress={() => setIncludeGmail((v) => !v)}
              />
            </Row>
            <Small style={{ marginTop: spacing.xs }}>
              {includeGmail
                ? 'One consent covers sign-in and reading job mail. Read and draft only — never send.'
                : 'Sign-in only. You can connect Gmail later.'}
            </Small>
          </View>
          <Row style={{ marginTop: spacing.md }}>
            <Button
              label="Continue with Google"
              variant="primary"
              pending={busy === 'google'}
              onPress={startGoogle}
            />
          </Row>
        </Panel>
      ) : null}

      {stage === 'awaiting_paste' ? (
        <Panel title="Finish signing in" accent={p.warn}>
          <Body muted>
            Google sends you to a page showing a session token. Paste it here.
            {Platform.OS === 'web' ? ' If the window posted it back automatically, you are already in.' : ''}
          </Body>
          <View style={{ marginTop: spacing.md }}>
            <Field
              label="Session token"
              value={pasted}
              onChangeText={setPasted}
              placeholder="cos1.…"
              multiline
            />
          </View>
          <Row style={{ marginTop: spacing.md }}>
            <Button
              label="Sign in"
              variant="primary"
              pending={busy === 'adopt'}
              disabled={!pasted.trim().startsWith('cos1.')}
              onPress={() => adopt(pasted)}
            />
            <Button label="Start over" small onPress={() => { setStage('choose'); setNotice(null); }} />
          </Row>
        </Panel>
      ) : null}

      {codeReady ? (
        <Panel title="Or get a code by email">
          <Field
            label="Email address"
            value={email}
            onChangeText={setEmail}
            placeholder="you@example.com"
            keyboardType="default"
            onSubmitEditing={sendCode}
          />
          {stage === 'code_sent' ? (
            <View style={{ marginTop: spacing.md }}>
              <Field
                label={`${describe.email_code.code_length}-digit code`}
                value={code}
                onChangeText={setCode}
                placeholder="000000"
                onSubmitEditing={verifyCode}
              />
              <Row style={{ marginTop: spacing.md }}>
                <Button
                  label="Sign in"
                  variant="primary"
                  pending={busy === 'verify'}
                  disabled={code.trim().length < describe.email_code.code_length}
                  onPress={verifyCode}
                />
                <Button label="Send another" small pending={busy === 'code'} onPress={sendCode} />
              </Row>
            </View>
          ) : (
            <Row style={{ marginTop: spacing.md }}>
              <Button
                label="Email me a code"
                pending={busy === 'code'}
                disabled={!email.includes('@')}
                onPress={sendCode}
              />
            </Row>
          )}
        </Panel>
      ) : null}

      <Panel title="Already have a token?">
        <Body muted>
          `careeros signin you@example.com` on the server prints one. Paste it in Settings.
        </Body>
        <Row style={{ marginTop: spacing.md }}>
          <Button label="Open Settings" small onPress={() => router.push('/settings')} />
        </Row>
      </Panel>
    </Screen>
  );
}
