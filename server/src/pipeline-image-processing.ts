import { RestaurantAI } from './ai.js';
import { extractedMentionSchema } from './ai-models.js';
import type { ImageAnalysisResult } from './ai-models.js';
import type { Executor } from './db/database.js';
import { evidenceReviewError, imageEvidenceAssessment, operatingStatusNote } from './extraction-safety.js';
import type { EvidenceAssessment } from './extraction-safety.js';
import type { MessageEnvelope } from './ingestion.js';
import { dedupeCandidates } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { PipelineEvents } from './pipeline-events.js';
import { PipelineEvidence } from './pipeline-evidence.js';
import { existingShopFromImage, imageCandidates, imageVerificationUrls, singleImageCandidate, structuredCandidateFromImage } from './pipeline-images.js';
import { PipelineJournal } from './pipeline-journal.js';
import { PipelineShops } from './pipeline-shops.js';
import { createCandidateShop, metadataReviewState, newMention, saveMention, storeCandidates } from './pipeline-storage.js';
import type { MentionRecord, ShopRecord } from './records.js';
import { extractBranchToken } from './resolution.js';

export async function analyzeImage(ai: RestaurantAI, journal: PipelineJournal, messageId: string, urls: readonly string[]): Promise<ImageAnalysisResult> {
  const result = await ai.analyzeImages(null, urls.slice(0, 1)).catch(error => { if (error instanceof Error) journal.failure(messageId, 'image_analysis', error); throw error; });
  journal.metrics(messageId, 'image_analysis', result.metrics); return result;
}
export async function processImageOnly(connection: Executor, ai: RestaurantAI, journal: PipelineJournal, envelope: MessageEnvelope, urls: readonly string[], sourceUrl: string | null, unresolved: string, allowAutomatic: boolean, input: EvidenceAssessment): Promise<MentionRecord> {
  const firstUrl = urls[0]; if (!firstUrl) throw new Error('Image processing requires an image URL');
  const evidence = new PipelineEvidence(connection, ai, journal, envelope.message_id); const shops = new PipelineShops(connection);
  const result = await analyzeImage(ai, journal, envelope.message_id, urls);
  const assessment = imageEvidenceAssessment(result.clues.subject_kind, envelope.content, unresolved, input);
  let candidates = dedupeCandidates(imageCandidates(result, firstUrl)); const image = singleImageCandidate(candidates);
  const reasons = [...new Set([unresolved, ...input.reasons, '画像補助: ' + result.clues.reason, ...assessment.reasons])];
  if (assessment.codes.includes('event_excluded')) {
    const event = extractedMentionSchema.parse({ shop_name: image?.name ?? '（店舗名未特定）', needs_review: true, confidence_reason: unresolved, subject_kind: 'event', event_origin: result.clues.event_origin });
    return new PipelineEvents(connection, shops, evidence, journal, envelope).process(event, input, 0, sourceUrl, 'responses_vision');
  }
  const automatic = urls.length === 1 && allowAutomatic && !assessment.codes.length;
  if (urls.length !== 1 || !allowAutomatic) reasons.push('複数画像の対応関係を検証できないため自動確定しない');
  const extracted = extractedMentionSchema.parse({ shop_name: image?.name ?? '（店舗名未特定）', branch_name: image ? extractBranchToken(image.name) : null, needs_review: true, confidence_reason: image ? result.clues.reason : unresolved, subject_kind: image ? result.clues.subject_kind : null, operating_status: image ? result.clues.operating_status : null, operating_status_evidence: image ? result.clues.operating_status_evidence : null });
  const status = image ? operatingStatusNote(extracted) : null; if (status) reasons.push(status);
  let chosen: ShopRecord | null = null; let selected: PipelineCandidate | null = null; let basis: string | null = null; let blocked: string | null = null;
  if (automatic && image) {
    const [shop, verified, reason] = await existingShopFromImage(connection, null, image);
    if (shop && verified) { chosen = shop; basis = 'image_evidence'; selected = verified; candidates = dedupeCandidates([...candidates, verified]); if (reason) reasons.push(reason); }
    else if (reason) { blocked = reason; reasons.push(reason); }
  }
  if (!chosen && automatic && !blocked && image && (image.phone || image.address)) {
    const batch = await evidence.web(extracted); candidates = dedupeCandidates([...candidates, ...batch.candidates]);
    if (batch.unresolved_reason) reasons.push('Web検索未解決: ' + batch.unresolved_reason);
    for (const url of imageVerificationUrls(image, candidates)) candidates.push(...await evidence.structured(url));
    candidates = dedupeCandidates(candidates);
    const [candidate, reason] = structuredCandidateFromImage(image, candidates);
    const [created, collision, collisionReason] = await shops.guardCreation(extracted, candidate, candidate ? null : reason);
    if (collision) { chosen = collision; selected = candidate; basis = 'verified_collision'; if (collisionReason) reasons.push(collisionReason); }
    else if (created) { chosen = await createCandidateShop(connection, extracted, created); selected = created; basis = 'image_evidence'; reasons.push(created.verification_reason || '画像と構造化データで確定'); }
    else if (collisionReason) reasons.push(collisionReason);
  }
  const [metadata, difference] = metadataReviewState(chosen?.area ?? null, chosen?.category ?? null); const now = new Date().toISOString();
  const mention = await saveMention(connection, { ...newMention(envelope.message_id, 0, extracted, sourceUrl, 'responses_vision'), shop_id: chosen?.id ?? null, resolution_status: chosen ? 'resolved' : candidates.length ? 'ambiguous' : 'not_found', review_status: chosen ? 'approved' : 'pending', metadata_review_status: metadata, metadata_difference_type: difference, resolution_method: chosen ? 'automatic' : null, resolution_basis: basis, difference_type: chosen ? null : candidates.length ? 'image_unverified' : 'extraction_not_found', extraction_error: evidenceReviewError(assessment) || (chosen ? null : unresolved), confidence_reason: reasons.join(' / '), reviewed_at: chosen ? now : null, metadata_reviewed_at: metadata === 'approved' ? now : null });
  if (candidates.length) await storeCandidates(connection, mention, extracted, candidates, selected);
  return mention;
}
