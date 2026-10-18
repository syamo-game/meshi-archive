import type { Executor } from './db/database.js';
import { AppError } from './errors.js';
import { areas, canonicalArea, canonicalCategory } from './geography.js';
import { candidateRecord, mentionRecord, shopRecord } from './records.js';
import type { CandidateRecord, MentionRecord, ShopRecord } from './records.js';
import { mentionExactIdentity } from './review-groups.js';
import { branchesConflict, evaluateIdentity, extractBranchToken, normalizeAddress, normalizeExternalIdentity, normalizeName, normalizePhone } from './resolution.js';
import type { CandidateIdentity } from './resolution.js';

interface ResolutionData { shops: readonly ShopRecord[]; mentions: readonly MentionRecord[]; candidates: readonly CandidateRecord[]; }
export type ReusableBasis = 'existing_shop' | 'verified_candidate';
function branch(name: string, suffix: string | null): string { return normalizeName(suffix || extractBranchToken(name) || ''); }
function identity(shop: ShopRecord): CandidateIdentity { return { name: [shop.shop_name, shop.branch_name].filter(Boolean).join(' ').trim(), area: shop.area, category: null, address: shop.address, phone: shop.phone, external_source: shop.external_source, external_id: shop.external_id, canonical_url: shop.canonical_url, evidence_url: null }; }
function candidateIdentity(candidate: CandidateRecord): CandidateIdentity { return { name: candidate.name, area: candidate.area, category: candidate.category, address: candidate.address, phone: candidate.phone, external_source: candidate.external_source, external_id: candidate.external_id, canonical_url: candidate.canonical_url, evidence_url: candidate.evidence_url }; }
function candidateConflict(candidates: readonly CandidateRecord[]): boolean {
  const sources = new Map<string, Set<string>>();
  for (const candidate of candidates) { const value = normalizeExternalIdentity(candidate.external_source, candidate.external_id); if (value) { const ids = sources.get(value[0]) ?? new Set<string>(); ids.add(value[1]); sources.set(value[0], ids); } }
  return [...sources.values()].some(ids => ids.size > 1) || new Set(candidates.map(candidate => normalizePhone(candidate.phone)).filter(Boolean)).size > 1 || new Set(candidates.map(candidate => normalizeAddress(candidate.address)).filter(Boolean)).size > 1;
}
function mentionConflicts(mention: MentionRecord, candidates: readonly CandidateRecord[], shop: ShopRecord): boolean {
  const mentionBranch = branch(mention.extracted_name, mention.extracted_branch_name); const shopBranch = branch(shop.shop_name, shop.branch_name);
  if (mentionBranch && shopBranch && mentionBranch !== shopBranch) return true;
  const extracted = canonicalArea(mention.extracted_area); const area = canonicalArea(shop.area);
  if (extracted && area && extracted !== area) return true;
  return candidates.filter(candidate => candidate.is_verified || candidate.is_strong_match).some(candidate => {
    const candidateArea = canonicalArea(candidate.area); if (candidateArea && area && candidateArea !== area) return true;
    return evaluateIdentity(identity(shop), candidateIdentity(candidate)).conflicting_fields.some(field => ['branch', 'external_id', 'address', 'phone'].includes(field));
  });
}
function strongShopIds(mention: MentionRecord, candidates: readonly CandidateRecord[], shops: readonly ShopRecord[]): ReadonlySet<number> {
  const name = [mention.extracted_name, mention.extracted_branch_name].filter(Boolean).join(' '); const ids = new Set<number>();
  for (const candidate of candidates) {
    if (!candidate.is_verified || branchesConflict(name, candidate.name)) continue;
    for (const shop of shops) {
      if (mentionConflicts(mention, candidates, shop)) continue;
      const outcome = evaluateIdentity(identity(shop), candidateIdentity(candidate)); if (outcome.is_strong_match && !outcome.conflicting_fields.length) ids.add(shop.id);
    }
  }
  return ids;
}
function shopExact(shop: ShopRecord): readonly [string, string, string] | null {
  const area = canonicalArea(shop.area); let name = normalizeName(shop.shop_name); const suffix = branch(shop.shop_name, shop.branch_name);
  if (suffix && !name.endsWith(suffix)) name += suffix;
  return name && area && areas.get(area)?.municipality !== area ? [name, suffix, area] : null;
}
export function reusableResolutionBasis(mention: MentionRecord, target: ShopRecord, data: ResolutionData): ReusableBasis | null {
  if (mention.extraction_error?.startsWith('evidence_review:') || (mention.shop_id !== null && mention.shop_id !== target.id)) return null;
  const candidates = data.candidates.filter(candidate => candidate.mention_id === mention.id);
  if (candidateConflict(candidates.filter(candidate => candidate.is_verified)) || mentionConflicts(mention, candidates, target)) return null;
  const approved = data.shops.filter(shop => data.mentions.some(item => item.shop_id === shop.id && item.review_status === 'approved'));
  const externalIds = new Set(candidates.filter(candidate => candidate.is_verified).flatMap(candidate => { const value = normalizeExternalIdentity(candidate.external_source, candidate.external_id); return value ? [JSON.stringify(value)] : []; }));
  if (approved.some(shop => shop.id !== target.id && externalIds.has(JSON.stringify(normalizeExternalIdentity(shop.external_source, shop.external_id))))) return null;
  const exact = mentionExactIdentity(mention, candidates);
  const strong = strongShopIds(mention, candidates, approved);
  if (exact) {
    const key = JSON.stringify(exact);
    const exactIds = approved.filter(shop => JSON.stringify(shopExact(shop)) === key || data.mentions.some(item => item.shop_id === shop.id && item.review_status === 'approved' && JSON.stringify(mentionExactIdentity(item, data.candidates.filter(candidate => candidate.mention_id === item.id))) === key)).map(shop => shop.id);
    if (exactIds.length === 1 && exactIds[0] === target.id && ![...strong].some(id => id !== target.id)) return 'existing_shop';
  }
  return strong.size === 1 && strong.has(target.id) ? 'verified_candidate' : null;
}
export function metadataAssessment(shop: ShopRecord, mention: MentionRecord): Pick<MentionRecord, 'metadata_review_status' | 'metadata_difference_type' | 'metadata_reviewed_at'> {
  const reason = !shop.area ? mention.extracted_area && !canonicalArea(mention.extracted_area) ? 'unknown_area' : 'missing_area' : !canonicalArea(shop.area) ? 'unknown_area' : !shop.category ? 'missing_category' : !canonicalCategory(shop.category) ? 'unknown_category' : null;
  return { metadata_review_status: reason ? 'pending' : 'approved', metadata_difference_type: reason, metadata_reviewed_at: reason ? null : new Date().toISOString() };
}
export async function autoResolvePending(connection: Executor, sourceId: number, target: ShopRecord): Promise<readonly number[]> {
  const load = async (): Promise<ResolutionData> => ({ shops: await connection.query('SELECT * FROM shops ORDER BY id', shopRecord), mentions: await connection.query('SELECT * FROM shop_mentions ORDER BY id', mentionRecord), candidates: await connection.query('SELECT * FROM resolution_candidates ORDER BY rank,id', candidateRecord) });
  const initial = await load(); const ids = initial.mentions.filter(mention => mention.id !== sourceId && mention.review_status === 'pending' && reusableResolutionBasis(mention, target, initial) !== null).map(mention => mention.id);
  if (!ids.length) return [];
  const placeholders = ids.map((_, index) => '$' + (index + 1)).join(',');
  const locked = await connection.query(`SELECT * FROM shop_mentions WHERE id IN (${placeholders}) AND review_status='pending' ORDER BY id${connection.dialect === 'postgres' ? ' FOR UPDATE SKIP LOCKED' : ''}`, mentionRecord, ids);
  const current = await load(); const resolved: number[] = [];
  for (const mention of locked) {
    const basis = reusableResolutionBasis(mention, target, current); if (!basis) continue;
    const now = new Date().toISOString(); const metadata = canonicalArea(target.area) && canonicalCategory(target.category) ? { metadata_review_status: 'approved', metadata_difference_type: null, metadata_reviewed_at: now } : mention.metadata_review_status === 'deferred' ? mention : metadataAssessment(target, { ...mention, extracted_area: null });
    const changed = await connection.execute("UPDATE shop_mentions SET shop_id=$3,resolution_status='resolved',review_status='approved',resolution_method='automatic',resolution_basis=$4,reused_from_mention_id=$5,reviewed_at=$6,metadata_review_status=$7,metadata_difference_type=$8,metadata_reviewed_at=$9,version=version+1 WHERE id=$1 AND version=$2 AND review_status='pending'", [mention.id, mention.version, target.id, basis, sourceId, now, metadata.metadata_review_status, metadata.metadata_difference_type, metadata.metadata_reviewed_at]);
    if (changed !== 1) throw new AppError(409, '関連する確認項目が更新されています。再確認してください。');
    await connection.execute("INSERT INTO review_events(mention_id,scope,action,previous_shop_id,selected_shop_id,note,created_at) VALUES($1,'identity','auto_reuse_approved_identity',$2,$3,$4,$5)", [mention.id, mention.shop_id, target.id, `basis=${basis}; reused_from_mention_id=${sourceId}`, now]);
    resolved.push(mention.id);
  }
  return resolved;
}
