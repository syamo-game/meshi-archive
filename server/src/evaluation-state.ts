import { z } from 'zod';
import type { ExtractedMention } from './ai-models.js';
import { nameWithBranch } from './candidate-geography.js';
import type { Executor } from './db/database.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { shopIdentity } from './pipeline-shops.js';
import { assetRecord, mentionRecord, messageRecord, shopRecord } from './records.js';
import type { AssetRecord, MentionRecord, MessageRecord, ShopRecord } from './records.js';
import { branchesConflict, evaluateIdentity, nameSimilarity, normalizeAddress, normalizeArea, normalizePhone } from './resolution.js';
import type { CandidateIdentity } from './resolution.js';
import { lockShopCreation } from './shop-creation.js';

export class EvaluationChanged extends Error {}
export interface EvaluationState { message: MessageRecord; mentions: MentionRecord[]; assets: AssetRecord[]; shops: ShopRecord[]; }
export async function loadEvaluation(connection: Executor, id: string, lock = false): Promise<EvaluationState> {
  const suffix = lock && connection.dialect === 'postgres' ? ' FOR UPDATE' : '';
  const message = (await connection.query('SELECT * FROM messages WHERE message_id=$1' + suffix, messageRecord, [id]))[0];
  if (!message) throw new EvaluationChanged('message_removed');
  const mentions = await connection.query('SELECT * FROM shop_mentions WHERE message_id=$1 ORDER BY id' + suffix, mentionRecord, [id]);
  const assets = await connection.query('SELECT * FROM source_assets WHERE message_id=$1 ORDER BY id' + suffix, assetRecord, [id]);
  const shops = await connection.query('SELECT * FROM shops WHERE id IN (SELECT shop_id FROM shop_mentions WHERE message_id=$1) ORDER BY id' + suffix, shopRecord, [id]);
  return { message, mentions, assets, shops };
}
export function evaluationSnapshot(state: EvaluationState): string {
  return JSON.stringify({ message: state.message, assets: state.assets.map(asset => [asset.id, asset.kind, asset.url, asset.title, asset.description, asset.mime_type]), mentions: state.mentions.map(row => [row.id, row.version, row.shop_id, row.occurrence_index]), shops: state.shops.map(row => [row.id, row.version]) });
}
export async function humanReviewed(connection: Executor, state: EvaluationState): Promise<boolean> {
  if (state.mentions.some(row => row.resolution_method === 'manual' && row.extraction_source !== 'legacy_import')) return true;
  return (await connection.query('SELECT id FROM review_events WHERE mention_id IN (SELECT id FROM shop_mentions WHERE message_id=$1) LIMIT 1', z.object({ id: z.number() }), [state.message.message_id])).length > 0;
}
export async function lockEvaluation(connection: Executor, id: string, expected: string): Promise<EvaluationState> {
  await lockShopCreation(connection);
  const state = await loadEvaluation(connection, id, true);
  if (evaluationSnapshot(state) !== expected) throw new EvaluationChanged('concurrent_change');
  if (await humanReviewed(connection, state)) throw new EvaluationChanged('human_reviewed');
  return state;
}
export function matchLegacyMention(extracted: ExtractedMention, unmatched: readonly MentionRecord[], shops: readonly ShopRecord[]): MentionRecord | null {
  const ranked = unmatched.flatMap(mention => { const shop = shops.find(row => row.id === mention.shop_id); return shop ? [{ mention, score: nameSimilarity(extracted.shop_name, shop.shop_name) }] : []; }).sort((a, b) => b.score - a.score);
  const first = ranked[0]; const second = ranked[1];
  return !first || first.score < 0.8 || (second && first.score - second.score < 0.05) ? null : first.mention;
}
export function identityCandidateDiffers(shop: ShopRecord, candidate: CandidateIdentity | null): boolean {
  if (!candidate) return false;
  const name = nameWithBranch(shop.shop_name, shop.branch_name);
  if (candidate.name && (nameSimilarity(name, candidate.name) < 0.9 || branchesConflict(name, candidate.name))) return true;
  const pairs: readonly [string | null, string | null][] = [[candidate.area ? normalizeArea(candidate.area) : null, normalizeArea(shop.area)], [normalizeAddress(candidate.address), normalizeAddress(shop.address)], [normalizePhone(candidate.phone), normalizePhone(shop.phone)], [candidate.canonical_url, shop.canonical_url], [candidate.external_source, shop.external_source], [candidate.external_id, shop.external_id]];
  return pairs.some(([proposed, current]) => proposed !== null && proposed !== current);
}
export function identityExtractionDiffers(shop: ShopRecord, extracted: ExtractedMention): boolean {
  const current = nameWithBranch(shop.shop_name, shop.branch_name); const proposed = nameWithBranch(extracted.shop_name, extracted.branch_name);
  return nameSimilarity(current, proposed) < 0.9 || branchesConflict(current, proposed) || Boolean(extracted.area && normalizeArea(extracted.area) !== normalizeArea(shop.area));
}
export function strongCandidateForShop(shop: ShopRecord, candidates: readonly PipelineCandidate[]): PipelineCandidate | null {
  const score = (candidate: PipelineCandidate): number => [candidate.external_id, candidate.phone, candidate.address, candidate.canonical_url].filter(value => value !== null).length;
  return candidates.filter(candidate => candidate.is_verified && evaluateIdentity(shopIdentity(shop), candidate).is_strong_match).sort((a, b) => score(b) - score(a))[0] ?? null;
}
export function markDifference(mention: MentionRecord, reason: string): MentionRecord {
  return { ...mention, review_status: 'pending', difference_type: ['legacy_review', 'source_unavailable'].includes(mention.difference_type ?? '') ? mention.difference_type : reason };
}
export function manualEvidenceRequired(error: string | null): boolean { return Boolean(error?.startsWith('evidence_review:')); }
