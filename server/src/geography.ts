import { readFileSync } from 'node:fs';
import { z } from 'zod';

const areaSchema = z.object({ value: z.string(), label: z.string(), display: z.string(), ward: z.string().nullable(), group: z.string().nullable(), municipality: z.string().nullable(), parent: z.string().nullable(), sort: z.tuple([z.number(), z.number(), z.string()]) });
const masterSchema = z.object({ entries: z.array(areaSchema), aliases: z.record(z.string(), z.string()), candidates: z.record(z.string(), z.array(z.string())), noncanonical: z.array(z.string()), group_prefix: z.string(), nonmunicipality_groups: z.array(z.string()) });
const master = masterSchema.parse(JSON.parse(readFileSync(new URL('../data/geography.json', import.meta.url), 'utf8')));
export const areas = new Map(master.entries.map(area => [area.value, area]));
export function canonicalArea(value: string | null): string | null {
  if (value === null) return null;
  const normalized = value.normalize('NFKC').trim();
  if (!normalized) return null;
  const canonical = master.aliases[normalized] ?? normalized;
  if (areas.has(canonical)) return canonical;
  if (master.noncanonical.includes(normalized)) return null;
  const candidates = master.candidates[normalized.replace(/\s+/gu, '')];
  return candidates?.length === 1 ? candidates[0] ?? null : null;
}
export function areaLabel(value: string | null, card = false): string {
  if (!value) return '';
  const area = areas.get(canonicalArea(value) ?? value);
  if (!area) return value;
  return card && area.ward ? `${area.ward}・${area.display}` : card ? area.display : area.label;
}
export function areaMatches(stored: string, selected: string): boolean {
  const area = areas.get(canonicalArea(stored) ?? stored);
  const selectedArea = areas.get(selected);
  if (selectedArea?.municipality === selected) return area?.municipality === selected || area?.parent === selected;
  if (selected.startsWith(master.group_prefix)) {
    const group = selected.slice(master.group_prefix.length);
    return master.nonmunicipality_groups.includes(group) && (area?.group ?? 'その他') === group;
  }
  return stored === selected;
}
export function areaSort(value: string | null): readonly [number, number, string] {
  if (!value?.trim()) return [3, 0, ''];
  return areas.get(canonicalArea(value) ?? value)?.sort ?? [2, 0, value];
}
export { categories, canonicalCategory } from './categories.js';

export interface AreaFilterOption { value: string; label: string; count: number; depth: number; }
export function buildAreaFilterOptions(rows: readonly { area: string | null; count: number }[]): AreaFilterOption[] {
  const counts = new Map<string, number>();
  const leaves = new Map<string, Map<string, number>>();
  const parents = new Map<string, string>();
  const children = new Map<string, Set<string>>();
  const groupOrder = new Map(master.entries.flatMap(entry => entry.group ? [[entry.group, entry.sort[1]] as const] : []));
  const add = (value: string, count: number): void => { counts.set(value, (counts.get(value) ?? 0) + count); };
  for (const { area, count } of rows) {
    if (!area) continue;
    const entry = areas.get(canonicalArea(area) ?? area);
    const group = entry?.municipality ?? master.group_prefix + (entry?.group ?? 'その他');
    add(group, count);
    if (area !== group) {
      const bucket = leaves.get(group) ?? new Map<string, number>();
      bucket.set(area, (bucket.get(area) ?? 0) + count); leaves.set(group, bucket);
    }
    if (entry?.parent) {
      parents.set(group, entry.parent);
      const bucket = children.get(entry.parent) ?? new Set<string>();
      bucket.add(group); children.set(entry.parent, bucket); add(entry.parent, count);
    }
  }
  const label = (value: string): string => value.startsWith(master.group_prefix) ? value.slice(master.group_prefix.length) : areaLabel(value);
  const textOrder = (a: string, b: string): number => a < b ? -1 : a > b ? 1 : 0;
  const order = (a: string, b: string): number => (groupOrder.get(label(a)) ?? Number.MAX_SAFE_INTEGER) - (groupOrder.get(label(b)) ?? Number.MAX_SAFE_INTEGER) || textOrder(label(a), label(b));
  const result: AreaFilterOption[] = [];
  const append = (value: string, depth: number): void => {
    result.push({ value, label: label(value), count: counts.get(value) ?? 0, depth });
    for (const [area, count] of [...(leaves.get(value) ?? [])].sort((a, b) => b[1] - a[1] || textOrder(a[0], b[0]))) result.push({ value: area, label: areaLabel(area), count, depth: depth + 1 });
    for (const child of [...(children.get(value) ?? [])].sort(order)) append(child, depth + 1);
  };
  for (const value of [...counts.keys()].filter(value => !parents.has(value)).sort(order)) append(value, 0);
  return result;
}
