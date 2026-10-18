import type { ReactNode } from 'react';
import type { CategoryGroup } from './api';

interface CategorySelectProps {
  groups: CategoryGroup[];
  value: string;
  onChange: (value: string) => void;
  emptyLabel: string;
  id?: string;
  name?: string;
  disabled?: boolean;
  required?: boolean;
  retainedValue?: string | null;
  includeUnclassified?: boolean;
}

export function CategorySelect({ groups, value, onChange, emptyLabel, id, name, disabled = false, required = false, retainedValue, includeUnclassified = false }: CategorySelectProps): ReactNode {
  const listedValues = new Set(groups.flatMap(group => group.options.map(option => option.value)));
  const retained = [...new Set([retainedValue, value])].filter((entry): entry is string => Boolean(entry) && entry !== '__none__' && !listedValues.has(entry ?? ''));
  return <select id={id} name={name} value={value} disabled={disabled} required={required} onChange={event => onChange(event.target.value)}>
    <option value="">{emptyLabel}</option>
    {includeUnclassified && <option value="__none__">未分類</option>}
    {retained.map(entry => <option key={entry} value={entry}>{entry}</option>)}
    {groups.map(group => <optgroup key={group.label} label={group.label}>
      {group.options.map(option => <option key={option.value} value={option.value}>{`${'　'.repeat(option.depth)}${option.label}`}</option>)}
    </optgroup>)}
  </select>;
}
