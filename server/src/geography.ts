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
const categoryMaster = z.object({ values: z.array(z.string()), aliases: z.record(z.string(), z.string()) }).parse(JSON.parse(readFileSync(new URL('../data/categories.json', import.meta.url), 'utf8')));
export const categories = new Set(categoryMaster.values);
export function canonicalCategory(value: string | null): string | null {
  if (value === null) return null;
  const cleaned = value.trim();
  return categories.has(cleaned) ? cleaned : categoryMaster.aliases[cleaned] ?? null;
}
