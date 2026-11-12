import type { InputStatus } from '@astryxdesign/core/Field';
import { useMemo, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { Typeahead } from '@astryxdesign/core/Typeahead';
import type { SearchSource, SearchableItem } from '@astryxdesign/core/Typeahead';
import type { z } from 'zod';
import type { areaSchema } from './api';

// Enter in a combobox picks an option; stop it from also submitting the form.
export function blockComboboxSubmit(event: KeyboardEvent<HTMLFormElement>): void {
  if (event.key === 'Enter' && event.target instanceof HTMLInputElement && event.target.getAttribute('role') === 'combobox') event.preventDefault();
}

type Area = z.infer<typeof areaSchema>;
interface AreaItem extends SearchableItem { hint: string }

// Typeahead only keeps picked items, so the typed text is kept on blur
// to allow free area names like the old datalist did.
export function AreaInput({ label, value, onChange, areas, required = false, disabled = false, status }: { label: string; value: string; onChange: (value: string) => void; areas: Area[]; required?: boolean; disabled?: boolean; status?: InputStatus }): ReactNode {
  const query = useRef<string | null>(null);
  const listOpen = useRef(false);
  const items = useMemo<AreaItem[]>(() => areas.map(area => ({ id: area.value, label: area.value, hint: area.label === area.value ? '' : area.label })), [areas]);
  const source = useMemo<SearchSource<AreaItem>>(() => ({
    search: (text: string): AreaItem[] => {
      const needle = text.trim();
      // Enter picks the first row, so an exact name must come before longer names that contain it.
      const exact = items.find(item => item.label === needle);
      const matches = items.filter(item => item !== exact && (item.label.includes(needle) || item.hint.includes(needle))).slice(0, 50);
      if (exact) return [exact, ...matches];
      return needle ? [{ id: `typed:${needle}`, label: needle, hint: '入力した値を使用' }, ...matches] : matches;
    },
    bootstrap: (): AreaItem[] => items.slice(0, 50),
  }), [items]);
  const selected: AreaItem | null = value ? items.find(item => item.label === value) ?? { id: `typed:${value}`, label: value, hint: '' } : null;
  return <div className="area-input" onKeyDownCapture={event => {
    // Escape with the list closed puts the picked area back; the typed text must not be kept for blur.
    if (event.key === 'Escape' && value && !listOpen.current && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) query.current = null;
  }} onBlur={event => {
    if (event.currentTarget.contains(event.relatedTarget)) return;
    const typed = query.current;
    query.current = null;
    // The typeahead resets its query to '' after a pick; an empty query is not a typed value and must not clear the area.
    if (typed !== null && typed.trim() && typed.trim() !== value) onChange(typed.trim());
  }}><Typeahead<AreaItem> label={label} searchSource={source} value={selected} isRequired={required} isDisabled={disabled} status={status} hasClear hasEntriesOnFocus width="100%"
    // The source is local, so search at once; a delay lets Enter pick a row from the previous list.
    debounceMs={0} onOpenChange={isOpen => { listOpen.current = isOpen; }}
    renderItem={item => <span className="area-option">{item.label}{item.hint && <small>{item.hint}</small>}</span>}
    onChangeQuery={text => { query.current = text; }}
    onChange={item => { query.current = null; onChange(item?.label ?? ''); }} /></div>;
}
