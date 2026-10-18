import type { ExtractedMention, ImageAnalysisResult } from './ai-models.js';
import { nameWithBranch } from './candidate-geography.js';
import type { Executor } from './db/database.js';
import { canonicalArea } from './geography.js';
import { pipelineCandidateSchema } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { shopIdentities, shopIdentity } from './pipeline-shops.js';
import { shopRecord } from './records.js';
import type { ShopRecord } from './records.js';
import { areaIsMunicipality } from './region-master.js';
import { branchesConflict, evaluateIdentity, nameSimilarity, normalizeAddress, normalizeName, normalizePhone } from './resolution.js';
import { identitiesConflict } from './shop-creation.js';

export function imageCandidates(result: ImageAnalysisResult, url: string): PipelineCandidate[] {
  const clues = result.clues; if (!clues.usable || !clues.visible_shop_names.length) return [];
  const names: string[] = []; const seen = new Set<string>();
  for (const name of clues.visible_shop_names) { const normalized = normalizeName(name); if (normalized && !seen.has(normalized)) { seen.add(normalized); names.push(name); } }
  const addresses = new Map(clues.address_clues.map(value => [normalizeAddress(value), value] as const).filter(([key]) => Boolean(key)));
  const phones = new Map(clues.phone_clues.map(value => [normalizePhone(value), value] as const).filter(([key]) => Boolean(key)));
  const address = names.length === 1 && addresses.size === 1 ? [...addresses.values()][0] ?? null : null;
  const phone = names.length === 1 && phones.size === 1 ? [...phones.values()][0] ?? null : null;
  return names.slice(0, 5).map(name => pipelineCandidateSchema.parse({ name, address, phone, evidence_url: url, provenance: 'image', is_verified: false, verification_reason: '画像解析結果は補助根拠であり店舗同定には未検証' }));
}
export function singleImageCandidate(candidates: readonly PipelineCandidate[]): PipelineCandidate | null { const images = candidates.filter(candidate => candidate.provenance === 'image'); return images.length === 1 ? images[0] ?? null : null; }
export function imageMatchesMention(mention: ExtractedMention, candidate: PipelineCandidate): boolean { const name = nameWithBranch(mention.shop_name, mention.branch_name); return !branchesConflict(name, candidate.name) && nameSimilarity(name, candidate.name) >= 0.8; }
export async function existingShopFromImage(connection: Executor, mention: ExtractedMention | null, candidate: PipelineCandidate): Promise<readonly [ShopRecord | null, PipelineCandidate | null, string | null]> {
  if (mention && !imageMatchesMention(mention, candidate)) return [null, null, '画像内の店名が投稿から抽出した店舗と一致しない'];
  const shops = await connection.query("SELECT s.* FROM shops s WHERE EXISTS(SELECT 1 FROM shop_mentions m WHERE m.shop_id=s.id AND m.review_status='approved') ORDER BY s.id", shopRecord);
  const matches: { shop: ShopRecord; reason: string }[] = []; let conflicts = 0;
  for (const shop of shops) {
    if (identitiesConflict(shopIdentities(shop))) { conflicts++; continue; }
    const identity = shopIdentity(shop); const outcome = evaluateIdentity(identity, candidate);
    const area = mention ? canonicalArea(mention.area) : null; const shopArea = canonicalArea(shop.area);
    const exact = Boolean(mention && area && shopArea && !areaIsMunicipality(area) && normalizeName(identity.name) === normalizeName(candidate.name) && area === shopArea && !branchesConflict(identity.name, candidate.name));
    if (outcome.is_strong_match && !outcome.conflicting_fields.length) matches.push({ shop, reason: '画像の' + outcome.reason });
    else if (outcome.is_strong_match) conflicts++;
    else if (exact && !outcome.conflicting_fields.length) matches.push({ shop, reason: '画像の店名・支店名と投稿由来のエリアが一意に一致' });
    else if (exact) conflicts++;
  }
  const selected = matches[0];
  if (matches.length === 1 && selected) return [selected.shop, { ...candidate, is_verified: true, verification_reason: selected.reason }, selected.reason];
  return [null, null, matches.length > 1 ? '画像の強い一致条件を満たす既存店舗が複数ある' : conflicts ? '画像と既存店舗の電話・住所に矛盾がある' : null];
}
export function imageVerificationUrls(image: PipelineCandidate, candidates: readonly PipelineCandidate[]): readonly string[] {
  const urls: string[] = []; const phone = normalizePhone(image.phone); const address = normalizeAddress(image.address);
  for (const candidate of candidates) {
    if (candidate.provenance !== 'web_search' || branchesConflict(image.name, candidate.name) || nameSimilarity(image.name, candidate.name) < 0.8) continue;
    const candidatePhone = normalizePhone(candidate.phone); const candidateAddress = normalizeAddress(candidate.address);
    if ((phone && candidatePhone && phone !== candidatePhone) || (address && candidateAddress && address !== candidateAddress)) continue;
    const url = candidate.canonical_url || candidate.evidence_url; if (url && !urls.includes(url)) urls.push(url); if (urls.length === 2) break;
  }
  return urls;
}
export function structuredCandidateFromImage(image: PipelineCandidate, candidates: readonly PipelineCandidate[]): readonly [PipelineCandidate | null, string | null] {
  const matches: { candidate: PipelineCandidate; reason: string }[] = []; let conflicts = 0;
  for (const candidate of candidates) {
    if (candidate.provenance !== 'structured_data' || !candidate.is_verified) continue;
    const outcome = evaluateIdentity(candidate, image);
    if (outcome.is_strong_match && !outcome.conflicting_fields.length) matches.push({ candidate, reason: '画像と構造化データの' + outcome.reason }); else if (outcome.is_strong_match) conflicts++;
  }
  const selected = matches[0];
  if (matches.length === 1 && selected) return [{ ...selected.candidate, verification_reason: selected.reason }, selected.reason];
  return [null, matches.length > 1 ? '画像と強い一致条件を満たす構造化候補が複数ある' : conflicts ? '画像と構造化候補の電話・住所に矛盾がある' : null];
}

