import type { ExtractedMention } from './ai-models.js';
import { candidateMatchesArea, nameWithBranch } from './candidate-geography.js';
import type { Executor } from './db/database.js';
import { areas, canonicalArea } from './geography.js';
import { canonicalCategory } from './categories.js';
import type { MessageEnvelope } from './ingestion.js';
import { candidateIdentities } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { preferredIdentity } from './pipeline-shops.js';
import { mentionRecord, shopRecord } from './records.js';
import type { MentionRecord, ShopRecord } from './records.js';
import { branchesConflict, nameSimilarity, normalizePhone } from './resolution.js';
import { identitiesConflict } from './shop-creation.js';
import { sourceAssetFingerprint, sourceAssetIdentity } from './source-identity.js';

export function metadataReviewState(area: string | null, category: string | null, categoryEvidence: string | null = category): readonly [MentionRecord['metadata_review_status'], string | null] {
  if (!area) return ['pending', 'missing_area'];
  if (!canonicalArea(area)) return ['pending', 'unknown_area'];
  if (!category) return ['pending', categoryEvidence && !canonicalCategory(categoryEvidence) ? 'unknown_category' : 'missing_category'];
  if (!canonicalCategory(category)) return ['pending', 'unknown_category'];
  return ['approved', null];
}
export async function replaceSourceAssets(connection: Executor, envelope: MessageEnvelope): Promise<void> {
  await connection.execute('DELETE FROM source_assets WHERE message_id=$1', [envelope.message_id]);
  const now = new Date().toISOString();
  for (const asset of envelope.assets) {
    const identity = sourceAssetIdentity(asset.url); const normalized = identity?.normalized_url ?? asset.url;
    await connection.execute("INSERT INTO source_assets(message_id,kind,url,title,description,mime_type,source_service,source_item_id,normalized_url,content_fingerprint,fetch_status,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'available',$11)", [envelope.message_id, asset.kind, asset.url, asset.title, asset.description, asset.mime_type, identity?.source_service ?? null, identity?.source_item_id ?? null, normalized, sourceAssetFingerprint({ kind: asset.kind, normalized_url: normalized, title: asset.title, description: asset.description }), now]);
  }
}
function storageName(mention: ExtractedMention, candidate: PipelineCandidate): string {
  if (!mention.branch_name) return candidate.name;
  const branch = mention.branch_name.trim();
  for (const value of [candidate.name, mention.shop_name]) { const stripped = value.trim(); if (stripped.endsWith(branch)) { const base = stripped.slice(0, -branch.length).replace(/[ 　\-・()（）]+$/, ''); if (base) return base; } }
  return mention.shop_name;
}
export async function createCandidateShop(connection: Executor, mention: ExtractedMention, candidate: PipelineCandidate): Promise<ShopRecord> {
  const now = new Date().toISOString(); const mentionArea = canonicalArea(mention.area); const candidateArea = canonicalArea(candidate.area);
  if (candidate.is_verified && identitiesConflict(candidateIdentities(candidate))) throw new Error('Candidate external identities conflict within one service');
  if (candidate.is_verified && mentionArea && candidate.area?.trim() && !candidateMatchesArea(mentionArea, candidate)) throw new Error(`Verified candidate area conflicts with the mention: mention_area=${mention.area}, candidate_area=${candidate.area}`);
  const area = mentionArea && areas.has(mentionArea) ? mentionArea : candidate.is_verified && candidateArea && areas.has(candidateArea) ? candidateArea : null;
  const rawCategory = candidate.is_verified ? candidate.category || mention.category : mention.category;
  const category = canonicalCategory(rawCategory);
  if (rawCategory && !category) console.warn('Candidate category requires review', { shopName: mention.shop_name, category: rawCategory, provenance: candidate.provenance });
  const [source, id] = candidate.is_verified ? preferredIdentity(candidate) : [null, null];
  const shop = (await connection.query('INSERT INTO shops(shop_name,branch_name,area,category,address,phone,canonical_url,external_source,external_id,is_visited,created_at,updated_at,version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,1) RETURNING *', shopRecord, [storageName(mention, candidate), mention.branch_name, area, category, candidate.is_verified ? candidate.address : null, candidate.is_verified ? normalizePhone(candidate.phone) : null, candidate.is_verified ? candidate.canonical_url : null, source ?? null, id ?? null, false, now, now]))[0];
  if (!shop) throw new Error('Shop insertion returned no row'); return shop;
}
export type NewMention = Omit<MentionRecord, 'id' | 'version' | 'created_at'>;
export function newMention(messageId: string, occurrence: number, extracted: ExtractedMention, sourceUrl: string | null, extractionSource: string): NewMention {
  const [metadata, difference] = metadataReviewState(extracted.area, extracted.category);
  return { message_id: messageId, shop_id: null, occurrence_index: occurrence, extracted_name: extracted.shop_name, extracted_branch_name: extracted.branch_name, extracted_area: extracted.area, extracted_category: extracted.category, source_url: sourceUrl, resolution_status: 'ambiguous', review_status: 'pending', metadata_review_status: metadata, metadata_difference_type: difference, resolution_method: null, difference_type: 'new_ambiguous', extraction_source: extractionSource, extraction_error: null, confidence_reason: extracted.confidence_reason, reviewed_at: null, metadata_reviewed_at: metadata === 'approved' ? new Date().toISOString() : null, reused_from_mention_id: null, resolution_basis: null };
}
export async function saveMention(connection: Executor, row: NewMention): Promise<MentionRecord> {
  const mention = (await connection.query('INSERT INTO shop_mentions(message_id,shop_id,occurrence_index,extracted_name,extracted_branch_name,extracted_area,extracted_category,source_url,resolution_status,review_status,metadata_review_status,metadata_difference_type,resolution_method,difference_type,extraction_source,extraction_error,confidence_reason,reviewed_at,metadata_reviewed_at,reused_from_mention_id,resolution_basis,version,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,1,$22) RETURNING *', mentionRecord, [row.message_id, row.shop_id, row.occurrence_index, row.extracted_name, row.extracted_branch_name, row.extracted_area, row.extracted_category, row.source_url, row.resolution_status, row.review_status, row.metadata_review_status, row.metadata_difference_type, row.resolution_method, row.difference_type, row.extraction_source, row.extraction_error, row.confidence_reason, row.reviewed_at, row.metadata_reviewed_at, row.reused_from_mention_id, row.resolution_basis, new Date().toISOString()]))[0];
  if (!mention) throw new Error(`Mention insertion returned no row: message_id=${row.message_id}; occurrence=${row.occurrence_index}`); return mention;
}
export async function updateMention(connection: Executor, row: MentionRecord): Promise<MentionRecord> {
  const keys = ['shop_id', 'extracted_name', 'extracted_branch_name', 'extracted_area', 'extracted_category', 'source_url', 'resolution_status', 'review_status', 'metadata_review_status', 'metadata_difference_type', 'resolution_method', 'difference_type', 'extraction_source', 'extraction_error', 'confidence_reason', 'reviewed_at', 'metadata_reviewed_at', 'reused_from_mention_id', 'resolution_basis'] as const;
  const result = (await connection.query(`UPDATE shop_mentions SET ${keys.map((key, index) => `${key}=$${index + 1}`).join(',')},version=version+1 WHERE id=$${keys.length + 1} AND version=$${keys.length + 2} RETURNING *`, mentionRecord, [...keys.map(key => row[key]), row.id, row.version]))[0];
  if (!result) throw new Error(`Mention changed during update: mention_id=${row.id}; version=${row.version}`);
  return result;
}
function sameCandidate(left: PipelineCandidate, right: PipelineCandidate | null): boolean { return right !== null && (['name', 'area', 'category', 'address', 'phone', 'canonical_url', 'external_source', 'external_id', 'evidence_url', 'provenance', 'is_verified', 'verification_reason'] as const).every(key => left[key] === right[key]); }
function roundEven(value: number): number { const floor = Math.floor(value); return value - floor === 0.5 ? floor % 2 === 0 ? floor : floor + 1 : Math.round(value); }
export async function storeCandidates(connection: Executor, mention: MentionRecord, extracted: ExtractedMention, candidates: readonly PipelineCandidate[], selected: PipelineCandidate | null, rankOffset = 0): Promise<void> {
  const ranked = selected ? [selected, ...candidates.filter(candidate => !sameCandidate(candidate, selected))] : [...candidates];
  const name = nameWithBranch(extracted.shop_name, extracted.branch_name); const now = new Date().toISOString();
  for (const [index, candidate] of ranked.slice(0, 5).entries()) {
    const populated = [candidate.external_id ? 'external_id' : '', candidate.phone ? 'phone' : '', candidate.address ? 'address' : ''].filter(Boolean).join(',') || null;
    await connection.execute('INSERT INTO resolution_candidates(mention_id,rank,name,area,category,address,phone,canonical_url,external_source,external_id,evidence_url,provenance,is_verified,verification_reason,matched_fields,conflicting_fields,name_similarity_milli,is_strong_match,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)', [mention.id, rankOffset + index + 1, candidate.name, candidate.area, candidate.category, candidate.address, normalizePhone(candidate.phone), candidate.canonical_url, candidate.external_source, candidate.external_id, candidate.evidence_url, candidate.provenance, candidate.is_verified, candidate.verification_reason, populated, branchesConflict(name, candidate.name) ? 'branch' : null, roundEven(nameSimilarity(name, candidate.name) * 1000), sameCandidate(candidate, selected) && candidate.is_verified, now]);
  }
}

