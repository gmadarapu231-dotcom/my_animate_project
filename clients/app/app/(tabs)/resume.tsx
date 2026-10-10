/**
 * Résumé -- upload one, see what was read from it, confirm the facts.
 *
 * Any format the server reads: .pdf, .docx, .txt, or a scan/phone photo,
 * which the server reads with OCR. Everything parsed lands as *unverified*
 * evidence, and a tailored résumé cannot cite a fact until it is confirmed
 * here -- so the confirm step is part of the flow, not a setting.
 */
import * as DocumentPicker from 'expo-document-picker';
import { router } from 'expo-router';
import { useState } from 'react';
import { View } from 'react-native';

import { IntakeReport, PickedFile } from '../../src/api';
import { Field } from '../../src/components/Field';
import {
  Body,
  Button,
  Caveat,
  Chip,
  Divider,
  ErrorNote,
  Mono,
  Panel,
  Row,
  Screen,
  Small,
} from '../../src/components/ui';
import { clamp } from '../../src/format';
import { useAction, useQuery, useSession } from '../../src/hooks';
import { spacing, type, usePalette } from '../../src/theme';

const ACCEPTED = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'text/plain',
  'text/markdown',
  'image/*',
];

const METHOD_LABEL: Record<string, string> = {
  text: 'read as text',
  docx: 'read from Word',
  'pdf-text': 'read from the PDF text layer',
  'ocr:tesseract': 'scanned: read with OCR (tesseract)',
  'ocr:claude': 'scanned: read with OCR (Claude)',
};

export default function ResumeScreen() {
  const { client } = useSession();
  const p = usePalette();
  const [pasted, setPasted] = useState('');
  const [showPaste, setShowPaste] = useState(false);
  const [report, setReport] = useState<IntakeReport | null>(null);

  const evidence = useQuery(() => client.evidence(), []);
  const upload = useAction((file: PickedFile) => client.uploadResume(file));
  const paste = useAction((text: string) => client.intakeText(text));
  const verify = useAction((ids: number[]) => client.verifyEvidence(ids));
  const discover = useAction((terms: string[]) => client.discover({ terms }));

  const pick = async () => {
    const picked = await DocumentPicker.getDocumentAsync({
      type: ACCEPTED,
      copyToCacheDirectory: true,
      multiple: false,
    });
    if (picked.canceled || !picked.assets?.length) return;
    const asset = picked.assets[0];
    const result = await upload.invoke({
      name: asset.name,
      uri: asset.uri,
      mimeType: asset.mimeType,
      file: (asset as { file?: Blob }).file ?? null,
    });
    if (result) {
      setReport(result);
      evidence.refresh();
    }
  };

  const submitPaste = async () => {
    const result = await paste.invoke(pasted);
    if (result) {
      setReport(result);
      setShowPaste(false);
      evidence.refresh();
    }
  };

  const unverified = (evidence.data?.evidence ?? []).filter((item) => !item.verified);
  const busy = upload.pending || paste.pending;
  const error = upload.error ?? paste.error;

  return (
    <Screen>
      <Panel title="Upload your résumé">
        <Small>
          PDF, Word (.docx), text, or a scan or photo of a printed résumé. Scans are read with OCR.
        </Small>
        <Row style={{ marginTop: spacing.md }} gap={spacing.sm}>
          <Button
            label={busy ? 'Reading…' : 'Choose file'}
            variant="primary"
            pending={upload.pending}
            onPress={pick}
          />
          <Button label={showPaste ? 'Hide' : 'Paste text instead'} small onPress={() => setShowPaste(!showPaste)} />
        </Row>
        {showPaste ? (
          <View style={{ marginTop: spacing.md }}>
            <Field
              label="Résumé text"
              value={pasted}
              onChangeText={setPasted}
              placeholder="Paste the whole résumé here"
              multiline
              autoCapitalize="sentences"
            />
            <View style={{ marginTop: spacing.sm }}>
              <Button
                label="Read this text"
                small
                pending={paste.pending}
                disabled={pasted.trim().length < 80}
                onPress={submitPaste}
              />
            </View>
          </View>
        ) : null}
        {error ? <ErrorNote message={error.message} /> : null}
      </Panel>

      {report ? <ReportPanel report={report} onDiscover={(terms) => discover.invoke(terms)} discovering={discover.pending} /> : null}

      {discover.result ? (
        <Panel title="Job search">
          <Body>{`Found ${discover.result.found} job(s) using your résumé's titles and skills.`}</Body>
          {discover.result.providers
            .filter((row) => row.skipped_reason)
            .slice(0, 4)
            .map((row) => (
              <Small key={row.provider}>{`${row.label}: ${row.skipped_reason}`}</Small>
            ))}
          <View style={{ marginTop: spacing.sm }}>
            <Button label="See ranked jobs" small variant="primary" onPress={() => router.push('/')} />
          </View>
        </Panel>
      ) : null}
      {discover.error ? <ErrorNote message={discover.error.message} /> : null}

      <Panel title={`Confirm what's true (${unverified.length} to review)`}>
        <Small>
          A tailored résumé only states facts you have confirmed. Confirm the ones that are accurate;
          anything left unconfirmed is never claimed.
        </Small>
        {/* 404 here just means no profile yet: the upload above creates it. */}
        {evidence.error && evidence.error.status !== 404 ? <ErrorNote message={evidence.error.message} onRetry={evidence.refresh} /> : null}
        {unverified.slice(0, 40).map((item) => (
          <View key={item.id} style={{ marginTop: spacing.sm }}>
            <Row gap={spacing.sm}>
              <Chip label={item.kind} />
              {item.employer ? <Small>{item.employer}</Small> : null}
            </Row>
            <Body style={{ marginTop: 2 }}>{item.text}</Body>
            <View style={{ marginTop: spacing.xs, alignSelf: 'flex-start' }}>
              <Button
                label="Confirm"
                small
                onPress={() => verify.invoke([item.id]).then(evidence.refresh)}
              />
            </View>
          </View>
        ))}
        {unverified.length ? (
          <View style={{ marginTop: spacing.md }}>
            <Divider />
            <Button
              label={`Confirm all ${unverified.length} as accurate`}
              variant="primary"
              pending={verify.pending}
              onPress={() => verify.invoke(unverified.map((item) => item.id)).then(evidence.refresh)}
            />
          </View>
        ) : evidence.data?.evidence.length ? (
          <Small style={{ marginTop: spacing.sm, color: p.ok }}>Everything on file is confirmed.</Small>
        ) : (
          <Small style={{ marginTop: spacing.sm }}>Upload a résumé and the facts read from it appear here.</Small>
        )}
      </Panel>
    </Screen>
  );
}

