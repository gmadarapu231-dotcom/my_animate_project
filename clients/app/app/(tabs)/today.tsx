/** Today -- "what should I apply for today?" plus the career learning loop. */
import { Link } from 'expo-router';
import { Pressable, View } from 'react-native';

import { Body, Button, Caveat, Chip, ErrorNote, ListScreen, Panel, Row, Small } from '../../src/components/ui';
import { useQuery, useSession } from '../../src/hooks';
import { titleCase } from '../../src/format';
import { spacing, type, usePalette } from '../../src/theme';

export default function TodayScreen() {
  const { client } = useSession();
  const p = usePalette();
  const recommendation = useQuery(() => client.recommendations(true), []);
  const learning = useQuery(() => client.learning(), []);
  const profile = useQuery(() => client.profile(), []);

  const signals = learning.data?.signals ?? [];
  const topTrack =
    profile.data?.career_tracks.find((track) => track.id === recommendation.data?.top_track_id)
      ?.name ?? null;

  return (
    <ListScreen
      header={
        <View>
          <Panel title="What should I apply for today?">
            {recommendation.error ? (
              <ErrorNote message={recommendation.error.message} onRetry={recommendation.refresh} />
            ) : null}
            {recommendation.data?.track_reasons?.length ? (
              <View style={{ marginBottom: spacing.md }}>
                <Body style={{ ...type.h2 }}>
                  {topTrack ? `Top career track today: ${topTrack}` : 'Top jobs today'}
                </Body>
                {recommendation.data.track_reasons.map((reason) => (
                  <Small key={reason} style={{ marginTop: 2 }}>{`• ${reason}`}</Small>
                ))}
              </View>
            ) : null}

            {(recommendation.data?.top_jobs ?? []).map((job, index) => (
              <Link key={job.job_id} href={`/job?id=${job.job_id}`} asChild>
                <Pressable accessibilityRole="link">
                  {({ pressed }) => (
                    <View
                      style={{
                        borderTopWidth: index === 0 ? 0 : 1,
                        borderTopColor: p.line,
                        paddingVertical: spacing.md,
                        opacity: pressed ? 0.7 : 1,
                      }}
                    >
                      <Row gap={spacing.sm}>
                        <Chip label={`#${index + 1}`} tone="accent" />
                        <Body style={{ ...type.title, flexShrink: 1 }}>{job.title}</Body>
                      </Row>
                      <Small style={{ marginTop: 2 }}>
                        {`${job.company} · ${job.flag} · priority ${Math.round(job.priority)}`}
                      </Small>
                      {job.why ? <Small style={{ marginTop: 4 }}>{job.why}</Small> : null}
                    </View>
                  )}
                </Pressable>
              </Link>
            ))}

            {!recommendation.loading && !recommendation.data?.top_jobs?.length ? (
              <Small>No actionable jobs today. Run an ingest, or widen your career tracks.</Small>
            ) : null}

            <View style={{ marginTop: spacing.md, alignSelf: 'flex-start' }}>
              <Button label="Refresh" small onPress={recommendation.refresh} pending={recommendation.loading} />
            </View>
          </Panel>

          <Panel title="Career learning signals">
            {signals.length === 0 ? (
              <Small>
                Not enough outcome history yet — apply to a few roles and check back.
              </Small>
            ) : (
              signals.map((signal) => (
                <View key={`${signal.kind}-${signal.subject}`} style={{ marginBottom: spacing.md }}>
                  <Row gap={spacing.sm}>
                    <Chip
                      label={titleCase(signal.kind)}
                      tone={signal.kind === 'targeting' ? 'ok' : 'neutral'}
                    />
                    <Body style={{ fontWeight: '600', flexShrink: 1 }}>{signal.subject}</Body>
                  </Row>
                  <Small style={{ marginTop: 3 }}>{signal.rationale}</Small>
                  <Small>{`confidence: ${signal.confidence}`}</Small>
                </View>
              ))
            )}
            {learning.data?.disclaimer ? <Caveat>{learning.data.disclaimer}</Caveat> : null}
          </Panel>
        </View>
      }
      items={[]}
      renderItem={() => null}
      emptyMessage=""
      loading={false}
    />
  );
}
