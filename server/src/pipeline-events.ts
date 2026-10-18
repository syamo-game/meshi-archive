import { extractedMentionSchema } from './ai-models.js';
import type { ExtractedMention } from './ai-models.js';
import type { Executor } from './db/database.js';
import { eventExclusionAssessment, eventOriginQuotesMatch, evidenceReviewError } from './extraction-safety.js';
import type { EvidenceAssessment } from './extraction-safety.js';
import { fetchHtmlDocument, HtmlFetchError } from './html-fetch.js';
import type { MessageEnvelope } from './ingestion.js';
import { safeHttpUrl } from './links.js';
import { extractRestaurantPageEvidence } from './page-evidence.js';
import { candidateForNewShop, dedupeCandidates, topWebCandidate } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { extractHttpUrls } from './pipeline-discovery.js';
import { candidateVerificationUrls, PipelineEvidence } from './pipeline-evidence.js';
import { PipelineJournal } from './pipeline-journal.js';
import { PipelineShops } from './pipeline-shops.js';
import { createCandidateShop, metadataReviewState, saveMention, storeCandidates, updateMention } from './pipeline-storage.js';
import type { NewMention } from './pipeline-storage.js';
import { mentionRecord, shopRecord } from './records.js';
import type { MentionRecord, ShopRecord } from './records.js';
import { normalizeName } from './resolution.js';
import { lockShopCreation } from './shop-creation.js';
import { citationUrlIdentity } from './source-evidence.js';

