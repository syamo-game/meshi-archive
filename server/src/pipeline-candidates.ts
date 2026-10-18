import { z } from 'zod';
import type { ExtractedMention } from './ai-models.js';
import { candidateAddressMatchesGroup, candidateAddressPreciselyMatchesArea, candidateMatchesArea, effectiveCandidateArea, matchesMentionNameAndArea, nameWithBranch } from './candidate-geography.js';
import { canonicalArea } from './geography.js';
import { pageAddressesMatch } from './page-evidence.js';
import { evaluateIdentity, extractBranchToken, identitySchema, nameSimilarity, normalizeAddress, normalizeArea, normalizeExternalSource, normalizeName, normalizePhone } from './resolution.js';
import type { CandidateIdentity } from './resolution.js';
import { collectExternalIdentities, identitiesConflict } from './shop-creation.js';
import { citationUrlIdentity, sourceEvidenceUrlIdentity } from './source-evidence.js';
import { casefold, compareText } from './text.js';

export const pipelineCandidateSchema = identitySchema.extend({ provenance: z.enum(['posted_url', 'structured_data', 'web_search', 'image']), is_verified: z.boolean(), verification_reason: z.string().max(2000).nullable().default(null) });
export type PipelineCandidate = z.infer<typeof pipelineCandidateSchema>;
export const topWebPolicy = 'unique_top_web_matching_score_080';
export function candidateIdentities(candidate: CandidateIdentity): ReadonlyMap<string, ReadonlySet<string>> { return collectExternalIdentities(candidate.external_source, candidate.external_id, [candidate.canonical_url, candidate.evidence_url]); }
function externalKeys(candidate: CandidateIdentity): ReadonlySet<string> { return new Set([...candidateIdentities(candidate)].flatMap(([source, ids]) => [...ids].map(id => `${source}:${id}`))); }
function intersects(left: ReadonlySet<string>, right: ReadonlySet<string>): boolean { return [...left].some(value => right.has(value)); }
function evidenceKeys(candidate: CandidateIdentity): ReadonlySet<string> { return new Set([candidate.canonical_url, candidate.evidence_url].filter((url): url is string => Boolean(url)).map(sourceEvidenceUrlIdentity)); }
export function candidateSourceKeys(candidate: CandidateIdentity): ReadonlySet<string> { return new Set([...externalKeys(candidate)].map(value => 'external:' + value).concat([candidate.canonical_url, candidate.evidence_url].filter((url): url is string => Boolean(url)).map(url => 'url:' + citationUrlIdentity(url)))); }
export function dedupeCandidates(candidates: readonly PipelineCandidate[]): PipelineCandidate[] {
  const result: PipelineCandidate[] = []; const indexes = new Map<string, number>();
  for (const candidate of candidates) {
    const key = JSON.stringify([candidate.provenance, [...externalKeys(candidate)].sort(compareText).join('|'), normalizeName(candidate.name), normalizeArea(candidate.area), normalizeName(candidate.category ?? ''), normalizeAddress(candidate.address) ?? '', normalizePhone(candidate.phone) ?? '', candidate.canonical_url ? citationUrlIdentity(candidate.canonical_url) : '', candidate.evidence_url ? citationUrlIdentity(candidate.evidence_url) : '']);
    const index = indexes.get(key);
    if (index === undefined) { indexes.set(key, result.length); result.push(candidate); }
    else if (candidate.is_verified && !result[index]?.is_verified) result[index] = candidate;
  }
  return result.sort((a, b) => Number(b.is_verified) - Number(a.is_verified));
}
export function replaceWebCandidate(candidates: readonly PipelineCandidate[], verified: PipelineCandidate): PipelineCandidate[] {
  const keys = candidateSourceKeys(verified);
  return dedupeCandidates([...candidates.filter(candidate => !(candidate.provenance === 'web_search' && !candidate.is_verified && normalizeName(candidate.name) === normalizeName(verified.name) && pageAddressesMatch(candidate.address, verified.address) && intersects(keys, candidateSourceKeys(candidate)))), verified]);
}
export function candidateBranchMatches(mention: ExtractedMention, name: string): boolean {
  if (!mention.branch_name && extractBranchToken(mention.shop_name)) return false;
  const branch = normalizeName(mention.branch_name ?? '');
  if (extractBranchToken(name) && !branch) return false;
  if (!branch) return normalizeName(name) === normalizeName(mention.shop_name);
  const parts = name.split(/[\s　()（）\[\]［］{}｛｝「」『』【】〈〉《》・\-‐‑–—]+/u).map(normalizeName).filter(Boolean);
  return parts.length >= 2 ? parts.at(-1) === branch : normalizeName(name) === normalizeName(nameWithBranch(mention.shop_name, mention.branch_name));
}
export function structuredSourceConflict(mention: ExtractedMention, selected: PipelineCandidate, candidates: readonly PipelineCandidate[]): boolean {
  const keys = candidateSourceKeys(selected);
  if (!keys.size || identitiesConflict(candidateIdentities(selected))) return true;
  return candidates.some(other => other !== selected && other.provenance === 'structured_data' && other.is_verified && intersects(keys, candidateSourceKeys(other)) && (identitiesConflict(candidateIdentities(other)) || !matchesMentionNameAndArea(mention, other) || evaluateIdentity(selected, other).conflicting_fields.length > 0));
}
function structuredMatches(mention: ExtractedMention, candidates: readonly PipelineCandidate[]): PipelineCandidate[] { return candidates.filter(candidate => candidate.provenance === 'structured_data' && candidate.is_verified && matchesMentionNameAndArea(mention, candidate)); }
export function candidateForNewShop(mention: ExtractedMention, candidates: readonly PipelineCandidate[]): PipelineCandidate | null {
  const matches = structuredMatches(mention, candidates); const selected = matches[0];
  if (matches.length !== 1 || !selected || structuredSourceConflict(mention, selected, candidates)) return null;
  const area = effectiveCandidateArea(mention, selected);
  return area ? { ...selected, area } : null;
}
export function structuredBlockReason(mention: ExtractedMention, candidates: readonly PipelineCandidate[]): string | null {
  const matches = structuredMatches(mention, candidates);
  if (matches.length > 1) return '店名・支店名・エリアが一致する構造化候補が複数ある';
  return matches.length === 1 && matches[0] && structuredSourceConflict(mention, matches[0], candidates) ? '同じ構造化データ出典の店舗候補が矛盾' : null;
}
export function webIdentityConflict(mention: ExtractedMention, selected: PipelineCandidate, candidates: readonly PipelineCandidate[]): boolean {
  const identities = externalKeys(selected); const area = effectiveCandidateArea(mention, selected);
  return candidates.some(other => other !== selected && other.provenance === 'web_search' && intersects(identities, externalKeys(other)) && (!candidateBranchMatches(mention, other.name) || area === null || !candidateMatchesArea(area, other) || evaluateIdentity(selected, other).conflicting_fields.length > 0));
}
export function unresolvedRedirect(urls: readonly string[]): boolean { return urls.some(url => ['bit.ly', 'goo.gl', 'maps.app.goo.gl', 't.co', 'tinyurl.com'].includes(new URL(url).hostname.toLowerCase().replace(/^www\./, ''))); }
export function webStructuredConflict(mention: ExtractedMention, area: string, selected: PipelineCandidate, candidates: readonly PipelineCandidate[], originalSources: ReadonlySet<string>): boolean {
  if (structuredBlockReason(mention, candidates)) return true;
  const selectedKeys = evidenceKeys(selected);
  for (const item of candidates) {
    if (item.provenance !== 'structured_data' || !item.is_verified) continue;
    const keys = evidenceKeys(item);
    const sameService = Boolean(selected.external_source && selected.external_id && item.external_source && item.external_id && normalizeExternalSource(selected.external_source) === normalizeExternalSource(item.external_source));
    const sameId = sameService && casefold(selected.external_id?.trim() ?? '') === casefold(item.external_id?.trim() ?? '');
    if (!intersects(selectedKeys, keys) && !sameId && !intersects(keys, originalSources)) continue;
    if (!candidateBranchMatches(mention, item.name) || (!mention.branch_name && normalizeName(item.name) !== normalizeName(mention.shop_name))) return true;
    if (evaluateIdentity(selected, item).conflicting_fields.some(field => field !== 'address')) return true;
    const address = (value: string | null): string => (normalizeAddress(value) ?? '').replace(/^〒?[0-9]{3}-?[0-9]{4}/, '');
    const a = address(selected.address); const b = address(item.address);
    if (a && b && a !== b) return true;
    const itemArea = canonicalArea(item.area);
    if (itemArea !== null && itemArea !== area) return true;
    if (item.address && !candidateAddressMatchesGroup(area, item.address)) return true;
    if (sameService && !sameId) return true;
  }
  return false;
}
export function topWebCandidate(mention: ExtractedMention, candidates: readonly PipelineCandidate[], precise = false, excludedUrls: readonly string[] = []): PipelineCandidate | null {
  if (unresolvedRedirect(excludedUrls)) return null;
  const excluded = new Set(excludedUrls.map(sourceEvidenceUrlIdentity));
  const scored = candidates.filter(candidate => candidate.provenance === 'web_search').map(candidate => ({ candidate, score: nameSimilarity(nameWithBranch(mention.shop_name, mention.branch_name), candidate.name) }));
  const score = Math.max(...scored.map(item => item.score)); const top = scored.filter(item => item.score === score);
  const candidate = top[0]?.candidate;
  if (!candidate || score < 0.8 || top.length !== 1 || identitiesConflict(candidateIdentities(candidate)) || webIdentityConflict(mention, candidate, candidates)) return null;
  const area = effectiveCandidateArea(mention, candidate);
  if (!area) return null;
  const urls = [candidate.evidence_url, candidate.canonical_url].filter((url): url is string => Boolean(url));
  if (!urls.length || unresolvedRedirect(urls) || intersects(evidenceKeys(candidate), excluded)) return null;
  if (precise && (!candidate.address || !candidateAddressPreciselyMatchesArea(area, candidate.address))) return null;
  if (!candidateBranchMatches(mention, candidate.name) || (precise && !mention.branch_name && normalizeName(candidate.name) !== normalizeName(mention.shop_name))) return null;
  if (!candidateMatchesArea(area, candidate) || webStructuredConflict(mention, area, candidate, candidates, excluded)) return null;
  return { ...candidate, area, verification_reason: `policy=${topWebPolicy}; matching_score=${score.toFixed(3)}; canonical_area=${area}; source_verified=false` };
}
export function pageCandidateCacheValue(mention: ExtractedMention, candidate: PipelineCandidate, url: string): string { return [citationUrlIdentity(url), normalizeName(mention.shop_name), normalizeName(mention.branch_name ?? ''), normalizeArea(mention.area), normalizeName(candidate.name), normalizeAddress(candidate.address) ?? ''].join('|'); }
export function pageVerifiedCandidateValid(mention: ExtractedMention, candidate: PipelineCandidate): boolean {
  const area = effectiveCandidateArea(mention, candidate);
  return Boolean(candidate.provenance === 'web_search' && candidate.is_verified && area && candidate.address && candidateAddressPreciselyMatchesArea(area, candidate.address) && candidateBranchMatches(mention, candidate.name) && candidateMatchesArea(area, candidate));
}
export function verifyFromStructuredPage(mention: ExtractedMention, candidate: PipelineCandidate, candidates: readonly PipelineCandidate[]): PipelineCandidate | null {
  const keys = candidateSourceKeys(candidate);
  if (!keys.size || !candidate.address) return null;
  const matches = candidates.filter(item => item.provenance === 'structured_data' && item.is_verified && intersects(keys, candidateSourceKeys(item)));
  const selected = matches[0];
  if (matches.length !== 1 || !selected || normalizeName(selected.name) !== normalizeName(candidate.name) || !pageAddressesMatch(selected.address, candidate.address)) return null;
  const area = effectiveCandidateArea(mention, selected); if (!area) return null;
  const verified: PipelineCandidate = { ...selected, area, provenance: 'web_search', is_verified: true, verification_reason: `policy=${topWebPolicy}; source_verified=true; matching_score=${nameSimilarity(nameWithBranch(mention.shop_name, mention.branch_name), candidate.name).toFixed(3)}; proof=structured_data_cache` };
  return pageVerifiedCandidateValid(mention, verified) ? verified : null;
}
