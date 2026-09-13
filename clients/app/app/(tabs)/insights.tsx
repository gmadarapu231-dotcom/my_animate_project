/**
 * Insights -- the funnel and its breakdowns.
 *
 * Low-confidence groups are labelled rather than hidden: a 100% response rate
 * on three applications is not a finding, and the UI says so instead of
 * letting the number speak.
 */
import { View } from 'react-native';

import { Column, Table } from '../../src/components/Table';
import { Caveat, Chip, ListScreen, Panel, Row, Small, StatTile } from '../../src/components/ui';
import { FunnelStats } from '../../src/api';
import { useQuery, useSession } from '../../src/hooks';
import { spacing } from '../../src/theme';

const COLUMNS: Column<FunnelStats>[] = [
  { key: 'label', header: 'Group', width: 2.2, render: (r) => (r.confident ? r.label : `${r.label} (low sample)`) },
  { key: 'apps', header: 'Apps', align: 'right', render: (r) => String(r.applications) },
  { key: 'int', header: 'Intv', align: 'right', render: (r) => String(r.interviews) },
  { key: 'off', header: 'Offers', align: 'right', render: (r) => String(r.offers) },
  { key: 'resp', header: 'Resp %', align: 'right', render: (r) => String(r.response_rate) },
];

export default function InsightsScreen() {
  const { client } = useSession();
  const analytics = useQuery(() => client.analytics(), []);
  const data = analytics.data;
  const overall = data?.overall;

  const breakdowns: [string, FunnelStats[]][] = data
    ? [
        ['By career track', data.by_track],
        ['By domain', data.by_domain],
        ['By country', data.by_country],
        ['By visa verdict', data.by_visa_verdict],
        ['By match band', data.by_match_band],
        ['By source', data.by_source],
      ]
    : [];

  return (
    <ListScreen
      header={
        <View>
          <Panel title="Funnel">
            {overall ? (
              <>
                <Row gap={spacing.sm}>
                  <StatTile value={overall.applications} label="Applications" />
                  <StatTile value={overall.responses} label="Responses" />
                  <StatTile value={overall.interviews} label="Interviews" />
                  <StatTile value={overall.offers} label="Offers" />
                  <StatTile value={overall.rejections} label="Rejections" />
                </Row>
                <Row gap={spacing.sm} style={{ marginTop: spacing.sm }}>
                  <StatTile value={`${overall.response_rate}%`} label="Response rate" />
                  <StatTile value={`${overall.interview_rate}%`} label="Interview rate" />
                  <StatTile value={`${overall.offer_rate}%`} label="Offer rate" />
                </Row>
                {overall.note ? <Caveat>{overall.note}</Caveat> : null}
              </>
            ) : (
              <Small>No application history yet.</Small>
            )}
          </Panel>

          {breakdowns
            .filter(([, rows]) => rows.length > 0)
            .map(([title, rows]) => (
              <Panel key={title} title={title}>
                <Table columns={COLUMNS} rows={rows} />
              </Panel>
            ))}

          {data?.rejections ? (
            <Panel title="Rejections">
              <Small>
                {`${data.rejections.total} recorded · ${data.rejections.explicit_count} with a reason stated by the employer.`}
              </Small>
              {data.rejections.explicit_reasons.map((reason, index) => (
                <Small key={index} style={{ marginTop: spacing.sm }}>{`Stated: “${reason}”`}</Small>
              ))}
              {data.rejections.hypotheses.length ? (
                <View style={{ marginTop: spacing.md }}>
                  <Row gap={spacing.xs + 1}>
                    {data.rejections.hypotheses.map((item) => (
                      <Chip key={item.reason} label={`${item.reason} (${item.count}×)`} tone="warn" />
                    ))}
                  </Row>
                  {/* Fact and guess are kept apart here exactly as in the API. */}
                  <Caveat>
                    These are CareerOS hypotheses, not reasons given by any employer.
                  </Caveat>
                </View>
              ) : null}
            </Panel>
          ) : null}
        </View>
      }
      items={[]}
      renderItem={() => null}
      emptyMessage=""
      loading={analytics.loading && !analytics.data}
      error={analytics.error?.message}
      onRetry={analytics.refresh}
    />
  );
}
