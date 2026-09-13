/**
 * Where the jobs come from.
 *
 * The screen's real job is to make sourcing legible: which boards are feeding
 * the list, which are one credential away, and which are deliberately not
 * scraped and what carries their postings instead. A board missing from the
 * results should never be a mystery, so the refused ones are shown as
 * prominently as the working ones, each with its reason.
 */
import { router } from 'expo-router';
import { useState } from 'react';
import { View } from 'react-native';

import {
  Body,
  Button,
  Caveat,
  Chip,
  Divider,
  Empty,
  ErrorNote,
  FilterChip,
  Loading,
  Mono,
  Panel,
  Row,
  Screen,
  SectionTitle,
  Small,
  StatTile,
} from '../../src/components/ui';
import { ProviderRow, ProviderState } from '../../src/api';
import { useAction, useQuery, useSession } from '../../src/hooks';
import { spacing, usePalette } from '../../src/theme';

const STATE_LABEL: Record<ProviderState, string> = {
  ready: 'Ready',
  needs_credentials: 'Needs a key',
  not_permitted: 'Not fetched',
};

export default function Sources() {
  const session = useSession();
  const p = usePalette();
  const [country, setCountry] = useState<string | undefined>(undefined);
  const [filter, setFilter] = useState<ProviderState | 'all'>('all');

  const status = useQuery(() => session.client.sources(country), [country]);
  const plan = useQuery(() => session.client.searchPlan().catch(() => null), []);
  const discovery = useAction(() => session.client.discover({ per_provider: 40 }));

  if (status.loading) return <Loading label="Asking the server where jobs come from…" />;
  if (status.error) {
    return (
      <Screen>
        <ErrorNote message={status.error.message} onRetry={status.refresh} />
        {status.error.isAuthError ? (
          <Panel title="Sign in first">
            <Body muted>This server wants an account before it will talk.</Body>
            <Row style={{ marginTop: spacing.md }}>
              <Button label="Sign in" variant="primary" onPress={() => router.push('/signin')} />
            </Row>
          </Panel>
        ) : null}
      </Screen>
    );
  }
  if (!status.data) return <Empty message="No provider information came back." />;

  const rows = status.data.providers.filter((r) => filter === 'all' || r.state === filter);
  const report = discovery.result;

  return (
    <Screen>
      <Panel>
        <Row gap={spacing.md}>
          <StatTile value={status.data.ready} label="Ready" />
          <StatTile value={status.data.needs_credentials} label="Need a key" />
          <StatTile value={status.data.not_permitted} label="Not fetched" />
          <StatTile value={status.data.count} label="Known" />
        </Row>
        <Caveat>
          Nothing here bypasses a login wall, a CAPTCHA or bot protection. Boards that forbid
          automated collection are listed with the route that carries them instead.
        </Caveat>
      </Panel>

      {plan.data ? (
        <Panel title="What would be searched for you">
          <Body>
            {plan.data.terms.length
              ? plan.data.terms.join(' · ')
              : 'No search terms yet — add career tracks to your profile.'}
          </Body>
          <Small style={{ marginTop: spacing.xs }}>
            in {plan.data.countries.join(' and ') || '—'} ·{' '}
            {plan.data.searches.length} search{plan.data.searches.length === 1 ? '' : 'es'} across{' '}
            {plan.data.ready_providers.length} ready provider
            {plan.data.ready_providers.length === 1 ? '' : 's'}
          </Small>
          <Caveat>{plan.data.note}</Caveat>
          <Row style={{ marginTop: spacing.md }}>
            <Button
              label={discovery.pending ? 'Searching…' : 'Find jobs now'}
              variant="primary"
              pending={discovery.pending}
              disabled={!plan.data.ready_providers.length}
              onPress={async () => {
                await discovery.invoke();
                status.refresh();
              }}
            />
          </Row>
          {!plan.data.ready_providers.length ? (
            <Small style={{ marginTop: spacing.sm }}>
              Nothing is credentialled yet. The four keyless providers below work immediately;
              everything else needs its own key.
            </Small>
          ) : null}
        </Panel>
      ) : null}

      {discovery.error ? <ErrorNote message={discovery.error.message} /> : null}

      {report ? (
        <Panel title="Last search" accent={p.accent}>
          <Body style={{ fontWeight: '600' }}>
            {report.found} posting{report.found === 1 ? '' : 's'} found
            {report.ingest ? ` · ${report.ingest.inserted} new, ${report.ingest.duplicates} already known` : ''}
          </Body>
          {Object.keys(report.by_board).length ? (
            <View style={{ marginTop: spacing.sm }}>
              <SectionTitle>By board</SectionTitle>
              <Row>
                {Object.entries(report.by_board).map(([board, count]) => (
                  <Chip key={board} label={`${board} · ${count}`} />
                ))}
              </Row>
            </View>
          ) : null}
          <Divider />
          {report.providers.map((row) => (
            <View key={row.provider} style={{ marginBottom: spacing.sm }}>
              <Small>
                {row.skipped_reason ? '—' : '✓'} {row.label}
                {row.skipped_reason ? '' : ` · ${row.found} from ${row.searches} search(es)`}
              </Small>
              {row.skipped_reason ? <Small style={{ color: p.muted }}>{row.skipped_reason}</Small> : null}
              {row.errors.slice(0, 1).map((err) => (
                <Small key={err} style={{ color: p.bad }}>
                  {err}
                </Small>
              ))}
            </View>
          ))}
          {report.ingest && report.ingest.inserted > 0 ? (
            <Row style={{ marginTop: spacing.sm }}>
              <Button label="See the jobs" variant="primary" small onPress={() => router.push('/')} />
            </Row>
          ) : null}
        </Panel>
      ) : null}

      <Panel title="Providers">
        <Row>
          <FilterChip label="All" active={filter === 'all'} onPress={() => setFilter('all')} />
          {(['ready', 'needs_credentials', 'not_permitted'] as ProviderState[]).map((s) => (
            <FilterChip
              key={s}
              label={STATE_LABEL[s]}
              active={filter === s}
              onPress={() => setFilter(s)}
            />
          ))}
        </Row>
        <Row style={{ marginTop: spacing.sm }}>
          <FilterChip label="Any country" active={!country} onPress={() => setCountry(undefined)} />
          <FilterChip label="United States" active={country === 'US'} onPress={() => setCountry('US')} />
          <FilterChip label="India" active={country === 'IN'} onPress={() => setCountry('IN')} />
        </Row>
      </Panel>

      {rows.map((row) => (
        <ProviderCard key={row.id} row={row} />
      ))}
      {!rows.length ? <Empty message="No providers in that group." /> : null}
    </Screen>
  );
}

