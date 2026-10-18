import { readFileSync } from 'node:fs';
import type { CategoryGroup } from '@meshi/contracts';
import { z } from 'zod';

const nameSchema = z.string().min(1).refine(value => value === value.normalize('NFKC').trim(), 'Use a trimmed NFKC category name.');
export const categoryMasterSchema = z.strictObject({
  groups: z.array(z.strictObject({ label: nameSchema, genres: z.array(z.strictObject({ value: nameSchema, children: z.array(nameSchema) })).min(1) })).min(1),
  aliases: z.record(nameSchema, nameSchema),
}).superRefine((master, context) => {
  const labels = new Set<string>();
  const values = new Set<string>();
  for (const [groupIndex, group] of master.groups.entries()) {
    if (labels.has(group.label)) context.addIssue({ code: 'custom', path: ['groups', groupIndex, 'label'], message: `Duplicate category group: ${group.label}` });
    labels.add(group.label);
    for (const [genreIndex, genre] of group.genres.entries()) {
      if (values.has(genre.value)) context.addIssue({ code: 'custom', path: ['groups', groupIndex, 'genres', genreIndex, 'value'], message: `Duplicate category value: ${genre.value}` });
      values.add(genre.value);
      for (const [childIndex, child] of genre.children.entries()) {
        if (values.has(child)) context.addIssue({ code: 'custom', path: ['groups', groupIndex, 'genres', genreIndex, 'children', childIndex], message: `Duplicate category value: ${child}` });
        values.add(child);
      }
    }
  }
  for (const [alias, target] of Object.entries(master.aliases)) {
    if (!values.has(target)) context.addIssue({ code: 'custom', path: ['aliases', alias], message: `Category alias target does not exist: ${alias} -> ${target}` });
    if (values.has(alias) || labels.has(alias)) context.addIssue({ code: 'custom', path: ['aliases', alias], message: `Category alias shadows a category or group: ${alias}` });
  }
});

const master = categoryMasterSchema.parse(JSON.parse(readFileSync(new URL('../data/categories.json', import.meta.url), 'utf8')));
const aliases = new Map<string, string>(Object.entries(master.aliases));
const children = new Map<string, ReadonlySet<string>>(master.groups.flatMap(group => group.genres.map(genre => [genre.value, new Set(genre.children)] as const)));
export const categories: ReadonlySet<string> = new Set(master.groups.flatMap(group => group.genres.flatMap(genre => [genre.value, ...genre.children])));
export const categoryGroups: CategoryGroup[] = master.groups.map(group => ({
  label: group.label,
  options: group.genres.flatMap(genre => [{ value: genre.value, label: genre.value, depth: 0 }, ...genre.children.map(value => ({ value, label: value, depth: 1 }))]),
}));

export function canonicalCategory(value: string | null): string | null {
  if (value === null) return null;
  const normalized = value.normalize('NFKC').trim();
  return categories.has(normalized) ? normalized : aliases.get(normalized) ?? null;
}

export function categoryMatches(stored: string | null, selected: string): boolean {
  if (selected === '__none__') return stored === null || !stored.normalize('NFKC').trim();
  const canonicalSelected = canonicalCategory(selected);
  const canonicalStored = canonicalCategory(stored);
  if (canonicalSelected === null || canonicalStored === null) return stored === selected;
  return canonicalStored === canonicalSelected || children.get(canonicalSelected)?.has(canonicalStored) === true;
}

export function buildCategoryFilterGroups(values: readonly (string | null)[]): CategoryGroup[] {
  const present = new Set<string>();
  const unclassified = new Set<string>();
  for (const value of values) {
    if (value === null || !value.normalize('NFKC').trim()) continue;
    const canonical = canonicalCategory(value);
    if (canonical === null) unclassified.add(value);
    else present.add(canonical);
  }
  const result: CategoryGroup[] = [];
  for (const group of master.groups) {
    const options: CategoryGroup['options'] = [];
    for (const genre of group.genres) {
      const matchingChildren = genre.children.filter(child => present.has(child));
      if (!present.has(genre.value) && !matchingChildren.length) continue;
      options.push({ value: genre.value, label: genre.value, depth: 0 });
      options.push(...matchingChildren.map(value => ({ value, label: value, depth: 1 })));
    }
    if (options.length) result.push({ label: group.label, options });
  }
  if (unclassified.size) result.push({ label: '未整理', options: [...unclassified].sort((a, b) => a < b ? -1 : a > b ? 1 : 0).map(value => ({ value, label: value, depth: 0 })) });
  return result;
}

export const categoryInstructions: string = [
  'カテゴリーは以下のジャンル名または細分類名から1つ選んでください。大分類の見出しだけをカテゴリーにしないでください。',
  '店舗そのものの主な業態・専門性を根拠に分類し、メニューにある料理だけで専門店と判断しないでください。',
  '根拠で確認できる場合だけ細分類を選び、スタイルが不明な場合はジャンル名を使ってください。分類できない場合はnullにしてください。',
  '複数の分類を結合した名前は使わないでください。ピザをイタリアンへ、ジェラートを洋菓子へ置き換えないでください。',
  ...master.groups.map(group => `${group.label}: ${group.genres.map(genre => genre.children.length ? `${genre.value}（細分類: ${genre.children.join('、')}）` : genre.value).join('、')}`),
].join('\n');
