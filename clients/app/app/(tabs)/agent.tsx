/**
 * Agent -- ask the tool-use loop.
 *
 * The tool calls are shown alongside the answer, marked read / changed /
 * error. An agent whose reasoning you cannot inspect is one you cannot
 * correct, so the trace is part of the answer rather than a debug view.
 */
import { useState } from 'react';
import { View } from 'react-native';

import { Field } from '../../src/components/Field';
import {
  Body,
  Button,
  Caveat,
  Chip,
  ErrorNote,
  ListScreen,
  Loading,
  Mono,
  Panel,
  Row,
  Small,
} from '../../src/components/ui';
import { AgentAnswer, AgentTools } from '../../src/api';
import { useAction, useQuery, useSession } from '../../src/hooks';
import { spacing, type, usePalette } from '../../src/theme';

const EXAMPLES = [
  'What should I apply for today, and why that order?',
  'Why am I not getting interviews for cloud roles?',
  'Can I honestly apply for the SAP Security role?',
];

export default function AgentScreen() {
  const { client } = useSession();
  const p = usePalette();
  const [prompt, setPrompt] = useState('');
  const [showTools, setShowTools] = useState(false);
  const ask = useAction((value: string) => client.askAgent(value));
  const tools = useQuery<AgentTools | null>(
    () => (showTools ? client.agentTools() : Promise.resolve(null)),
    [showTools],
  );
  const runs = useQuery(() => client.agentRuns(8), [ask.result]);

  const answer: AgentAnswer | null = ask.result;

  return (
    <ListScreen
      header={
        <View>
          <Panel title="Ask the agent">
            <Small>
              Claude drives the CareerOS engines as tools and decides what to look at. It has
              autonomy over strategy, never over truthfulness.
            </Small>
            <View style={{ marginTop: spacing.md }}>
              <Field
                value={prompt}
                onChangeText={setPrompt}
                placeholder="e.g. Why am I not getting interviews for cloud roles?"
                multiline
                onSubmitEditing={() => prompt.trim() && ask.invoke(prompt.trim())}
              />
            </View>
            <Row style={{ marginTop: spacing.sm }} gap={spacing.xs + 2}>
              <Button
                label="Ask"
                variant="primary"
                small
                pending={ask.pending}
                disabled={!prompt.trim()}
                onPress={() => ask.invoke(prompt.trim())}
              />
              <Button
                label={showTools ? 'Hide tools' : 'Tool surface'}
                small
                onPress={() => setShowTools(!showTools)}
              />
            </Row>
            <Row style={{ marginTop: spacing.md }} gap={spacing.xs + 2}>
              {EXAMPLES.map((example) => (
                <Button key={example} label={example} small onPress={() => setPrompt(example)} />
              ))}
            </Row>
          </Panel>

          {ask.pending ? <Loading label="Thinking — the agent may call several tools." /> : null}

          {ask.error ? (
            <ErrorNote
              message={ask.error.message}
              hint={
                ask.error.isUnavailable
                  ? 'The agent needs model access on the server (ANTHROPIC_API_KEY). The scheduled pipeline works without it.'
                  : undefined
              }
            />
          ) : null}

          {answer ? (
            <>
              {answer.tool_calls.length ? (
                <Panel title="Tool calls">
                  {answer.tool_calls.map((call, index) => (
                    <Mono key={index} style={{ opacity: call.is_error ? 0.6 : 1 }}>
                      {`${call.is_error ? '!' : call.mutating ? '*' : '·'} turn ${call.turn}  ${call.name}(${Object.entries(
                        call.arguments ?? {},
                      )
                        .map(([key, value]) => `${key}=${value}`)
                        .join(', ')})`}
                    </Mono>
                  ))}
                  <Caveat>· read · * changed something · ! error</Caveat>
                </Panel>
              ) : null}

              <Panel accent={answer.ok ? p.accent : p.bad}>
                <Body>{answer.answer}</Body>
                <Small style={{ marginTop: spacing.md }}>
                  {`${answer.turns} turn(s) · ${answer.tool_calls.length} tool call(s) · ${
                    answer.input_tokens + answer.output_tokens
                  } tokens`}
                </Small>
                {answer.mutations.length ? (
                  <Row style={{ marginTop: spacing.sm }} gap={spacing.xs + 1}>
                    <Small>Changed:</Small>
                    {[...new Set(answer.mutations)].map((name) => (
                      <Chip key={name} label={name} tone="warn" />
                    ))}
                  </Row>
                ) : null}
              </Panel>
            </>
          ) : null}

          {showTools && tools.data ? (
            <>
              <Panel title={`${tools.data.count} tools`}>
                {tools.data.tools.map((tool) => (
                  <View key={tool.name} style={{ marginBottom: spacing.sm }}>
                    <Row gap={spacing.sm}>
                      <Mono style={{ color: p.ink }}>{tool.name}</Mono>
                      <Chip label={tool.mutating ? 'changes data' : 'read'} tone={tool.mutating ? 'warn' : 'neutral'} />
                    </Row>
                    <Small>{tool.description}</Small>
                  </View>
                ))}
              </Panel>
              <Panel title="Deliberately withheld">
                <Small>{tools.data.note}</Small>
                <View style={{ marginTop: spacing.md }}>
                  {tools.data.withheld_capabilities.map((item) => (
                    <View key={item.name} style={{ marginBottom: spacing.sm }}>
                      <Mono style={{ color: p.bad }}>{item.name}</Mono>
                      <Small>{item.reason}</Small>
                    </View>
                  ))}
                </View>
              </Panel>
            </>
          ) : null}

          {runs.data?.count ? (
            <Panel title="Recent runs">
              {runs.data.runs.map((run) => (
                <View key={run.run_id} style={{ marginBottom: spacing.md }}>
                  <Row gap={spacing.sm}>
                    <Body style={{ fontWeight: '600' }}>{`#${run.run_id}`}</Body>
                    <Chip label={run.ok ? 'ok' : 'failed'} tone={run.ok ? 'ok' : 'bad'} />
                    <Small>{`${run.turns} turn(s) · ${run.input_tokens + run.output_tokens} tokens`}</Small>
                  </Row>
                  <Small style={{ marginTop: 2 }}>{`“${run.prompt}”`}</Small>
                  <Mono>{run.tool_calls.map((call) => call.name).join(' → ') || 'no tool calls'}</Mono>
                </View>
              ))}
            </Panel>
          ) : null}
        </View>
      }
      items={[]}
      renderItem={() => null}
      emptyMessage=""
      loading={false}
    />
  );
}
