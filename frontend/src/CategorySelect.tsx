import { Selector } from '@astryxdesign/core/Selector';
import type { SelectorOptionType, SelectorStatus } from '@astryxdesign/core/Selector';
import type { ReactNode } from 'react';
import type { CategoryGroup } from './api';

interface CategorySelectProps {
  groups: CategoryGroup[];
  value: string;
  onChange: (value: string) => void;
  emptyLabel: string;
  label: string;
  name?: string;
  disabled?: boolean;
  required?: boolean;
  status?: SelectorStatus;
  retainedValue?: string | null;
  includeUnclassified?: boolean;
}

export function CategorySelect({ groups, value, onChange, emptyLabel, label, name, disabled = false, required = false, status, retainedValue, includeUnclassified = false }: CategorySelectProps): ReactNode {
  const listedValues = new Set(groups.flatMap(group => group.options.map(option => option.value)));
  const retained = [...new Set([retainedValue, value])].filter((entry): entry is string => Boolean(entry) && entry !== '__none__' && !listedValues.has(entry ?? ''));
  const options: SelectorOptionType[] = [
    { value: '', label: emptyLabel },
    ...(includeUnclassified ? [{ value: '__none__', label: '未分類' }] : []),
    ...retained.map(entry => ({ value: entry, label: entry })),
    ...groups.map(group => ({ type: 'section' as const, title: group.label, options: group.options.map(option => ({ value: option.value, label: `${'　'.repeat(option.depth)}${option.label}` })) })),
  ];
  return <Selector label={label} options={options} value={value} onChange={onChange} htmlName={name} isDisabled={disabled} isRequired={required} status={status} hasSearch searchPlaceholder="カテゴリを検索" width="100%" />;
}