export interface EventOriginResolution { extracted: ExtractedMention | null; candidates: readonly PipelineCandidate[]; shop_id: number | null; shop_version: number | null; new_candidate: PipelineCandidate | null; reason: string | null; }
const empty: EventOriginResolution = { extracted: null, candidates: [], shop_id: null, shop_version: null, new_candidate: null, reason: null };
export class PipelineEvents {
  constructor(private readonly connection: Executor, private readonly shops: PipelineShops, private readonly evidence: PipelineEvidence, private readonly journal: PipelineJournal, private readonly envelope: MessageEnvelope) {}
  async prepare(event: ExtractedMention, input: EvidenceAssessment): Promise<EventOriginResolution> {
    const origin = event.event_origin;
    if (!origin || !origin.is_unique || input.codes.length) return { ...empty, reason: '出店元の常設店舗・支店を一意に確認できません。' };
    const shops = (await this.connection.query('SELECT * FROM shops ORDER BY id', shopRecord)).filter(shop => normalizeName(shop.shop_name) === normalizeName(origin.shop_name));
    if (!origin.branch_name && (shops.length > 1 || shops.some(shop => shop.branch_name))) return { ...empty, reason: '同名の支店があり、催事の出店元支店を特定できません。' };
    const permitted = new Set([...extractHttpUrls(this.envelope.content), ...this.envelope.assets.map(asset => asset.url), ...shops.map(shop => shop.canonical_url).filter((url): url is string => Boolean(url))]);
    const texts = new Map<string, string>();
    for (const url of [origin.relation_source_url, origin.permanent_source_url]) {
      if (!url || texts.has(url)) continue;
      if (!safeHttpUrl(url) || !permitted.has(url)) return { ...empty, reason: '出店元の出典URLを元投稿・既存店舗情報から確認できません。' };
      const document = await fetchHtmlDocument(url).catch(error => { if (!(error instanceof HtmlFetchError)) throw error; this.journal.failure(this.envelope.message_id, 'event_origin_evidence', error); return null; });
      if (!document) return { ...empty, reason: '出店元の根拠ページを取得できません。' };
      if (citationUrlIdentity(document.final_url) !== citationUrlIdentity(url)) return { ...empty, reason: '出店元の根拠ページが別URLへ転送されました。' };
      texts.set(url, extractRestaurantPageEvidence(document.html, document.final_url).text_blocks.join('\n'));
    }
    if (!eventOriginQuotesMatch({ name: origin.shop_name, branch: origin.branch_name, area: origin.area, relation_evidence: origin.relation_evidence, permanent_evidence: origin.permanent_evidence, relation_text: texts.get(origin.relation_source_url ?? '') ?? this.envelope.content, permanent_text: texts.get(origin.permanent_source_url ?? '') ?? this.envelope.content })) return { ...empty, reason: '催事と出店元の関係、常設店舗・支店の引用根拠が一致しません。' };
    const extracted = extractedMentionSchema.parse({ shop_name: origin.shop_name, branch_name: origin.branch_name, area: origin.area, category: event.category, subject_kind: 'restaurant', identity_evidence: 'explicit', name_evidence: origin.permanent_evidence, branch_evidence: origin.permanent_evidence, source_url: origin.permanent_source_url, needs_review: false, confidence_reason: [...`催事の出店元常設店舗を確認。関係: ${origin.relation_evidence} / 常設店舗: ${origin.permanent_evidence} / 出典: ${origin.relation_source_url || '元投稿'} / ${origin.permanent_source_url || '元投稿'}`].slice(0, 2000).join('') });
    let [existing, blocked] = await this.shops.fromMention(extracted, null);
    if (existing) return { ...empty, extracted, shop_id: existing.id, shop_version: existing.version };
    if (blocked) return { ...empty, extracted, reason: blocked };
    let candidates = origin.permanent_source_url ? await this.evidence.structured(origin.permanent_source_url) : [];
    if (!candidates.length) {
      const batch = await this.evidence.web(extracted); candidates.push(...batch.candidates);
      for (const url of candidateVerificationUrls(extracted, batch.candidates)) candidates.push(...await this.evidence.structured(url));
    }
    candidates = dedupeCandidates(candidates); [existing, blocked] = await this.shops.fromCandidates(extracted, candidates);
    if (existing) return { ...empty, extracted, candidates, shop_id: existing.id, shop_version: existing.version };
    let candidate = blocked ? null : candidateForNewShop(extracted, candidates);
    if (!candidate && !blocked) { candidate = topWebCandidate(extracted, candidates); if (candidate) [candidate, blocked] = await this.evidence.verifyPage(extracted, candidate, candidates); }
    if (!candidate?.is_verified) return { ...empty, extracted, candidates, reason: blocked || '常設店舗を確定できる候補がありません。' };
    return { ...empty, extracted, candidates, new_candidate: candidate };
  }
  async process(event: ExtractedMention, input: EvidenceAssessment, occurrence: number, sourceUrl: string | null, extractionSource: string): Promise<MentionRecord> {
    return this.save(event, await this.prepare(event, input), occurrence, sourceUrl, extractionSource);
  }
  async save(event: ExtractedMention, prepared: EventOriginResolution, occurrence: number, sourceUrl: string | null, extractionSource: string, previous: MentionRecord | null = null): Promise<MentionRecord> {
    let result = prepared; let chosen: ShopRecord | null = null;
    if (result.extracted && (result.shop_id !== null || result.new_candidate)) {
      await lockShopCreation(this.connection);
      if (!result.extracted.branch_name) {
        const name = normalizeName(result.extracted.shop_name); const matches = (await this.connection.query('SELECT * FROM shops ORDER BY id', shopRecord)).filter(shop => normalizeName(shop.shop_name) === name);
        if (matches.length > 1 || matches.some(shop => shop.branch_name)) result = { ...empty, extracted: result.extracted, candidates: result.candidates, reason: '保存前の再確認で出店元の支店が一意に定まりません。' };
      }
    }
    let usedExisting = result.shop_id !== null; const lock = this.connection.dialect === 'postgres' ? ' FOR UPDATE' : '';
    if (result.shop_id !== null) {
      chosen = (await this.connection.query('SELECT * FROM shops WHERE id=$1' + lock, shopRecord, [result.shop_id]))[0] ?? null;
      if (!chosen || chosen.version !== result.shop_version || !(await this.connection.query("SELECT * FROM shop_mentions WHERE shop_id=$1 AND review_status='approved'" + lock, mentionRecord, [chosen.id])).length) chosen = null;
    } else if (result.new_candidate && result.extracted) {
      const [candidate, collision] = await this.shops.guardCreation(result.extracted, result.new_candidate, null);
      if (collision) { chosen = collision; usedExisting = true; }
      else if (candidate) chosen = await createCandidateShop(this.connection, result.extracted, candidate);
    }
    const excluded = chosen === null; const extracted = chosen ? result.extracted ?? event : event; const [metadata, difference] = metadataReviewState(chosen?.area ?? null, chosen?.category ?? null);
    const assessment: EvidenceAssessment = excluded ? eventExclusionAssessment(result.reason) : { codes: [], reasons: [] }; const now = new Date().toISOString();
    const row: NewMention = { message_id: this.envelope.message_id, shop_id: chosen?.id ?? null, occurrence_index: occurrence, extracted_name: extracted.shop_name, extracted_branch_name: extracted.branch_name, extracted_area: extracted.area, extracted_category: extracted.category, source_url: sourceUrl, extraction_source: extractionSource, resolution_status: excluded ? 'invalid' : 'resolved', review_status: excluded ? 'rejected' : 'approved', metadata_review_status: excluded ? 'deferred' : metadata, metadata_difference_type: excluded ? null : difference, difference_type: excluded ? 'event_excluded' : null, extraction_error: evidenceReviewError(assessment), confidence_reason: [...[extracted.confidence_reason, ...assessment.reasons].join(' / ')].slice(0, 2000).join(''), resolution_method: excluded ? null : 'automatic', resolution_basis: excluded ? null : usedExisting ? 'existing_shop' : 'verified_candidate', reviewed_at: now, metadata_reviewed_at: !excluded && metadata === 'approved' ? now : null, reused_from_mention_id: null };
    const mention = previous ? await updateMention(this.connection, { ...previous, ...row }) : await saveMention(this.connection, row);
    if (previous) await this.connection.execute('DELETE FROM resolution_candidates WHERE mention_id=$1', [previous.id]);
    if (result.candidates.length && result.extracted) await storeCandidates(this.connection, mention, result.extracted, result.candidates, chosen ? result.new_candidate : null);
    return mention;
  }
}
