/** Pipeline -- the application lifecycle, with inline status changes. */
import { useState } from 'react';
import { View } from 'react-native';

import { Table } from '../../src/components/Table';
import { Body, Button, Chip, ListScreen, Panel, Row, Small } from '../../src/components/ui';
import { ApplicationRow } from '../../src/api';
import { relativeDate, score, titleCase } from '../../src/format';
import { useAction, useQuery, useSession } from '../../src/hooks';
import { spacing, type } from '../../src/theme';

/** The transitions worth one tap; the full set of 20 lives in the API. */
const QUICK_STATUSES = ['saved', 'applied', 'interview', 'offer', 'rejected', 'withdrawn'];

export default function ApplicationsScreen() {
  const { client } = useSession();
  const [filter, setFilter] = useState<string | undefined>();
  const applications = useQuery(() => client.applications(filter), [filter]);
  const [expanded, setExpanded] = useState<number | null>(null);
  const setStatus = useAction((id: number, status: string) =>
    client.setStatus(id, status, 'Changed from the app'),
  );

  const rows = applications.data?.applications ?? [];
  const counts = rows.reduce<Record<string, number>>((acc, row) => {
    acc[row.status] = (acc[row.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <ListScreen
      header={
        <View>
          <Panel title="Pipeline">
            <Row>
              {Object.entries(counts).map(([status, count]) => (
                <Chip key={status} label={`${titleCase(status)} ${count}`} />
              ))}
              {rows.length === 0 ? <Small>No applications tracked yet.</Small> : null}
            </Row>
          </Panel>
        </View>
      }
      items={rows}
      loading={applications.loading && !applications.data}
      error={applications.error?.message}
      onRetry={applications.refresh}
      emptyMessage="No applications yet. Run the daily pipeline or prepare one from a job."
      renderItem={(row: ApplicationRow) => (
        <Panel key={row.id}>
          <Body style={{ ...type.title }}>{row.title}</Body>
          <Small>{`${row.company} · ${row.country} · priority ${score(row.priority)}`}</Small>
          <Row style={{ marginTop: spacing.sm }} gap={spacing.xs + 1}>
            <Chip label={titleCase(row.status)} tone="accent" />
            {row.applied_on ? <Chip label={`applied ${relativeDate(row.applied_on)}`} /> : null}
            {row.last_activity_on ? <Chip label={`activity ${relativeDate(row.last_activity_on)}`} /> : null}
          </Row>
          {row.notes ? <Small style={{ marginTop: spacing.sm }}>{row.notes}</Small> : null}

          <Row style={{ marginTop: spacing.md }} gap={spacing.xs + 2}>
            <Button
              label={expanded === row.id ? 'Close' : 'Change status'}
              small
              onPress={() => setExpanded(expanded === row.id ? null : row.id)}
            />
          </Row>

          {expanded === row.id ? (
            <Row style={{ marginTop: spacing.sm }} gap={spacing.xs + 2}>
              {QUICK_STATUSES.filter((status) => status !== row.status).map((status) => (
                <Button
                  key={status}
                  label={titleCase(status)}
                  small
                  pending={setStatus.pending}
                  onPress={async () => {
                    await setStatus.invoke(row.id, status);
                    setExpanded(null);
                    applications.refresh();
                  }}
                />
              ))}
            </Row>
          ) : null}
          {setStatus.error && expanded === row.id ? (
            <Small style={{ marginTop: spacing.sm }}>{setStatus.error.message}</Small>
          ) : null}
        </Panel>
      )}
    />
  );
}
