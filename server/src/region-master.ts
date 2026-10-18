import { readFileSync } from 'node:fs';
import { z } from 'zod';
import { canonicalArea } from './geography.js';
import { compareText, escapeRegex } from './text.js';

const municipalitySchema = z.object({ code: z.string(), prefecture_code: z.string(), prefecture: z.string(), county: z.string(), city: z.string(), municipality: z.string(), name: z.string(), area: z.string(), region: z.string(), aliases: z.array(z.string()) });
export type Municipality = z.infer<typeof municipalitySchema>;
const masterSchema = z.object({ municipalities: z.array(municipalitySchema), area_municipalities: z.record(z.string(), z.string()), leaf_areas: z.record(z.string(), z.record(z.string(), z.string())), prefectures: z.record(z.string(), z.string()), admin_parents: z.record(z.string(), z.array(z.string())), broad_localities: z.record(z.string(), z.array(z.string())), precise_aliases: z.record(z.string(), z.array(z.string())) });
export const regionMaster = masterSchema.parse(JSON.parse(readFileSync(new URL('../data/region-detail.json', import.meta.url), 'utf8')));
export const municipalities = new Map(regionMaster.municipalities.map(row => [row.code, row]));
const municipalityAreas = new Set(regionMaster.municipalities.map(row => row.area));
const normalize = (value: string): string => value.normalize('NFKC').replace(/\s+/gu, '');
const prefectures = [...new Set(regionMaster.municipalities.map(row => row.prefecture))];
const prefecturePattern = (): RegExp => new RegExp(prefectures.map(escapeRegex).join('|'), 'g');
const localityOptions = new Map(prefectures.map(prefecture => [prefecture, regionMaster.municipalities.filter(row => row.prefecture === prefecture).flatMap(row => row.aliases.map(alias => ({ alias: normalize(alias), municipality: row }))).sort((a, b) => b.alias.length - a.alias.length || compareText(a.municipality.code, b.municipality.code))]));
export function areaMunicipality(area: string | null): Municipality | null { const canonical = canonicalArea(area); const code = canonical ? regionMaster.area_municipalities[canonical] : null; return code ? municipalities.get(code) ?? null : null; }
export function areaIsMunicipality(area: string): boolean { return municipalityAreas.has(canonicalArea(area) ?? ''); }
export function municipalitiesInText(value: string): readonly Municipality[] {
  const normalized = normalize(value); const matches = [...normalized.matchAll(prefecturePattern())]; const chunks: { prefecture: string; value: string }[] = [];
  if (matches.length) for (const [index, match] of matches.entries()) chunks.push({ prefecture: match[0], value: normalized.slice(match.index + match[0].length, matches[index + 1]?.index ?? normalized.length) });
  else for (const prefecture of prefectures) chunks.push({ prefecture, value: normalized });
  const found = new Map<string, Municipality>();
  for (const chunk of chunks) for (const raw of chunk.value.split(/[/／、,;；|｜]/)) {
    const part = raw.replace(/^[ :：・]+/, ''); const matching = (localityOptions.get(chunk.prefecture) ?? []).filter(option => part.startsWith(option.alias)); const longest = matching[0]?.alias.length;
    for (const item of matching) if (item.alias.length === longest) found.set(item.municipality.code, item.municipality);
  }
  return [...found].sort(([a], [b]) => compareText(a, b)).map(([, row]) => row);
}
export function addressMunicipality(value: string): Municipality | null {
  if ([...normalize(value).matchAll(prefecturePattern())].length !== 1) return null;
  const rows = municipalitiesInText(value); return rows.length === 1 ? rows[0] ?? null : null;
}
export function municipalityAddressTail(value: string, municipality: Municipality): string | null {
  const normalized = normalize(value); const start = normalized.indexOf(municipality.prefecture); if (start < 0) return null;
  const chunk = normalized.slice(start + municipality.prefecture.length);
  for (const raw of chunk.split(/[/／、,;；|｜]/)) {
    const part = raw.replace(/^[ :：・]+/, ''); const aliases = municipality.aliases.filter(alias => part.startsWith(alias)).sort((a, b) => b.length - a.length);
    if (aliases[0]) return part.slice(aliases[0].length);
  }
  return null;
}
export function areaFromAddress(value: string): string | null {
  const parent = addressMunicipality(value); if (!parent) return null;
  const tail = municipalityAddressTail(value, parent); if (tail === null) return null;
  const matches = Object.entries(regionMaster.leaf_areas[parent.code] ?? {}).filter(([, token]) => tail.startsWith(token));
  if (matches.length) {
    const longest = Math.max(...matches.map(([, token]) => token.length)); const selected = new Set(matches.filter(([, token]) => token.length === longest).map(([area]) => area));
    if (selected.size === 1) return [...selected][0] ?? parent.area;
  }
  return parent.area;
}