function ReportPanel({
  report,
  onDiscover,
  discovering,
}: {
  report: IntakeReport;
  onDiscover: (terms: string[]) => void;
  discovering: boolean;
}) {
  const parsed = report.parsed;
  const skills = Object.entries(parsed.skills)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 14)
    .map(([skill]) => skill.replace(/_/g, ' '));

  return (
    <Panel title="What was read">
      <Row gap={spacing.sm}>
        <Chip label={METHOD_LABEL[report.file.method] ?? report.file.method} tone="accent" />
        <Small>{`${report.file.name} · ${report.file.characters} characters`}</Small>
      </Row>
      <Body style={{ ...type.h2, marginTop: spacing.md }}>{parsed.full_name ?? '(no name found)'}</Body>
      {parsed.current_title ? <Small>{parsed.current_title}</Small> : null}
      <Small>{[parsed.email, parsed.phone].filter(Boolean).join(' · ')}</Small>
      <Small>{`${parsed.total_experience_years} years across ${parsed.employers.length} role(s)`}</Small>

      {parsed.employers.map((employer, index) => (
        <View key={index} style={{ marginTop: spacing.sm }}>
          <Body>{employer.name}</Body>
          <Small>
            {`${employer.title ?? '(no title)'} · ${employer.start_date ?? '?'} → ${
              employer.current ? 'present' : employer.end_date ?? '?'
            }`}
          </Small>
        </View>
      ))}

      {skills.length ? (
        <>
          <Small style={{ marginTop: spacing.md }}>Skills recognised</Small>
          <Row style={{ marginTop: spacing.xs }} gap={spacing.xs + 1}>
            {skills.map((skill) => (
              <Chip key={skill} label={skill} />
            ))}
          </Row>
        </>
      ) : null}
      {parsed.certifications.length ? (
        <Small style={{ marginTop: spacing.sm }}>{`Certifications: ${parsed.certifications.join(', ')}`}</Small>
      ) : null}

      {parsed.warnings.map((warning, index) => (
        <Caveat key={index}>{warning}</Caveat>
      ))}

      {report.intake ? (
        <Small style={{ marginTop: spacing.md }}>
          {`Saved ${report.intake.evidence_created} new fact(s)` +
            (report.intake.evidence_skipped ? `, ${report.intake.evidence_skipped} already known` : '') +
            '. Confirm them below.'}
        </Small>
      ) : null}

      {report.search_terms.length ? (
        <>
          <Divider />
          <Small>Searches derived from this résumé</Small>
          <Mono style={{ marginTop: spacing.xs }}>{clamp(report.search_terms.join(' · '), 240)}</Mono>
          <View style={{ marginTop: spacing.sm }}>
            <Button
              label="Find jobs for me"
              variant="primary"
              pending={discovering}
              onPress={() => onDiscover(report.search_terms)}
            />
          </View>
        </>
      ) : null}
    </Panel>
  );
}
