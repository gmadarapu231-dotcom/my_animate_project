/**
 * Job detail -- everything behind a card, plus the actions.
 *
 * Addressed as `/job?id=N` rather than `/job/N` on purpose: a nested path
 * makes the browser resolve the bundle's relative asset URLs against `/job/`,
 * which 404s on any static host that serves the app from a directory.
 *
 * `tailor_resume` is the one action with a gate the UI must respect: a resume
 * that failed the factuality check comes back `is_final: false`, and this
 * screen reports that plainly rather than presenting the document as ready.
 */
import { Stack, useLocalSearchParams } from 'expo-router';
import { Linking, View } from 'react-native';

import { VerdictBanner } from '../src/components/JobCard';
import {
  Body,
  Button,
  Caveat,
  Chip,
  Divider,
  ErrorNote,
  Loading,
  Mono,
  Panel,
  Row,
  Screen,
  ScoreTile,
  Small,
} from '../src/components/ui';
import { clamp, deadlineFlag, score } from '../src/format';
import { useAction, useQuery, useSession } from '../src/hooks';
import { bucketColor, spacing, type, usePalette } from '../src/theme';

export default function JobDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const jobId = Number(id);
  const { client } = useSession();
  const p = usePalette();

  const job = useQuery(() => client.job(jobId), [jobId]);
  const analyze = useAction(() => client.analyzeJob(jobId));
  const tailor = useAction(() => client.tailorResume(jobId));
  const letter = useAction(() => client.coverLetter(jobId));
  const prepare = useAction(() => client.prepare(jobId));

  if (job.loading && !job.data) {
    return (
      <Screen>
        <Loading />
      </Screen>
    );
  }
  if (job.error || !job.data) {
    return (
      <Screen>
        <ErrorNote message={job.error?.message ?? 'Job not found'} onRetry={job.refresh} />
      </Screen>
    );
  }

  const data = job.data;
  const classification = data.classification;
  const priority = data.priority;

  return (
    <Screen>
      <Stack.Screen options={{ title: clamp(data.title, 28) }} />

      <Panel accent={bucketColor(priority?.bucket, p)}>
        <Body style={{ ...type.h2 }}>{data.title}</Body>
        <Small>{`${data.company} · ${data.location || data.country_name} · ${data.source}`}</Small>
        {priority ? (
          <Small style={{ ...type.tiny, color: bucketColor(priority.bucket, p), marginTop: spacing.sm }}>
            {deadlineFlag(priority.bucket)}
            {priority.days_to_deadline !== null ? ` · ${priority.days_to_deadline} day(s) left` : ''}
          </Small>
        ) : null}

        <Row style={{ marginTop: spacing.md }} gap={spacing.lg}>
          <ScoreTile value={score(data.scores.priority)} label="Priority" />
          <ScoreTile value={score(data.scores.match)} label="Match" />
          <ScoreTile value={score(data.scores.ats)} label="ATS" />
          <ScoreTile value={score(data.scores.eligibility)} label="Elig" />
          <ScoreTile value={score(data.scores.urgency)} label="Urgency" />
          <ScoreTile value={score(data.scores.competition)} label="Compete" />
        </Row>

        <Row style={{ marginTop: spacing.md }} gap={spacing.xs + 1}>
          <Chip label={data.salary.display} />
          {data.employment_type ? <Chip label={data.employment_type} /> : null}
          {data.work_arrangement ? <Chip label={data.work_arrangement} /> : null}
          {data.applicant_count !== null ? <Chip label={`${data.applicant_count} applicants`} /> : null}
          {data.posted_on ? <Chip label={`posted ${data.posted_on}`} /> : null}
          {data.deadline_on ? <Chip label={`closes ${data.deadline_on}`} /> : null}
          {priority?.best_track ? <Chip label={`track: ${priority.best_track}`} tone="accent" /> : null}
        </Row>

        <VerdictBanner job={data} />
      </Panel>

      <Panel title="Actions">
        <Row gap={spacing.xs + 2}>
          <Button label="Analyze" small pending={analyze.pending} onPress={() => analyze.invoke().then(job.refresh)} />
          <Button label="Tailor resume" small variant="primary" pending={tailor.pending} onPress={() => tailor.invoke()} />
          <Button label="Cover letter" small pending={letter.pending} onPress={() => letter.invoke()} />
          <Button label="Prepare packet" small pending={prepare.pending} onPress={() => prepare.invoke()} />
          {data.url ? (
            <Button label="Open posting" small onPress={() => Linking.openURL(data.url as string)} />
          ) : null}
        </Row>

        {tailor.error ? <Small style={{ marginTop: spacing.sm }}>{tailor.error.message}</Small> : null}

        {tailor.result ? (
          <View style={{ marginTop: spacing.md }}>
            <Row gap={spacing.sm}>
              <Chip
                label={tailor.result.is_final ? 'Resume ready' : 'BLOCKED — not sendable'}
                tone={tailor.result.is_final ? 'ok' : 'bad'}
              />
              <Small>{tailor.result.storage_path ?? ''}</Small>
            </Row>
            {tailor.result.factuality ? (
              <Small style={{ marginTop: spacing.sm }}>{tailor.result.factuality.summary}</Small>
            ) : null}
            {tailor.result.notes.map((note, index) => (
              <Small key={index} style={{ marginTop: 3 }}>{`• ${note}`}</Small>
            ))}
            {/* The gate, surfaced: unsupported claims are named, not glossed. */}
            {tailor.result.factuality && !tailor.result.factuality.passed
              ? tailor.result.factuality.claims
                  .filter((claim) => claim.verdict === 'unsupported')
                  .map((claim, index) => (
                    <View key={index} style={{ marginTop: spacing.sm }}>
                      <Small style={{ color: p.bad }}>{`Unsupported: ${clamp(claim.text, 120)}`}</Small>
                      {claim.issues.map((issue, i) => (
                        <Small key={i}>{`   – ${issue}`}</Small>
                      ))}
                    </View>
                  ))
              : null}
          </View>
        ) : null}

        {letter.result ? (
          <View style={{ marginTop: spacing.md }}>
            <Divider />
            <Body>{letter.result.body}</Body>
            {letter.result.unclaimed_gaps.length ? (
              <Caveat>{`Gaps acknowledged rather than hidden: ${letter.result.unclaimed_gaps.join(', ')}`}</Caveat>
            ) : null}
          </View>
        ) : null}

        {prepare.result ? (
          <View style={{ marginTop: spacing.md }}>
            <Divider />
            <Row gap={spacing.sm}>
              <Chip label={`mode: ${prepare.result.submission_mode}`} tone="accent" />
              {prepare.result.blockers.map((blocker) => (
                <Chip key={blocker} label={blocker} tone="bad" />
              ))}
            </Row>
            <Caveat>{prepare.result.notice}</Caveat>
          </View>
        ) : null}
      </Panel>

      {priority?.explanation?.length ? (
        <Panel title="Why this rank">
          {priority.explanation.map((line, index) => (
            <Small key={index} style={{ marginTop: 2 }}>{`• ${line}`}</Small>
          ))}
        </Panel>
      ) : null}

      {classification ? (
        <Panel title="Classification">
          <Row gap={spacing.xs + 1}>
            <Chip label={classification.domain_label} tone="accent" />
            {classification.function ? <Chip label={classification.function} /> : null}
            {classification.industry ? <Chip label={classification.industry} /> : null}
            <Chip label={classification.seniority} />
          </Row>
          {classification.specialization ? (
            <Small style={{ marginTop: spacing.sm }}>{`Specialisation: ${classification.specialization}`}</Small>
          ) : null}
          <Small style={{ marginTop: 2 }}>
            {`${classification.min_experience_years ?? '—'} yrs required · education: ${
              classification.education_requirement ?? 'unspecified'
            } · confidence ${score(classification.confidence * 100)}% (${classification.method})`}
          </Small>

          <Divider />
          <Small>Required skills</Small>
          <Row style={{ marginTop: spacing.xs }} gap={spacing.xs + 1}>
            {classification.required_skills.length ? (
              classification.required_skills.map((skill, index) => <Chip key={index} label={skill.name} />)
            ) : (
              <Small>none detected</Small>
            )}
          </Row>

          <Small style={{ marginTop: spacing.md }}>Preferred skills</Small>
          <Row style={{ marginTop: spacing.xs }} gap={spacing.xs + 1}>
            {classification.preferred_skills.length ? (
              classification.preferred_skills.map((skill, index) => <Chip key={index} label={skill.name} />)
            ) : (
              <Small>none detected</Small>
            )}
          </Row>

          {classification.required_certifications.length ? (
            <>
              <Small style={{ marginTop: spacing.md }}>Certifications named</Small>
              <Row style={{ marginTop: spacing.xs }} gap={spacing.xs + 1}>
                {classification.required_certifications.map((cert) => (
                  <Chip key={cert} label={cert} tone="warn" />
                ))}
              </Row>
            </>
          ) : null}

          <Small style={{ marginTop: spacing.md }}>ATS keywords derived from this posting</Small>
          <Mono style={{ marginTop: spacing.xs }}>
            {classification.ats_keywords.slice(0, 24).join(' · ')}
          </Mono>
        </Panel>
      ) : null}

      {data.matches?.length ? (
        <Panel title="Match detail">
          {data.matches[0].skill_matches.map((match, index) => (
            <View key={index} style={{ marginBottom: spacing.sm }}>
              <Row gap={spacing.sm}>
                <Chip
                  label={match.kind}
                  tone={
                    match.kind === 'direct'
                      ? 'ok'
                      : match.kind === 'missing'
                        ? 'bad'
                        : match.claimable
                          ? 'ok'
                          : 'warn'
                  }
                />
                <Body style={{ flexShrink: 1 }}>{match.requirement}</Body>
              </Row>
              {match.note ? <Small>{match.note}</Small> : null}
            </View>
          ))}
          {data.matches[0].gaps.length ? (
            <Caveat>{`Gaps (required, unevidenced): ${data.matches[0].gaps.join(', ')}`}</Caveat>
          ) : null}
        </Panel>
      ) : null}

      {data.ats ? (
        <Panel title="ATS report">
          <Row gap={spacing.sm}>
            <ScoreTile value={score(data.ats.overall)} label="Overall" />
            <ScoreTile value={score(data.ats.keyword_match)} label="Keywords" />
            <ScoreTile value={score(data.ats.required_skills_match)} label="Required" />
            <ScoreTile value={score(data.ats.experience_match)} label="Experience" />
            <ScoreTile value={score(data.ats.title_match)} label="Title" />
          </Row>
          {data.ats.missing_keywords.length ? (
            <>
              <Small style={{ marginTop: spacing.md }}>Highest-value terms missing</Small>
              <Mono style={{ marginTop: spacing.xs }}>
                {data.ats.missing_keywords.slice(0, 14).join(' · ')}
              </Mono>
            </>
          ) : null}
          {data.ats.suggestions.map((suggestion, index) => (
            <Small key={index} style={{ marginTop: spacing.sm }}>{`• ${suggestion}`}</Small>
          ))}
        </Panel>
      ) : null}

      {data.description ? (
        <Panel title="Posting">
          <Small style={{ lineHeight: 19 }}>{data.description}</Small>
        </Panel>
      ) : null}
    </Screen>
  );
}
