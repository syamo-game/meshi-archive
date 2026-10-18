import type { ExtractedMention } from './ai-models.js';
import { areas, canonicalArea } from './geography.js';
import { areaFromAddress, areaIsMunicipality, areaMunicipality, municipalitiesInText, regionMaster } from './region-master.js';
import { branchesConflict, normalizeAddress, normalizeName } from './resolution.js';
import type { CandidateIdentity } from './resolution.js';
import { compareText, escapeRegex } from './text.js';

const prefectures = [...new Set(Object.values(regionMaster.prefectures))].sort(compareText);
const leaf = (area: string): string => area.split(' / ').at(-1) ?? area;
const leafAreas = [...areas.keys()].sort(compareText).filter(area => !areaIsMunicipality(area)).map(area => ({ area, token: normalizeName(leaf(area)) }));
function groupParts(area: string): readonly [string, string | null] {
  const group = areas.get(area)?.group; if (!group) throw new Error(`Area group is missing: area=${area}`);
  const [region, ...parent] = group.split(' / '); if (!region) throw new Error(`Area group is empty: area=${area}`); return [region, parent.length ? parent.join(' / ') : null];
}
function expectedPrefecture(region: string): string { const value = regionMaster.prefectures[region]; if (!value) throw new Error(`Prefecture is missing: region=${region}`); return value; }
function prefectureRegion(value: string): string { return value === '北海道' ? value : value.slice(0, -1); }
function prefecturesInText(value: string): readonly string[] { return [...normalizeName(value).matchAll(new RegExp(prefectures.map(escapeRegex).join('|'), 'g'))].map(match => match[0]); }
const singleton = (values: ReadonlySet<string>, expected: string): boolean => values.size === 1 && values.has(expected);
export function canonicalAreasInText(value: string): ReadonlySet<string> {
  const normalized = normalizeName(value); const matches: { start: number; end: number; area: string }[] = [];
  for (const { area, token } of leafAreas) {
    if (!token) continue; let offset = 0;
    for (let start = normalized.indexOf(token, offset); start >= 0; start = normalized.indexOf(token, offset)) {
      const end = start + token.length; if (end === normalized.length || !'都道府県市区町村'.includes(normalized[end] ?? '')) matches.push({ start, end, area }); offset = end;
    }
  }
  matches.sort((a, b) => (b.end - b.start) - (a.end - a.start) || a.start - b.start || compareText(a.area, b.area));
  const selected: typeof matches = [];
  for (const match of matches) if (!selected.some(chosen => match.start < chosen.end && match.end > chosen.start && (match.start !== chosen.start || match.end !== chosen.end))) selected.push(match);
  return new Set(selected.map(match => match.area));
}
function areasInGroup(value: string, expected: string): ReadonlySet<string> { return new Set([...canonicalAreasInText(value)].filter(area => areas.get(area)?.group === areas.get(expected)?.group)); }
function areaRegions(value: string): ReadonlySet<string> {
  const present = prefecturesInText(value); const regions = new Set<string>(); let remainder = normalizeName(value);
  for (const municipality of [...municipalitiesInText(value)].sort((a, b) => b.name.length - a.name.length)) {
    const prefix = normalizeName(municipality.prefecture + municipality.name);
    if (remainder.startsWith(prefix)) { remainder = normalizeName(municipality.prefecture) + remainder.slice(prefix.length); break; }
  }
  for (const prefecture of present) { regions.add(normalizeName(prefectureRegion(prefecture))); remainder = remainder.replace(normalizeName(prefecture), ''); }
  for (const prefecture of prefectures) { const region = normalizeName(prefectureRegion(prefecture)); if (region && remainder.includes(region)) regions.add(region); }
  return regions;
}
export function hasMultipleGeographicValues(value: string): boolean {
  if (canonicalArea(value)) return false;
  const parts = value.split(/(?:[/／|｜,，、;；・\r\n]+|および|及び|ならびに|並びに|または|又は)/).map(normalizeName).filter(Boolean);
  const geographic = parts.filter(part => prefectures.some(prefecture => part.includes(normalizeName(prefecture))) || /[市区町村]/.test(part) || Object.hasOwn(regionMaster.prefectures, part) || canonicalAreasInText(part).size > 0);
  if (geographic.length > 1) return true;
  const admin = value.split(/[()（）\[\]［］{}\s]+/u).map(normalizeName).filter(Boolean);
  for (let index = 1; index < admin.length; index++) {
    const prefix = admin.slice(0, index).join(''); const match = /^([^0-9０-９\-ー丁目番地号]{1,30})([市区町村])/.exec(admin[index] ?? '');
    if (!match?.[1] || !match[2]) continue; let name = match[1]; const level = match[2];
    for (const separator of '都道府県郡') if (name.includes(separator)) name = name.slice(name.lastIndexOf(separator) + 1);
    if ('町村'.includes(level) && name.length < 2) continue; if (prefix.includes(name + level)) continue;
    if ((level === '市' && /[市区町村]/.test(prefix)) || (level === '区' && /[区町村]/.test(prefix)) || ('町村'.includes(level) && /[町村]/.test(prefix))) return true;
  }
  return false;
}
function regionMatches(value: string, expected: string): boolean { const regions = areaRegions(value); return !regions.size || singleton(regions, normalizeName(expected)); }
function parentsInText(value: string, region: string): ReadonlySet<string> { const normalized = normalizeName(value); return new Set((regionMaster.admin_parents[region] ?? []).filter(parent => normalized.includes(normalizeName(parent)))); }
function nonAdminLeaf(value: string, token: string): boolean {
  if (!token) return false; let offset = 0;
  for (let start = value.indexOf(token, offset); start >= 0; start = value.indexOf(token, offset)) { const end = start + token.length; if (end === value.length || !'都道府県市区町村'.includes(value[end] ?? '')) return true; offset = end; }
  return false;
}
export function candidateAreaContextMatches(area: string, value: string): boolean {
  if (hasMultipleGeographicValues(value)) return false;
  const normalized = normalizeName(value); const municipality = areaMunicipality(area); const canonical = canonicalArea(value); const valueMunicipality = canonical ? areaMunicipality(canonical) : null;
  const broad = regionMaster.broad_localities[area];
  if (!municipality && valueMunicipality && broad) { const [region] = groupParts(area); return valueMunicipality.prefecture === expectedPrefecture(region) && broad.includes(valueMunicipality.municipality); }
  if (municipality && valueMunicipality) {
    if (municipality.prefecture !== valueMunicipality.prefecture || (municipality.code !== valueMunicipality.code && municipality.name !== valueMunicipality.city && valueMunicipality.name !== municipality.city)) return false;
    if (areaIsMunicipality(canonical ?? '') || areaIsMunicipality(area)) return true;
  }
  const embedded = areaIsMunicipality(area) ? new Set<string>() : areasInGroup(value, area); if (embedded.size && !singleton(embedded, area)) return false;
  const [region, parent] = groupParts(area); if (!regionMatches(value, region)) return false; const parents = parentsInText(value, region);
  if (!municipality && !broad && parents.size && (!parent || !singleton(parents, parent))) return false;
  const token = normalizeName(leaf(area));
  return region === '東京' ? Boolean((parent && normalized.includes(normalizeName(parent))) || nonAdminLeaf(normalized, token)) : Boolean(normalized.includes(token) || (parent && normalized.includes(normalizeName(parent))) || normalized.includes(normalizeName(region)));
}
export function geographicAreaConflict(area: string, value: string | null): boolean {
  if (value && hasMultipleGeographicValues(value)) return true;
  const normalized = normalizeAddress(value); if (!normalized) return false;
  const [region, parent] = groupParts(area); const municipality = areaMunicipality(area); const broad = regionMaster.broad_localities[area];
  if (!municipality && broad && municipalitiesInText(value ?? '').some(present => !broad.includes(present.municipality))) return true;
  if (municipality) {
    const canonical = canonicalArea(value); const valueMunicipality = canonical ? areaMunicipality(canonical) : null;
    const present = canonical !== null || prefectures.some(prefecture => normalized.includes(prefecture)) ? [...municipalitiesInText(value ?? '')] : [];
    if (valueMunicipality) present.push(valueMunicipality);
    if (present.some(row => row.prefecture !== municipality.prefecture || (row.code !== municipality.code && row.name !== municipality.city && municipality.name !== row.city))) return true;
  }
  const presentPrefectures = prefecturesInText(normalized);
  if (presentPrefectures.length > 1 || (presentPrefectures.length === 1 && presentPrefectures[0] !== expectedPrefecture(region)) || !regionMatches(normalized, region)) return true;
  const parents = parentsInText(normalized, region); if (!municipality && !broad && parents.size && (!parent || !singleton(parents, parent))) return true;
  const embedded = areaIsMunicipality(area) ? new Set<string>() : areasInGroup(normalized, area); return embedded.size > 0 && !singleton(embedded, area);
}
function adminLocalities(area: string): readonly string[] {
  const broad = regionMaster.broad_localities[area]; if (broad) return broad; const municipality = areaMunicipality(area);
  if (municipality) return [...new Set([municipality.name, municipality.city + municipality.municipality])];
  const [, parent] = groupParts(area); return parent ? [parent] : [];
}
export function candidateAreaHasConflict(area: string, value: string, address: string | null): boolean {
  if (geographicAreaConflict(area, value)) return true; let normalized = normalizeName(value);
  for (const prefecture of prefectures) normalized = normalized.replaceAll(normalizeName(prefecture), '');
  const [region] = groupParts(area); const localities = adminLocalities(area).map(normalizeName).sort((a, b) => b.length - a.length); const normalizedRegion = normalizeName(region);
  if (normalized.startsWith(normalizedRegion) && !localities.some(locality => normalized.startsWith(locality))) normalized = normalized.slice(normalizedRegion.length);
  const remainder = normalized.replace(normalizeName(leaf(area)), ''); if (!/[市区町村]/.test(remainder)) return false;
  const normalizedAddress = normalizeAddress(address); if (!normalizedAddress) return true; const prefecture = normalizeName(expectedPrefecture(region)); const index = normalizedAddress.indexOf(prefecture); if (index < 0) return true;
  const tail = normalizedAddress.slice(index + prefecture.length);
  for (const locality of localities) {
    if (!normalized.includes(locality)) continue; const other = normalized.replace(locality, ''); if (/[市町村]/.test(other)) return true;
    if (other.includes('区')) return !tail.startsWith(locality + other); return Boolean(other && !normalizedAddress.includes(other));
  }
  if (/[市町村]/.test(remainder) || remainder.split('区').length - 1 !== 1) return true;
  return !localities.some(locality => locality.endsWith('市') && tail.startsWith(locality + remainder));
}
export function candidateAddressMatchesGroup(area: string, address: string | null): boolean {
  const normalized = normalizeAddress(address); if (!normalized || geographicAreaConflict(area, address)) return false;
  const [region] = groupParts(area); const present = prefecturesInText(normalized); if (present.length !== 1 || present[0] !== expectedPrefecture(region)) return false;
  const prefecture = normalizeName(expectedPrefecture(region)); const index = normalized.indexOf(prefecture); if (index < 0) return false;
  const tail = normalized.slice(index + prefecture.length);
  return adminLocalities(area).some(locality => {
    const token = normalizeAddress(locality); if (!token) return false; if (tail.startsWith(token)) return true;
    const county = /^[^市区町村]{1,20}郡/.exec(tail)?.[0]; return Boolean(county && tail.slice(county.length).startsWith(token));
  });
}
export function candidateAddressMatchesArea(area: string, address: string | null): boolean {
  const normalized = normalizeAddress(address); if (!normalized || !candidateAddressMatchesGroup(area, address)) return false;
  if (areaIsMunicipality(area)) return true;
  const [region] = groupParts(area); const embedded = areasInGroup(normalized, area); if (embedded.size && !singleton(embedded, area)) return false;
  if (regionMaster.broad_localities[area]) return true; const token = normalizeName(leaf(area)); return region === '東京' ? nonAdminLeaf(normalized, token) : normalized.includes(token);
}
export function candidateAddressPreciselyMatchesArea(area: string, address: string | null): boolean {
  const normalized = normalizeAddress(address); if (!normalized || !candidateAddressMatchesGroup(area, address)) return false;
  const geographic = normalized.replace(/^〒?[0-9]{3}-?[0-9]{4}/, ''); const number = /[0-9]/.exec(geographic); if (!number) return false; const prefix = geographic.slice(0, number.index);
  if (areaIsMunicipality(area)) { const municipality = areaMunicipality(area); const locality = municipality ? normalizeAddress(municipality.prefecture + municipality.name) : null; return Boolean(locality && prefix.startsWith(locality) && prefix.slice(locality.length)); }
  const embedded = areasInGroup(prefix, area); if (embedded.size && !singleton(embedded, area)) return false;
  const [region] = groupParts(area); const tokens = (regionMaster.precise_aliases[area] ?? [leaf(area)]).map(normalizeName);
  return region === '東京' ? tokens.some(token => nonAdminLeaf(prefix, token)) : tokens.some(token => prefix.includes(token));
}
export function candidateCanonicalArea(candidate: CandidateIdentity): string | null {
  if (!candidate.address) return null; const area = areaFromAddress(candidate.address);
  return area && candidateAddressPreciselyMatchesArea(area, candidate.address) && (!candidate.area || candidateAreaContextMatches(area, candidate.area)) ? area : null;
}
export function effectiveCandidateArea(mention: ExtractedMention, candidate: CandidateIdentity): string | null {
  const area = canonicalArea(mention.area); if (area && !areaIsMunicipality(area)) return area;
  const selected = candidateCanonicalArea(candidate); return selected && (!mention.area || candidateAreaContextMatches(selected, mention.area)) ? selected : null;
}
export function candidateMatchesArea(area: string, candidate: CandidateIdentity): boolean {
  const canonical = canonicalArea(candidate.area);
  if (canonical) {
    if (canonical !== area && (areaIsMunicipality(canonical) || areaIsMunicipality(area))) return Boolean(candidate.address && candidateAreaContextMatches(area, candidate.area ?? '') && candidateAddressMatchesArea(area, candidate.address) && candidateAddressMatchesGroup(canonical, candidate.address));
    return canonical === area && (!candidate.address || candidateAddressMatchesGroup(area, candidate.address));
  }
  if (candidate.area && candidateAreaHasConflict(area, candidate.area, candidate.address)) return false;
  if (candidateAddressMatchesArea(area, candidate.address)) return true;
  return Boolean(candidate.area && normalizeName(candidate.area).includes(normalizeName(leaf(area))) && candidateAreaContextMatches(area, candidate.area) && candidateAddressMatchesGroup(area, candidate.address));
}
export function nameWithBranch(name: string, branch: string | null): string { const suffix = normalizeName(branch ?? ''); return suffix && !normalizeName(name).endsWith(suffix) ? name + ' ' + branch : name; }
export function matchesMentionNameAndArea(mention: ExtractedMention, candidate: CandidateIdentity): boolean {
  const area = effectiveCandidateArea(mention, candidate); const name = nameWithBranch(mention.shop_name, mention.branch_name);
  return area !== null && normalizeName(candidate.name) === normalizeName(name) && candidateMatchesArea(area, candidate) && !branchesConflict(name, candidate.name);
}
