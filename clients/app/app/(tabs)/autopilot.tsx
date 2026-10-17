/**
 * Autopilot -- the agent doing the whole job search: find, prep, apply, track.
 *
 * This is the screen a user leaves running. It shows the four stages of the
 * last pass, the applications waiting for a yes or no, and the three switches
 * that decide how hands-off it is. Sending for real is always two explicit
 * steps away: turn off the dry run, then turn off "ask before each send".
 */
import { Link } from 'expo-router';
import { useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import {
  AutopilotPolicy,
  AutopilotReport,
  AutopilotStage,
  QueueItem,
} from '../../src/api';
import {
  Body,
  Button,
  Caveat,
  Chip,
  ErrorNote,
  Panel,
  Row,
  Screen,
  Small,
  StatTile,
  StepLabel,
  Toggle,
} from '../../src/components/ui';
import { useQuery, useSession } from '../../src/hooks';
import { radius, spacing, type, usePalette } from '../../src/theme';

const STAGE_COPY: Record<AutopilotStage['key'], string> = {
  find: 'Searches every connected board and career page, then ranks what fits.',
  prep: 'Tailors a résumé per role from your verified evidence, then fact-checks it.',
  apply: 'Submits through the posting’s own API or address, inside your caps.',
  track: 'Reads replies and moves each application along on its own.',
};

const ORDER: AutopilotStage['key'][] = ['find', 'prep', 'apply', 'track'];

function mode(policy: AutopilotPolicy | undefined): { label: string; tone: 'neutral' | 'warn' | 'ok' } {
  if (!policy?.enabled) return { label: 'Off', tone: 'neutral' };
  if (policy.dry_run) return { label: 'Dry run', tone: 'warn' };
  if (policy.require_approval) return { label: 'Live · asks before sending', tone: 'ok' };
  return { label: 'Live · hands off', tone: 'ok' };
}

export default function AutopilotScreen() {
  const { client } = useSession();
  const p = usePalette();
  const described = useQuery(() => client.autopilot(), []);
  const queue = useQuery(() => client.autopilotQueue(), []);
  const [report, setReport] = useState<AutopilotReport | null>(null);
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [acting, setActing] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const policy = described.data?.policy;
  const status = mode(policy);

  const save = async (field: keyof AutopilotPolicy, value: boolean) => {
    setSaving(String(field));
    setError(null);
    try {
      await client.setAutopilot({ [field]: value } as Partial<AutopilotPolicy>);
      described.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(null);
    }
  };

  const runNow = async () => {
    setRunning(true);
    setError(null);
    setMessage(null);
    try {
      const result = await client.runAutopilot();
      setReport(result);
      queue.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };

  const decide = async (item: QueueItem, yes: boolean) => {
    setActing(item.job_id);
    setError(null);
    try {
      const result = yes ? await client.approve(item.job_id) : await client.decline(item.job_id);
      if (result.outcome === 'submitted') setMessage(`Sent to ${item.company}.`);
      else if (result.outcome === 'would_submit') setMessage('Approved. Still a dry run, so nothing went out.');
      else if (result.outcome === 'declined') setMessage(`Passed on ${item.company}. It will not be sent.`);
      else
        setMessage(
          `Not sent: ${(result.verdict?.blockers ?? []).join('; ') || result.outcome}. It stays in the queue.`,
        );
      queue.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setActing(null);
    }
  };

  const stages = report?.stages ?? null;
  const items = queue.data?.items ?? [];

  return (
    <Screen>
        <View>
          {/* -- hero ---------------------------------------------------- */}
          <View style={{ paddingVertical: spacing.lg }}>
            <Row gap={spacing.sm}>
              <Chip label={status.label} tone={status.tone} />
              {policy?.enabled ? (
                <Small>{`every ${policy.interval_hours}h · up to ${policy.max_per_day} a day`}</Small>
              ) : null}
            </Row>
            <Text style={[type.display, { color: p.ink, marginTop: spacing.md }]}>
              Your job search, running itself.
            </Text>
            <Body muted style={{ marginTop: spacing.sm, maxWidth: 620 }}>
              {described.data?.summary ??
                'One agent finds roles, tailors your résumé, applies and tracks the replies.'}
            </Body>
            <Row gap={spacing.sm} style={{ marginTop: spacing.lg }}>
              <Button
                label={running ? 'Running…' : 'Run a pass now'}
                variant="primary"
                onPress={runNow}
                pending={running}
                disabled={!policy?.enabled}
              />
              {!policy?.enabled ? (
                <Button label="Turn it on" onPress={() => save('enabled', true)} pending={saving === 'enabled'} />
              ) : null}
            </Row>
          </View>

          {described.error ? (
            <ErrorNote message={described.error.message} onRetry={described.refresh} />
          ) : null}
          {error ? <ErrorNote message={error} /> : null}
          {message ? (
            <Panel accent={p.signal}>
              <Body>{message}</Body>
            </Panel>
          ) : null}

          {/* -- the four stages ---------------------------------------- */}
          <Row gap={spacing.md} style={{ alignItems: 'stretch', marginBottom: spacing.md }}>
            {ORDER.map((key, index) => {
              const stage = stages?.find((s) => s.key === key);
              return (
                <View
                  key={key}
                  style={{
                    flexGrow: 1,
                    flexBasis: 150,
                    backgroundColor: p.panel,
                    borderColor: p.line,
                    borderWidth: 1,
                    borderRadius: radius.lg,
                    padding: spacing.lg,
                  }}
                >
                  <StepLabel index={index + 1} label={key} live={running || !!stage?.done} />
                  <Body style={{ ...type.h2, marginTop: spacing.sm }}>
                    {key[0].toUpperCase() + key.slice(1)}
                  </Body>
                  <Small style={{ marginTop: spacing.xs }}>{STAGE_COPY[key]}</Small>
                  {stage ? (
                    <Text style={[type.mono, { color: p.ink, marginTop: spacing.md, fontSize: 12 }]}>
                      {stage.detail}
                    </Text>
                  ) : null}
                </View>
              );
            })}
          </Row>

          {/* -- the approval queue -------------------------------------- */}
          <Panel title={`Waiting for your yes · ${items.length}`}>
            {queue.error ? <ErrorNote message={queue.error.message} onRetry={queue.refresh} /> : null}
            {items.length === 0 && !queue.loading ? (
              <Small>
                {policy?.enabled && !policy.dry_run && policy.require_approval
                  ? 'Nothing waiting. Ready applications land here after each pass.'
                  : 'When it runs live and asks before sending, ready applications wait here.'}
              </Small>
            ) : null}
            {items.map((item, index) => (
              <View
                key={item.job_id}
                style={{
                  borderTopWidth: index === 0 ? 0 : 1,
                  borderTopColor: p.line,
                  paddingVertical: spacing.md,
                }}
              >
                <Link href={`/job?id=${item.job_id}`} asChild>
                  <Pressable accessibilityRole="link">
                    <Body style={{ ...type.title }}>{item.title}</Body>
                    <Small style={{ marginTop: 2 }}>
                      {[
                        item.company,
                        `via ${item.tier}`,
                        item.match_score != null ? `match ${item.match_score}` : null,
                        item.deadline_on ? `closes ${item.deadline_on}` : null,
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                    </Small>
                  </Pressable>
                </Link>
                <Row gap={spacing.sm} style={{ marginTop: spacing.sm }}>
                  <Button
                    label="Approve and send"
                    variant="primary"
                    small
                    onPress={() => decide(item, true)}
                    pending={acting === item.job_id}
                  />
                  <Button label="Pass" small onPress={() => decide(item, false)} disabled={acting === item.job_id} />
                </Row>
              </View>
            ))}
          </Panel>

          {/* -- the last pass ------------------------------------------- */}
          {report ? (
            <Panel title="Last pass">
              {report.skipped_reason ? <Body>{`Skipped: ${report.skipped_reason}`}</Body> : null}
              <Row gap={spacing.sm}>
                <StatTile value={report.counts.considered} label="considered" />
                <StatTile value={report.counts.tailored} label="tailored" />
                <StatTile value={report.counts.submitted} label="sent" />
                <StatTile value={report.counts.awaiting_approval ?? 0} label="waiting" />
                <StatTile value={report.counts.assisted} label="need you" />
              </Row>
              {Object.keys(report.blocked_by).length ? (
                <View style={{ marginTop: spacing.md }}>
                  <Small style={{ fontWeight: '600' }}>Not applied, by reason</Small>
                  {Object.entries(report.blocked_by).map(([reason, count]) => (
                    <Small key={reason} style={{ marginTop: 2 }}>{`${count} × ${reason}`}</Small>
                  ))}
                </View>
              ) : null}
              {report.errors.map((line) => (
                <Small key={line} style={{ color: p.bad, marginTop: 2 }}>{line}</Small>
              ))}
            </Panel>
          ) : null}

          {/* -- the switches -------------------------------------------- */}
          <Panel title="How hands-off">
            <Toggle
              label="Autopilot"
              hint="Searches, ranks and prepares applications on a timer."
              value={!!policy?.enabled}
              onChange={(v) => save('enabled', v)}
              disabled={!policy || saving !== null}
            />
            <Toggle
              label="Dry run"
              hint="Assemble everything and report what would go out. Nothing is sent."
              value={policy?.dry_run ?? true}
              onChange={(v) => save('dry_run', v)}
              disabled={!policy?.enabled || saving !== null}
            />
            <Toggle
              label="Ask before each send"
              hint="Every ready application waits for your approval. Off means fully hands-off."
              value={policy?.require_approval ?? true}
              onChange={(v) => save('require_approval', v)}
              disabled={!policy?.enabled || !!policy?.dry_run || saving !== null}
            />
          </Panel>

          <Panel title="Never switched off">
            {(described.data?.never_configurable ?? []).map((line) => (
              <Small key={line} style={{ marginTop: 2 }}>{`• ${line}`}</Small>
            ))}
            <Caveat>
              Postings behind a web form or login are prepared for you to submit yourself; the agent
              never bypasses a CAPTCHA or bot protection.
            </Caveat>
          </Panel>
        </View>
    </Screen>
  );
}
