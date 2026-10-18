import { z } from 'zod';
import { RestaurantAI } from './ai.js';
import type { AISettings } from './ai.js';
import { extractedMentionSchema } from './ai-models.js';
import type { ExtractedMessage, ImageAnalysisResult } from './ai-models.js';
import type { Database, Executor } from './db/database.js';
import { EvaluationChanged, evaluationSnapshot, humanReviewed, identityCandidateDiffers, identityExtractionDiffers, loadEvaluation, lockEvaluation, manualEvidenceRequired, markDifference, matchLegacyMention, strongCandidateForShop } from './evaluation-state.js';
import type { EvaluationState } from './evaluation-state.js';
import { assessMentionEvidence, evidenceReviewError, imageEvidenceAssessment, inputEvidenceAssessment, operatingStatusNote } from './extraction-safety.js';
import type { EvidenceAssessment } from './extraction-safety.js';
import type { MessageEnvelope } from './ingestion.js';
import { candidateForNewShop, dedupeCandidates } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { PipelineEvents } from './pipeline-events.js';
import type { EventOriginResolution } from './pipeline-events.js';
import { candidateVerificationUrls, PipelineEvidence } from './pipeline-evidence.js';
import { imageCandidates } from './pipeline-images.js';
import { PipelineJournal } from './pipeline-journal.js';
import { PipelineShops } from './pipeline-shops.js';
import { metadataReviewState, newMention, saveMention, storeCandidates, updateMention } from './pipeline-storage.js';
import { sourceUrls } from './pipeline.js';
import { eventExcluded, shopRecord } from './records.js';
import type { MentionRecord } from './records.js';

