/** A labelled text input, used by search, settings and the agent prompt. */
import { TextInput, View } from 'react-native';

import { radius, spacing, usePalette } from '../theme';
import { Small } from './ui';

export function Field({
  label,
  value,
  onChangeText,
  placeholder,
  onSubmitEditing,
  secure,
  autoCapitalize = 'none',
  keyboardType,
  multiline,
  hint,
}: {
  label?: string;
  value: string;
  onChangeText: (value: string) => void;
  placeholder?: string;
  onSubmitEditing?: () => void;
  secure?: boolean;
  autoCapitalize?: 'none' | 'sentences';
  keyboardType?: 'default' | 'url';
  multiline?: boolean;
  hint?: string;
}) {
  const p = usePalette();
  return (
    <View style={{ gap: spacing.xs, flex: 1 }}>
      {label ? <Small>{label}</Small> : null}
      <TextInput
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={p.muted}
        onSubmitEditing={onSubmitEditing}
        secureTextEntry={secure}
        autoCapitalize={autoCapitalize}
        autoCorrect={false}
        keyboardType={keyboardType}
        multiline={multiline}
        returnKeyType="go"
        style={{
          borderWidth: 1,
          borderColor: p.line,
          backgroundColor: p.bg,
          color: p.ink,
          borderRadius: radius.sm + 1,
          paddingHorizontal: spacing.md - 1,
          paddingVertical: spacing.sm + 2,
          fontSize: 14,
          minHeight: multiline ? 88 : 40,
          textAlignVertical: multiline ? 'top' : 'center',
        }}
      />
      {hint ? <Small>{hint}</Small> : null}
    </View>
  );
}
