/**
 * A compact data table.
 *
 * Dense tabular analytics is the one thing react-native-web makes harder than
 * plain React, so this renders as flex rows with per-column weights rather
 * than a real <table>. It stays readable at phone width and scrolls
 * horizontally only when a caller asks for more columns than fit.
 */
import { ScrollView, View } from 'react-native';

import { spacing, type, usePalette } from '../theme';
import { Small } from './ui';

export type Column<T> = {
  key: string;
  header: string;
  width?: number;          // flex weight
  align?: 'left' | 'right';
  render: (row: T) => string;
};

export function Table<T>({
  columns,
  rows,
  minWidth,
}: {
  columns: Column<T>[];
  rows: T[];
  minWidth?: number;
}) {
  const p = usePalette();

  const content = (
    <View style={{ minWidth }}>
      <View
        style={{
          flexDirection: 'row',
          paddingBottom: spacing.sm,
          borderBottomWidth: 1,
          borderBottomColor: p.line,
        }}
      >
        {columns.map((column) => (
          <Small
            key={column.key}
            style={{
              ...type.tiny,
              flex: column.width ?? 1,
              textTransform: 'uppercase',
              textAlign: column.align ?? 'left',
            }}
          >
            {column.header}
          </Small>
        ))}
      </View>
      {rows.map((row, index) => (
        <View
          key={index}
          style={{
            flexDirection: 'row',
            paddingVertical: spacing.sm,
            borderBottomWidth: index === rows.length - 1 ? 0 : 1,
            borderBottomColor: p.line,
          }}
        >
          {columns.map((column) => (
            <Small
              key={column.key}
              style={{
                flex: column.width ?? 1,
                color: p.ink,
                textAlign: column.align ?? 'left',
              }}
            >
              {column.render(row)}
            </Small>
          ))}
        </View>
      ))}
    </View>
  );

  if (!minWidth) return content;
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false}>
      {content}
    </ScrollView>
  );
}
