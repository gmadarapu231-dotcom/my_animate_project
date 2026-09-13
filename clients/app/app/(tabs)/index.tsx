/**
 * Jobs -- the ranked list.
 *
 * Order is the backend's: deadline tier first, then priority score. The client
 * does not re-sort, because the "deadline today floats to the top" rule is a
 * product guarantee and duplicating it here would be a second place for it to
 * drift.
 */
import { useState } from 'react';
import { View } from 'react-native';

import { JobCardView } from '../../src/components/JobCard';
import { Field } from '../../src/components/Field';
import {
  Button,
  ErrorNote,
  FilterChip,
  ListScreen,
  Panel,
  Row,
  Small,
} from '../../src/components/ui';
import { useQuery, useSession } from '../../src/hooks';
import { spacing } from '../../src/theme';

export default function JobsScreen() {
  const { client, health, healthError, recheck } = useSession();
  const [country, setCountry] = useState<string | undefined>();
  const [domain, setDomain] = useState<string | undefined>();
  const [term, setTerm] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchInfo, setSearchInfo] = useState<string | null>(null);

  const facets = useQuery(() => client.facets(), [health?.status]);
  const jobs = useQuery(
    () =>
      searching && term.trim()
        ? client.search(term.trim()).then((result) => {
            setSearchInfo(`${result.filters_description} — ${result.count} result(s)`);
            return { count: result.count, jobs: result.jobs };
          })
        : client.jobs({ country, domain, limit: 60 }),
    [country, domain, searching, term, health?.status],
  );

  if (healthError) {
    return (
      <View style={{ flex: 1, padding: spacing.lg }}>
        <ErrorNote
          message={healthError}
          hint="Open Settings (⚙︎) to point the app at your CareerOS server."
          onRetry={recheck}
        />
      </View>
    );
  }

  return (
    <ListScreen
      header={
        <View>
          <Panel>
            <Row gap={spacing.sm}>
              <Field
                value={term}
                onChangeText={(value) => {
                  setTerm(value);
                  if (!value) {
                    setSearching(false);
                    setSearchInfo(null);
                  }
                }}
                placeholder='e.g. "H1B-friendly cybersecurity jobs in Texas"'
                onSubmitEditing={() => setSearching(true)}
              />
            </Row>
            <Row style={{ marginTop: spacing.sm }}>
              <Button label="Search" variant="primary" small onPress={() => setSearching(true)} />
              <Button
                label="Reset"
                small
                onPress={() => {
                  setTerm('');
                  setSearching(false);
                  setSearchInfo(null);
                  setCountry(undefined);
                  setDomain(undefined);
                }}
              />
            </Row>
            {searchInfo ? (
              <Small style={{ marginTop: spacing.sm }}>{`Interpreted as: ${searchInfo}`}</Small>
            ) : null}
          </Panel>

          {!searching && facets.data ? (
            <View style={{ gap: spacing.sm, marginBottom: spacing.md }}>
              <Row>
                <FilterChip label="All domains" active={!domain} onPress={() => setDomain(undefined)} />
                {facets.data.domains.map((item) => (
                  <FilterChip
                    key={item.id}
                    label={`${item.label} (${item.job_count})${item.origin === 'inferred' ? ' ✨' : ''}`}
                    active={domain === item.id}
                    onPress={() => setDomain(domain === item.id ? undefined : item.id)}
                  />
                ))}
              </Row>
              <Row>
                <FilterChip label="All countries" active={!country} onPress={() => setCountry(undefined)} />
                {facets.data.countries.map((item) => (
                  <FilterChip
                    key={item.code}
                    label={item.name}
                    active={country === item.code}
                    onPress={() => setCountry(country === item.code ? undefined : item.code)}
                  />
                ))}
              </Row>
            </View>
          ) : null}
        </View>
      }
      loading={jobs.loading && !jobs.data}
      error={jobs.error?.message}
      onRetry={jobs.refresh}
      items={jobs.data?.jobs ?? []}
      emptyMessage="No jobs match. Run `careeros run-daily data/sample_jobs.json`, or clear the filters."
      renderItem={(job) => <JobCardView key={job.id} job={job} />}
    />
  );
}