function ProviderCard({ row }: { row: ProviderRow }) {
  const p = usePalette();
  const [open, setOpen] = useState(false);
  const accent =
    row.state === 'ready' ? p.ok : row.state === 'needs_credentials' ? p.warn : p.bad;

  return (
    <Panel accent={accent}>
      <Row style={{ justifyContent: 'space-between' }}>
        <Body style={{ fontWeight: '600', flexShrink: 1 }}>{row.label}</Body>
        <Chip
          label={STATE_LABEL[row.state]}
          tone={row.state === 'ready' ? 'ok' : row.state === 'needs_credentials' ? 'warn' : 'bad'}
        />
      </Row>
      <Small>
        {row.access_label} · {row.countries.join(', ')}
        {row.endpoint_verified ? '' : ' · endpoint unverified'}
      </Small>

      {row.state === 'needs_credentials' ? (
        <View style={{ marginTop: spacing.sm }}>
          <SectionTitle>Set on the server</SectionTitle>
          {row.missing_env.map((name) => (
            <Mono key={name}>{name}</Mono>
          ))}
        </View>
      ) : null}

      {row.state === 'not_permitted' ? (
        <View style={{ marginTop: spacing.sm }}>
          <Body muted>{row.reason}</Body>
          {row.use_instead.length ? (
            <View style={{ marginTop: spacing.sm }}>
              <SectionTitle>Carried instead by</SectionTitle>
              <Row>
                {row.use_instead.map((id) => (
                  <Chip key={id} label={id} tone="accent" />
                ))}
              </Row>
            </View>
          ) : null}
          {row.partner_route ? <Caveat>{row.partner_route}</Caveat> : null}
        </View>
      ) : null}

      {row.notes ? (
        <View style={{ marginTop: spacing.sm }}>
          {open ? <Small>{row.notes}</Small> : null}
          <Row style={{ marginTop: spacing.xs }}>
            <Button
              label={open ? 'Hide detail' : 'What is this?'}
              small
              onPress={() => setOpen((v) => !v)}
            />
          </Row>
        </View>
      ) : null}
    </Panel>
  );
}