export interface EvaluationResult { message_id: string; matched_mentions: number; new_mentions: number; differences: number; skipped: boolean; skip_reason: string | null; }
interface Prepared { candidates: PipelineCandidate[]; image: EvidenceAssessment; event: EventOriginResolution | null; }
const emptyAssessment: EvidenceAssessment = { codes: [], reasons: [] };
function envelopeFor(state: EvaluationState): MessageEnvelope {
  return { message_id: state.message.message_id, channel_id: state.message.channel_id ?? '', content: state.message.content ?? '', created_at: state.message.source_created_at ?? '', omitted_asset_count: Math.max(0, state.assets.length - 50), assets: state.assets.map(asset => ({ kind: asset.kind, url: asset.url, title: asset.title, description: asset.description, mime_type: asset.mime_type, is_embed_preview: false })) };
}
function remove(unmatched: MentionRecord[], mention: MentionRecord): void { const index = unmatched.findIndex(row => row.id === mention.id); if (index >= 0) unmatched.splice(index, 1); }
export class MessageEvaluator {
  private readonly ai: RestaurantAI;
  constructor(private readonly database: Database, private readonly settings: AISettings, directory: string, private readonly cacheDays = 30) { this.ai = new RestaurantAI(settings, directory); }
  preflightModels(): Promise<void> { return this.ai.preflightModels(); }
  private async prepare(state: EvaluationState, extracted: ExtractedMessage, journal: PipelineJournal): Promise<Prepared[]> {
    const envelope = envelopeFor(state); const input = inputEvidenceAssessment(envelope.content, envelope.omitted_asset_count);
    const [, hint, ambiguous] = sourceUrls(envelope); const unmatched = [...state.mentions];
    const images = state.assets.filter(asset => asset.kind === 'image' && ['cdn.discordapp.com', 'media.discordapp.net', 'images-ext-1.discordapp.net', 'images-ext-2.discordapp.net'].includes(new URL(asset.url).hostname.toLowerCase())).map(asset => asset.url);
    const shops = new PipelineShops(this.database); const evidence = new PipelineEvidence(this.database, this.ai, journal, envelope.message_id);
    const events = new PipelineEvents(this.database, shops, evidence, journal, envelope);
    const prepared: Prepared[] = []; let image: ImageAnalysisResult | null = null;
    for (const mention of extracted.mentions) {
      const prior = matchLegacyMention(mention, unmatched, state.shops); if (prior) remove(unmatched, prior);
      const safety = assessMentionEvidence(mention, envelope.content, input);
      if (safety.codes.includes('event_excluded')) { prepared.push({ candidates: [], image: emptyAssessment, event: await events.prepare(mention, input) }); continue; }
      if (safety.codes.length || (prior && prior.review_status !== 'approved' && manualEvidenceRequired(prior.extraction_error))) { prepared.push({ candidates: [], image: emptyAssessment, event: null }); continue; }
      let [existing, blocked] = ambiguous ? [null, null] : await shops.fromMention(mention, extracted.mentions.length === 1 ? hint : null);
      let candidates: PipelineCandidate[] = []; let automatic: PipelineCandidate | null = null; let assessment = emptyAssessment;
      if (!existing && !blocked) {
        const urls = [...new Set([...(hint ? [hint] : []), ...envelope.assets.filter(asset => asset.kind === 'link' || asset.kind === 'embed').map(asset => asset.url)])];
        for (const url of urls.slice(0, 10)) candidates.push(...await evidence.structured(url));
        const [structuredShop] = await shops.fromCandidates(mention, candidates);
        if (!structuredShop && !candidateForNewShop(mention, candidates)) {
          const batch = await evidence.web(mention); candidates.push(...batch.candidates);
          for (const url of candidateVerificationUrls(mention, batch.candidates)) candidates.push(...await evidence.structured(url));
        }
        candidates = dedupeCandidates(candidates);
        [existing, blocked] = ambiguous ? [null, null] : await shops.fromCandidates(mention, candidates);
        if (!ambiguous && !existing && !blocked) automatic = candidateForNewShop(mention, candidates);
      }
      if (!existing && !automatic && !blocked && images.length && !ambiguous && extracted.mentions.length === 1) {
        if (!image) {
          try { image = await this.ai.analyzeImages(mention, images); journal.metrics(envelope.message_id, 'evaluation_image', image.metrics); }
          catch (error) { if (error instanceof Error) journal.failure(envelope.message_id, 'evaluation_image', error); throw error; }
        }
        assessment = imageEvidenceAssessment(image.clues.subject_kind, '', '', input);
        const first = images[0]; if (!first) throw new Error('Evaluation image URL disappeared');
        const visual = imageCandidates(image, first);
        if (assessment.codes.includes('event_excluded')) {
          const event = { ...mention, subject_kind: 'event' as const, event_origin: image.clues.event_origin }; const origin = await events.prepare(event, input);
          prepared.push({ candidates: [], image: assessment, event: { ...origin, extracted: origin.extracted ?? event, candidates: dedupeCandidates([...origin.candidates, ...visual]) } }); continue;
        }
        candidates.push(...visual);
      }
      prepared.push({ candidates: dedupeCandidates(candidates), image: assessment, event: null });
    }
    return prepared;
  }
  private async skip(id: string, reason: string, journal: PipelineJournal): Promise<EvaluationResult> {
    return this.database.transaction(async connection => {
      const present = await connection.query('SELECT message_id FROM messages WHERE message_id=$1', z.object({ message_id: z.string() }), [id]);
      if (present.length) { await journal.persist(connection); await connection.execute("INSERT INTO processing_runs(message_id,stage,status,input_tokens,output_tokens,web_search_calls,image_count,latency_ms,estimated_cost_microusd,error,created_at) VALUES($1,'evaluation_pipeline','ignored',0,0,0,0,0,0,$2,$3)", [id, reason, new Date().toISOString()]); }
      return { message_id: id, matched_mentions: 0, new_mentions: 0, differences: 0, skipped: true, skip_reason: present.length ? reason : 'message_removed' };
    });
  }
  async evaluate(id: string): Promise<EvaluationResult> {
    z.string().regex(/^\d{17,20}$/).parse(id);
    const journal = new PipelineJournal(this.settings, this.cacheDays);
    const state = await this.database.transaction(connection => loadEvaluation(connection, id)); const expected = evaluationSnapshot(state);
    if (state.mentions.length && state.mentions.every(eventExcluded)) return this.skip(id, 'event_excluded', journal);
    if (await humanReviewed(this.database, state)) return this.skip(id, 'human_reviewed', journal);
    try {
      if (state.message.fetch_error || !state.message.content) {
        return await this.database.transaction(async connection => {
          const current = await lockEvaluation(connection, id, expected);
          for (const mention of current.mentions) await updateMention(connection, markDifference(mention, 'source_unavailable'));
          return { message_id: id, matched_mentions: 0, new_mentions: 0, differences: current.mentions.length, skipped: true, skip_reason: 'source_unavailable' };
        });
      }
      const result = await this.ai.extract(state.message.content).catch(error => { if (error instanceof Error) journal.failure(id, 'evaluation_extraction', error); throw error; });
      journal.metrics(id, 'evaluation_extraction', result.metrics); let extracted = result.message;
      if (!extracted.mentions.length && imageEvidenceAssessment('unknown', state.message.content, extracted.unresolved_reason || extracted.ignore_reason || '').codes.includes('event_excluded')) extracted = { ...extracted, is_restaurant_message: true, mentions: [extractedMentionSchema.parse({ shop_name: '催事（出店元未特定）', needs_review: true, subject_kind: 'event', confidence_reason: extracted.unresolved_reason || extracted.ignore_reason || '催事の紹介' })] };
      const protectedShop = state.mentions.some(row => row.shop_id !== null || row.review_status === 'approved');
      if (protectedShop && extracted.mentions.some(mention => assessMentionEvidence(mention, state.message.content ?? '').codes.includes('event_excluded'))) return this.skip(id, 'event_reassessment_preserved', journal);
      const prepared = extracted.is_restaurant_message ? await this.prepare(state, extracted, journal) : [];
      if (protectedShop && prepared.some(item => item.event)) return this.skip(id, 'event_reassessment_preserved', journal);
      return await this.database.transaction(async connection => {
        const current = await lockEvaluation(connection, id, expected);
        const outcome = await this.apply(connection, current, extracted, prepared, journal);
        await connection.execute("UPDATE messages SET processing_status='succeeded',processed_at=$1 WHERE message_id=$2", [new Date().toISOString(), id]);
        await journal.persist(connection); return outcome;
      });
    } catch (error) {
      if (error instanceof EvaluationChanged) return this.skip(id, error.message, journal);
      console.error('Message evaluation failed', { messageId: id, error });
      journal.failure(id, 'evaluation_pipeline', error instanceof Error ? error : new Error(String(error)));
      try {
        await this.database.transaction(async connection => { await lockEvaluation(connection, id, expected); await connection.execute("UPDATE messages SET processing_status='failed' WHERE message_id=$1", [id]); await journal.persist(connection); });
      } catch (failure) {
        if (failure instanceof EvaluationChanged) return this.skip(id, failure.message, journal);
        throw new AggregateError([error, failure], `Evaluation and journal persistence failed: message_id=${id}`);
      }
      throw error;
    }
  }
  private async apply(connection: Executor, state: EvaluationState, extracted: ExtractedMessage, prepared: readonly Prepared[], journal: PipelineJournal): Promise<EvaluationResult> {
    const envelope = envelopeFor(state); const id = envelope.message_id; const input = inputEvidenceAssessment(envelope.content, envelope.omitted_asset_count);
    const outcome: EvaluationResult = { message_id: id, matched_mentions: 0, new_mentions: 0, differences: 0, skipped: false, skip_reason: null };
    if (!extracted.is_restaurant_message) {
      for (const mention of state.mentions) if (!eventExcluded(mention)) await updateMention(connection, { ...markDifference(mention, 'new_pipeline_not_restaurant'), ...(input.codes.length ? { extraction_error: evidenceReviewError(input), confidence_reason: input.reasons.join(' / ') } : {}) });
      return { ...outcome, differences: state.mentions.length };
    }
    if (prepared.length !== extracted.mentions.length) throw new Error(`Evaluation candidate count differs: message_id=${id}`);
    const unmatched = [...state.mentions]; let next = Math.max(-1, ...unmatched.map(row => row.occurrence_index)) + 1;
    const [source, hint, ambiguous] = sourceUrls(envelope); const shops = new PipelineShops(connection);
    const events = new PipelineEvents(connection, shops, new PipelineEvidence(connection, this.ai, journal, id), journal, envelope);
    for (const [index, raw] of extracted.mentions.entries()) {
      const batch = prepared[index]; if (!batch) throw new Error('Prepared evaluation candidate is missing');
      const priorEvent = unmatched.find(row => row.shop_id === null && row.extracted_name === raw.shop_name && row.extracted_branch_name === raw.branch_name);
      if (priorEvent && eventExcluded(priorEvent)) { remove(unmatched, priorEvent); continue; }
      if (batch.event) {
        if (priorEvent) { remove(unmatched, priorEvent); outcome.matched_mentions++; } else outcome.new_mentions++;
        const saved = await events.save(raw, batch.event, priorEvent?.occurrence_index ?? next++, source, 'responses_structured', priorEvent ?? null);
        if (saved.shop_id !== null) outcome.differences++; continue;
      }
      const note = operatingStatusNote(raw); const item = note ? { ...raw, confidence_reason: raw.confidence_reason + ' / ' + note } : raw;
      let mention = matchLegacyMention(item, unmatched, state.shops);
      if (mention) { remove(unmatched, mention); outcome.matched_mentions++; }
      else { mention = await saveMention(connection, { ...newMention(id, next++, item, source, 'responses_structured'), difference_type: 'new_pipeline_mention' }); outcome.new_mentions++; outcome.differences++; }
      const safety = assessMentionEvidence(item, envelope.content, input);
      const assessment: EvidenceAssessment = { codes: [...new Set([...safety.codes, ...batch.image.codes])], reasons: [...new Set([...safety.reasons, ...batch.image.reasons])] };
      if (assessment.codes.length || (mention.review_status !== 'approved' && manualEvidenceRequired(mention.extraction_error))) {
        const updated = await updateMention(connection, { ...markDifference(mention, 'evidence_review'), resolution_status: 'ambiguous', extraction_error: evidenceReviewError(assessment) ?? mention.extraction_error, confidence_reason: [item.confidence_reason, ...assessment.reasons].join(' / ') });
        if (batch.candidates.length) {
          const highest = (await connection.query('SELECT COALESCE(MAX(rank),0) AS rank FROM resolution_candidates WHERE mention_id=$1', z.object({ rank: z.number() }), [mention.id]))[0];
          await storeCandidates(connection, updated, item, batch.candidates, null, highest?.rank ?? 0);
        }
        outcome.differences++; continue;
      }
      let [existing, blocked] = ambiguous ? [null, null] : await shops.fromMention(item, extracted.mentions.length === 1 ? hint : null);
      let automatic: PipelineCandidate | null = null; let collision: PipelineCandidate | null = null;
      if (!existing && !blocked) {
        [existing, blocked] = ambiguous ? [null, null] : await shops.fromCandidates(item, batch.candidates);
        automatic = ambiguous || existing || blocked ? null : candidateForNewShop(item, batch.candidates);
        const before = automatic; const [candidate, collided] = await shops.guardCreation(item, automatic, blocked); automatic = candidate;
        if (collided) { existing = collided; collision = before; }
      }
      if (existing) {
        const locked = (await connection.query('SELECT * FROM shops WHERE id=$1' + (connection.dialect === 'postgres' ? ' FOR UPDATE' : ''), shopRecord, [existing.id]))[0];
        if (!locked || locked.version !== existing.version) throw new EvaluationChanged('concurrent_change');
      }
      mention = { ...mention, extracted_name: item.shop_name, extracted_branch_name: item.branch_name, extracted_area: item.area, extracted_category: item.category, source_url: source || mention.source_url, extraction_source: 'responses_structured', confidence_reason: item.confidence_reason };
      await connection.execute('DELETE FROM resolution_candidates WHERE mention_id=$1', [mention.id]);
      await storeCandidates(connection, mention, item, batch.candidates, automatic || collision);
      if (mention.shop_id === null && existing && collision) {
        const [metadata, difference] = metadataReviewState(existing.area, existing.category); const now = new Date().toISOString();
        mention = { ...mention, shop_id: existing.id, resolution_status: 'resolved', review_status: 'approved', resolution_method: 'automatic', resolution_basis: 'verified_collision', reviewed_at: now, metadata_review_status: metadata, metadata_difference_type: difference, metadata_reviewed_at: metadata === 'approved' ? now : null };
      } else if (mention.shop_id !== null) {
        const shopId = mention.shop_id; const shop = state.shops.find(row => row.id === shopId); if (!shop) throw new EvaluationChanged('concurrent_change');
        const proposal = automatic || strongCandidateForShop(shop, batch.candidates);
        const differs = identityExtractionDiffers(shop, item) || Boolean(existing && existing.id !== shop.id) || identityCandidateDiffers(shop, proposal);
        if (differs) { mention = markDifference(mention, 'new_pipeline_difference'); outcome.differences++; }
        const [metadata, difference] = metadataReviewState(shop.area, shop.category);
        if ((item.category && item.category !== shop.category) || (proposal?.category && proposal.category !== shop.category) || metadata === 'pending') {
          mention = { ...mention, metadata_review_status: 'pending', metadata_difference_type: difference || 'new_pipeline_metadata_difference' }; if (!differs) outcome.differences++;
        }
      }
      await updateMention(connection, mention);
    }
    for (const mention of unmatched) if (!eventExcluded(mention)) { await updateMention(connection, markDifference(mention, 'new_pipeline_missing_mention')); outcome.differences++; }
    return outcome;
  }
}
