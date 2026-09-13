/**
 * The job card -- the product's primary unit of information.
 *
 * Carries everything a decision needs: identity, the deadline flag, six
 * scores rather than one composite, and the work-authorization verdict *with
 * the posting phrase that produced it*. The verdict is inline and not behind a
 * tap, because it is the output most likely to be wrong in a way that costs
 * the user a real opportunity.
 */
import { Link } from 'expo-router';
import { Pressable, View } from 'react-native';

import { JobCard as Job } from '../api';
import { deadlineFlag, score } from '../format';
import { bucketColor, spacing, type, usePalette, verdictColor } from '../theme';
import { Body, Caveat, Chip, Panel, Row, ScoreTile, Small } from './ui';

export function VerdictBanner({ job, compact }: { job: Job; compact?: boolean }) {
  const p = usePalette();
  const eligibility = job.eligibility;
  if (!eligibility) return null;
  const colour = verdictColor(eligibility.verdict, p);
  const quote = eligibility.evidence?.[0]?.quote;

  return (
    <View
      style={{
        backgroundColor: p.panelAlt,
        borderLeftWidth: 3,
        borderLeftColor: colour,
        borderRadius: 6,
        padding: spacing.md - 2,
        marginTop: spacing.md,
      }}
    >
      <Body style={{ color: colour, fontWeight: '600', fontSize: 13 }}>
        {`Work authorization: ${eligibility.verdict.replace(/_/g, ' ')}`}
      </Body>
      {eligibility.reasons?.[0] ? (
        <Small style={{ marginTop: 3 }}>{eligibility.reasons[0]}</Small>
      ) : null}
      {quote && !compact ? <Caveat>{`Source: “${quote}”`}</Caveat> : null}
      {/* Never dismissible: the verdict is an assessment, not a ruling. */}
      <Caveat>{eligibility.disclaimer}</Caveat>
    </View>
  );
}

export function JobCardView({ job }: { job: Job }) {
  const p = usePalette();
  const priority = job.priority;
  const classification = job.classification;

  return (
    <Link href={`/job?id=${job.id}`} asChild>
      <Pressable accessibilityRole="link">
        {({ pressed }) => (
          <Panel
            accent={bucketColor(priority?.bucket, p)}
            style={{ opacity: priority?.bucket === 'expired' ? 0.6 : pressed ? 0.85 : 1 }}
          >
            <View style={{ flexDirection: 'row', justifyContent: 'space-between', gap: spacing.sm }}>
              <View style={{ flex: 1 }}>
                <Body style={{ ...type.title }}>{job.title}</Body>
                <Small>{`${job.company} · ${job.location || job.country_name} · ${job.source}`}</Small>
              </View>
              {priority ? (
                <Small style={{ ...type.tiny, color: bucketColor(priority.bucket, p), textAlign: 'right' }}>
                  {deadlineFlag(priority.bucket)}
                </Small>
              ) : null}
            </View>

            <Row style={{ marginTop: spacing.md }} gap={spacing.xs + 1}>
              <Chip label={classification?.domain_label ?? 'unclassified'} tone="accent" />
              {classification?.seniority ? <Chip label={classification.seniority} /> : null}
              <Chip label={job.country} />
              {job.work_arrangement ? <Chip label={job.work_arrangement} /> : null}
              {job.employment_type ? <Chip label={job.employment_type} /> : null}
              <Chip label={job.salary.display} />
              {job.applicant_count !== null ? <Chip label={`${job.applicant_count} applicants`} /> : null}
              {job.application ? <Chip label={job.application.status} /> : null}
            </Row>

            <Row style={{ marginTop: spacing.md }} gap={spacing.lg}>
              <ScoreTile value={score(job.scores.priority)} label="Priority" />
              <ScoreTile value={score(job.scores.match)} label="Match" />
              <ScoreTile value={score(job.scores.ats)} label="ATS" />
              <ScoreTile value={score(job.scores.eligibility)} label="Elig" />
              <ScoreTile value={score(job.scores.urgency)} label="Urgency" />
              <ScoreTile value={score(job.scores.competition)} label="Compete" />
            </Row>

            <VerdictBanner job={job} compact />
          </Panel>
        )}
      </Pressable>
    </Link>
  );
}
