import { createHash } from 'node:crypto';
import { areas, canonicalArea } from './geography.js';
import type { CandidateRecord, MentionRecord } from './records.js';
import { branchesConflict, extractBranchToken, nameSimilarity, normalizeAddress, normalizeExternalIdentity, normalizeName, normalizePhone, normalizeUrlIdentity } from './resolution.js';

export interface ReviewGroup { key: string; reason: string; mention_ids: readonly number[]; }
function normalizedName(name: string, branch: string | null): string {
  const normalized = normalizeName(name); const suffix = normalizeName(branch || extractBranchToken(name) || '');
  return suffix && !normalized.endsWith(suffix) ? normalized + suffix : normalized;
}
function canGroup(mention: MentionRecord, candidate: CandidateRecord): boolean {
  if (candidate.provenance === 'image' || (!candidate.is_verified && candidate.provenance !== 'web_search')) return false;
  const name = [mention.extracted_name, mention.extracted_branch_name].filter(Boolean).join(' ');
  return !branchesConflict(name, candidate.name) && nameSimilarity(name, candidate.name) >= 0.8;
}
function externalIds(candidates: readonly CandidateRecord[]): Map<string, Set<string>> {
  const result = new Map<string, Set<string>>();
  for (const candidate of candidates) {
    const identity = normalizeExternalIdentity(candidate.external_source, candidate.external_id); if (!identity) continue;
    const ids = result.get(identity[0]) ?? new Set<string>(); ids.add(identity[1]); result.set(identity[0], ids);
  }
  return result;
}
export function mentionExactIdentity(mention: MentionRecord, candidates: readonly CandidateRecord[], allowUnverified = false): readonly [string, string, string] | null {
  const name = normalizedName(mention.extracted_name, mention.extracted_branch_name); if (!name) return null;
  const extracted = canonicalArea(mention.extracted_area); if (!extracted && mention.extracted_area?.trim()) return null;
  const matching = candidates.filter(candidate => (candidate.is_verified || (allowUnverified && canGroup(mention, candidate))) && normalizedName(candidate.name, null) === name);
  const conflict = [...externalIds(matching).values()].some(ids => ids.size > 1) || new Set(matching.map(candidate => normalizePhone(candidate.phone)).filter(Boolean)).size > 1 || new Set(matching.map(candidate => normalizeAddress(candidate.address)).filter(Boolean)).size > 1;
  const candidateAreas = new Set(conflict ? [] : matching.flatMap(candidate => { const area = canonicalArea(candidate.area); return area ? [area] : []; }));
  if (extracted && [...candidateAreas].some(area => area !== extracted)) return null;
  const area = extracted ?? (candidateAreas.size === 1 ? [...candidateAreas][0] : null);
  if (!area || areas.get(area)?.municipality === area) return null;
  return [name, normalizeName(mention.extracted_branch_name || extractBranchToken(mention.extracted_name) || ''), area];
}
function key(kind: string, value: string): string { return kind + ':' + createHash('sha256').update(kind + ':' + value).digest('hex').slice(0, 24); }
export function reviewGroupKeys(mention: MentionRecord, candidates: readonly CandidateRecord[]): readonly (readonly [string, string])[] {
  const keys: (readonly [string, string])[] = []; const groupable = candidates.filter(candidate => canGroup(mention, candidate));
  const identities = externalIds(groupable);
  if (![...identities.values()].some(ids => ids.size > 1)) {
    const pairs = [...identities].flatMap(([source, ids]) => [...ids].map(id => source + ':' + id));
    const unique = (values: readonly (string | null)[]): string | null => { const result = new Set(values.filter((value): value is string => value !== null)); return result.size === 1 ? [...result][0] ?? null : null; };
    const external = pairs.length === 1 ? pairs[0] : null;
    const canonical = unique(groupable.map(candidate => normalizeUrlIdentity(candidate.canonical_url)));
    const phone = unique(groupable.map(candidate => { const value = normalizePhone(candidate.phone); return value ? value + ':' + normalizeName(candidate.name) : null; }));
    const address = unique(groupable.map(candidate => { const value = normalizeAddress(candidate.address); return value ? value + ':' + normalizeName(candidate.name) : null; }));
    if (external) keys.push([key('external_id', external), '候補の外部IDが一致']);
    else if (canonical) keys.push([key('canonical_url', canonical), '候補の店舗URLが一致']);
    else if (phone) keys.push([key('phone_name', phone), '候補の電話番号と店名が一致']);
    else if (address) keys.push([key('address_name', address), '候補の住所と店名が一致']);
  }
  const exact = mentionExactIdentity(mention, candidates, true);
  if (exact) keys.push([key('mention_identity', exact.join(':')), '抽出した店名・支店名・エリアが一致']);
  return keys;
}
export function reviewGroups(mentions: readonly MentionRecord[], candidates: readonly CandidateRecord[]): ReadonlyMap<number, ReviewGroup> {
  const groups = new Map<string, ReviewGroup>();
  for (const mention of mentions) {
    if (mention.review_status !== 'pending' && mention.review_status !== 'deferred') continue;
    for (const [groupKey, reason] of reviewGroupKeys(mention, candidates.filter(candidate => candidate.mention_id === mention.id))) {
      const previous = groups.get(groupKey); groups.set(groupKey, { key: groupKey, reason, mention_ids: [...(previous?.mention_ids ?? []), mention.id] });
    }
  }
  const priorities = ['external_id', 'canonical_url', 'phone_name', 'address_name', 'mention_identity'];
  const ordered = [...groups.values()].sort((left, right) => Number(left.mention_ids.length < 2) - Number(right.mention_ids.length < 2) || priorities.indexOf(left.key.split(':')[0] ?? '') - priorities.indexOf(right.key.split(':')[0] ?? '') || right.mention_ids.length - left.mention_ids.length || (left.key < right.key ? -1 : left.key > right.key ? 1 : 0));
  const index = new Map<number, ReviewGroup>();
  for (const group of ordered) {
    const ids = group.mention_ids.filter(id => !index.has(id)); if (!ids.length) continue;
    const summary = { ...group, mention_ids: ids }; for (const id of ids) index.set(id, summary);
  }
  return index;
}
