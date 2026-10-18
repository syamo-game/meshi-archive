import { z } from 'zod';
import { RestaurantAI } from './ai.js';
import type { AISettings } from './ai.js';
import { extractedMentionSchema, requireExtractionEvidence } from './ai-models.js';
import type { ExtractedMention, ExtractedMessage, ImageAnalysisResult } from './ai-models.js';
import { candidateMatchesArea } from './candidate-geography.js';
import type { Database, Executor } from './db/database.js';
import { assessMentionEvidence, evidenceReviewError, imageEvidenceAssessment, inputEvidenceAssessment, operatingStatusNote } from './extraction-safety.js';
import { canonicalArea } from './geography.js';
import { ImageCache } from './image-cache.js';
import { envelopeSchema } from './ingestion.js';
import type { MessageEnvelope, MessageProcessor, ProcessResult } from './ingestion.js';
import { candidateForNewShop, dedupeCandidates, replaceWebCandidate, structuredBlockReason, topWebCandidate } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { discoveryEvidence, SourceDiscovery } from './pipeline-discovery.js';
import type { DiscoveryEvidence } from './pipeline-discovery.js';
import { PipelineEvents } from './pipeline-events.js';
import { candidateVerificationUrls, PipelineEvidence } from './pipeline-evidence.js';
import { analyzeImage, processImageOnly } from './pipeline-image-processing.js';
import { existingShopFromImage, imageCandidates, imageMatchesMention, imageVerificationUrls, singleImageCandidate, structuredCandidateFromImage } from './pipeline-images.js';
import { PipelineJournal } from './pipeline-journal.js';
import { PipelineShops } from './pipeline-shops.js';
import { findSourceReuse } from './pipeline-source-reuse.js';
import { createCandidateShop, metadataReviewState, newMention, replaceSourceAssets, saveMention, storeCandidates } from './pipeline-storage.js';
import { mentionRecord, messageRecord } from './records.js';
import type { ShopRecord } from './records.js';
import { normalizeExternalIdentity, normalizeUrlIdentity } from './resolution.js';
import { externalIdentity } from './shop-creation.js';
import { identifySourceUrl } from './source-identity.js';

export interface ProcessingOptions { allowSourceDiscovery?: boolean; allowImageFallback?: boolean; fallbackMentions?: readonly ExtractedMention[]; }
export function sourceUrls(envelope: MessageEnvelope): readonly [string | null, string | null, boolean] {
  let source: string | null = null; const canonical = new Map<string, string>();
  for (const asset of envelope.assets) {
    if (asset.kind !== 'link' && asset.kind !== 'embed') continue;
    const url = new URL(asset.url); const host = url.hostname.toLowerCase(); const path = url.pathname.replace(/\/+$/, ''); const domain = (value: string): boolean => host === value || host.endsWith('.' + value);
    const isCanonical = domain('tabelog.com') ? /\/[0-9]{6,}(?:\/|$)/.test(path + '/') : domain('hotpepper.jp') ? path.includes('/str') : domain('gnavi.co.jp') ? Boolean(path) : domain('ikyu.com') ? path.includes('/restaurant/') : domain('retty.me') && path.includes('/area/') && path.includes('/stores/');
    if (!isCanonical) { source ??= asset.url; continue; }
    const external = externalIdentity(asset.url); const identity = external ? normalizeExternalIdentity(...external) : null;
    const key = identity ? `external:${identity[0]}:${identity[1]}` : 'url:' + normalizeUrlIdentity(asset.url);
    if (!canonical.has(key)) canonical.set(key, asset.url);
  }
  return [source, canonical.size === 1 ? [...canonical.values()][0] ?? null : null, canonical.size > 1];
}
export class RestaurantProcessor implements MessageProcessor {
  private readonly ai: RestaurantAI;
  private readonly images: ImageCache;
  constructor(private readonly database: Database, private readonly settings: AISettings, directory: string, private readonly cacheDays = 30) { this.ai = new RestaurantAI(settings, directory); this.images = new ImageCache(directory); z.number().int().min(1).parse(cacheDays); }
  preflightModels(): Promise<void> { return this.ai.preflightModels(); }
  async cacheImage(url: string): Promise<void> { await this.images.original(url); }
  async processMessage(input: MessageEnvelope, options: ProcessingOptions = {}): Promise<ProcessResult> {
    const envelope = envelopeSchema.parse(input); const journal = new PipelineJournal(this.settings, this.cacheDays);
    try {
      return await this.database.transaction(async connection => {
        const result = await this.process(connection, journal, envelope, options); await journal.persist(connection); return result;
      });
    } catch (error) {
      console.error('Message pipeline failed', { messageId: envelope.message_id, error });
      const failure = error instanceof Error ? error : new Error(String(error)); journal.failure(envelope.message_id, 'message_pipeline', failure);
      try {
        await this.database.transaction(async connection => {
          await connection.execute("INSERT INTO messages(message_id,channel_id,content,source_created_at,is_target,processing_status,fetch_error) VALUES($1,$2,$3,$4,$5,'failed',$6) ON CONFLICT(message_id) DO UPDATE SET processing_status='failed',fetch_error=excluded.fetch_error", [envelope.message_id, envelope.channel_id, envelope.content, envelope.created_at, true, `${failure.name}: ${failure.message}`]);
          await replaceSourceAssets(connection, envelope); await journal.persist(connection);
        });
      } catch (persistenceError) { console.error('Failed to preserve pipeline operational records', { messageId: envelope.message_id, persistenceError }); throw new AggregateError([failure, persistenceError], `Message processing and operational journal persistence failed: message_id=${envelope.message_id}`); }
      throw error;
    }
  }
  private async finish(connection: Executor, id: string, ignored = false): Promise<ProcessResult> {
    await connection.execute('UPDATE messages SET processing_status=$1,is_target=$2,processed_at=$3 WHERE message_id=$4', [ignored ? 'ignored' : 'succeeded', !ignored, new Date().toISOString(), id]);
    const mentions = await connection.query('SELECT * FROM shop_mentions WHERE message_id=$1 ORDER BY occurrence_index', mentionRecord, [id]);
    return { message_id: id, shop_ids: mentions.map(mention => mention.shop_id).filter((shop): shop is number => shop !== null), pending_mention_ids: mentions.filter(mention => mention.review_status === 'pending').map(mention => mention.id), ignored };
  }
  private async process(connection: Executor, journal: PipelineJournal, envelope: MessageEnvelope, options: ProcessingOptions): Promise<ProcessResult> {
    const id = envelope.message_id; const lock = connection.dialect === 'postgres' ? ' FOR UPDATE' : '';
    await connection.execute("INSERT INTO messages(message_id,channel_id,content,source_created_at,is_target,processing_status) VALUES($1,$2,$3,$4,$5,'pending') ON CONFLICT(message_id) DO NOTHING", [id, envelope.channel_id, envelope.content, envelope.created_at, true]);
    const existing = (await connection.query('SELECT * FROM messages WHERE message_id=$1' + lock, messageRecord, [id]))[0];
    if (!existing) throw new Error(`Message insertion returned no row: message_id=${id}`);
    if (existing.processing_status === 'succeeded' || existing.processing_status === 'ignored') {
      const mentions = await connection.query('SELECT * FROM shop_mentions WHERE message_id=$1 ORDER BY occurrence_index', mentionRecord, [id]);
      return { message_id: id, shop_ids: mentions.map(row => row.shop_id).filter((shop): shop is number => shop !== null), pending_mention_ids: mentions.filter(row => row.review_status === 'pending').map(row => row.id), ignored: existing.processing_status === 'ignored' };
    }
    await connection.execute("UPDATE messages SET channel_id=$1,content=$2,source_created_at=$3,processing_status='processing',fetch_error=NULL WHERE message_id=$4", [envelope.channel_id, envelope.content, envelope.created_at, id]);
    await connection.execute('DELETE FROM shop_mentions WHERE message_id=$1', [id]); await replaceSourceAssets(connection, envelope);
    const reuse = await findSourceReuse(connection, envelope);
    if (reuse) {
      const extracted = extractedMentionSchema.parse({ shop_name: reuse.mention.extracted_name, branch_name: reuse.mention.extracted_branch_name, area: reuse.mention.extracted_area, category: reuse.mention.extracted_category, needs_review: false, confidence_reason: `source_service=${reuse.identity.service}; source_item_id=${reuse.identity.item_id}; reused_from_mention_id=${reuse.mention.id}` });
      const url = envelope.assets.find(asset => (asset.kind === 'link' || asset.kind === 'embed') && identifySourceUrl(asset.url)?.normalized_url === reuse.identity.normalized_url)?.url ?? reuse.identity.normalized_url;
      const [metadata, difference] = metadataReviewState(reuse.shop.area, reuse.shop.category); const now = new Date().toISOString();
      await saveMention(connection, { ...newMention(id, 0, extracted, url, 'source_identity_reuse'), shop_id: reuse.shop.id, resolution_status: 'resolved', review_status: 'approved', metadata_review_status: metadata, metadata_difference_type: difference, resolution_method: 'automatic', resolution_basis: 'source_reuse', reused_from_mention_id: reuse.mention.id, difference_type: null, reviewed_at: now, metadata_reviewed_at: metadata === 'approved' ? now : null });
      return this.finish(connection, id);
    }
    const extraction = await this.ai.extract(envelope.content).catch(error => { if (error instanceof Error) journal.failure(id, 'message_extraction', error); throw error; }); journal.metrics(id, 'message_extraction', extraction.metrics);
    const fallback = options.fallbackMentions ?? []; let discovery: DiscoveryEvidence | null = null; let discovered: ExtractedMessage | null = null;
    if (options.allowSourceDiscovery && !extraction.message.mentions.length && !fallback.length) { discovery = discoveryEvidence(envelope); if (discovery) discovered = await new SourceDiscovery(connection, this.ai, journal, id).discover(discovery); }
    const input = inputEvidenceAssessment(envelope.content, Math.max(envelope.omitted_asset_count, envelope.assets.length - 50), Boolean(discovery?.input_truncated));
    const usingFallback = !extraction.message.mentions.length && fallback.length > 0; const usingDiscovery = Boolean(discovered?.mentions.length); const untrusted = usingFallback || usingDiscovery;
    const context = extraction.message.unresolved_reason || extraction.message.ignore_reason || '新規抽出で店舗名を特定できませんでした。';
    const fallbackMentions = usingFallback ? requireExtractionEvidence({ is_restaurant_message: true, mentions: [...fallback], ignore_reason: null, unresolved_reason: null }).mentions.map(mention => { const fresh = [...`新規抽出結果: ${context}`].slice(0, 1000).join(''); const legacy = [...mention.confidence_reason].slice(0, Math.max(0, 2000 - [...fresh].length - 3)).join(''); return { ...mention, needs_review: true, confidence_reason: fresh + (legacy ? ' / ' + legacy : '') }; }) : [];
    const mentions = extraction.message.mentions.length ? extraction.message.mentions : fallbackMentions.length ? fallbackMentions : (discovered?.mentions ?? []).map(mention => ({ ...mention, needs_review: true }));
    const eventContext = imageEvidenceAssessment('unknown', envelope.content, extraction.message.unresolved_reason || extraction.message.ignore_reason || '').codes.includes('event_excluded');
    if (!extraction.message.is_restaurant_message && !discovered?.is_restaurant_message && !mentions.length && !input.codes.length && !eventContext) return this.finish(connection, id, true);
    const imageUrls = options.allowImageFallback !== false && !discovered ? [...new Set(envelope.assets.filter(asset => asset.kind === 'image' && ['cdn.discordapp.com', 'media.discordapp.net', 'images-ext-1.discordapp.net', 'images-ext-2.discordapp.net'].includes(new URL(asset.url).hostname.toLowerCase())).map(asset => asset.url))] : [];
    const [source, canonicalHint, ambiguousAssets] = sourceUrls(envelope); const evidence = new PipelineEvidence(connection, this.ai, journal, id); const shops = new PipelineShops(connection); const events = new PipelineEvents(connection, shops, evidence, journal, envelope);
    if (!mentions.length) {
      const reasons = [discovered?.unresolved_reason || extraction.message.unresolved_reason || '飲食店への言及ですが、投稿から店舗名を特定できませんでした。', ...input.reasons];
      if (ambiguousAssets) reasons.push('複数の店舗ID付きURLと投稿内店舗の対応を一意に確認できない'); const reason = reasons.join(' / ');
      if (imageUrls.length) await processImageOnly(connection, this.ai, journal, envelope, imageUrls, source, reason, !ambiguousAssets, input);
      else {
        const mention = extractedMentionSchema.parse({ shop_name: '（店舗名未特定）', needs_review: true, confidence_reason: [...reason].slice(0, 2000).join(''), subject_kind: eventContext ? 'event' : null });
        if (eventContext) await events.process(mention, input, 0, source, 'responses_structured');
        else await saveMention(connection, { ...newMention(id, 0, mention, source, discovered ? 'responses_source_discovery' : 'responses_structured'), resolution_status: 'not_found', metadata_difference_type: null, difference_type: 'extraction_not_found', extraction_error: evidenceReviewError(input) || reason, confidence_reason: reason });
      }
      return this.finish(connection, id);
    }
    let imageResult: ImageAnalysisResult | null = null;
    for (const [occurrence, original] of mentions.entries()) {
      const note = operatingStatusNote(original); const extracted = note ? { ...original, confidence_reason: original.confidence_reason + ' / ' + note } : original;
      const assessment = assessMentionEvidence(extracted, envelope.content, input, usingDiscovery && extracted.source_url !== null); const url = usingDiscovery ? extracted.source_url : source; const extractionSource = usingFallback ? 'legacy_hint' : usingDiscovery ? 'responses_source_discovery' : 'responses_structured';
      if (assessment.codes.includes('event_excluded')) { await events.process(extracted, input, occurrence, url, usingDiscovery ? 'responses_source_discovery' : 'responses_structured'); continue; }
      if (assessment.codes.length) { await saveMention(connection, { ...newMention(id, occurrence, extracted, url, extractionSource), difference_type: 'evidence_review', extraction_error: evidenceReviewError(assessment), confidence_reason: [...[extracted.confidence_reason, ...assessment.reasons].join(' / ')].slice(0, 2000).join(''), metadata_reviewed_at: null }); continue; }
      const automatic = !ambiguousAssets && (!usingDiscovery || extracted.source_url !== null);
      let [chosen, reason] = untrusted || !automatic ? [null, null] as [ShopRecord | null, string | null] : await shops.fromMention(extracted, mentions.length === 1 ? canonicalHint : null);
      let blocked = chosen ? null : reason; let basis: string | null = chosen ? 'existing_shop' : null; let candidate: PipelineCandidate | null = null; let selected: PipelineCandidate | null = null; let candidates: PipelineCandidate[] = []; let searchReason: string | null = null;
      const resolveCandidates = async (): Promise<void> => {
        const structuredBlocked = structuredBlockReason(extracted, candidates);
        if (automatic && structuredBlocked) { blocked = structuredBlocked; return; }
        if (usingDiscovery || !automatic) return;
        const [shop, matchReason] = await shops.fromCandidates(extracted, candidates); chosen = shop; if (shop) basis = 'existing_shop'; reason = matchReason || reason; blocked = shop ? null : matchReason;
        candidate = shop || blocked ? null : candidateForNewShop(extracted, candidates); const before = candidate;
        const [created, collision, collisionReason] = await shops.guardCreation(extracted, candidate, blocked); candidate = created;
        if (collision) { chosen = collision; reason = collisionReason; selected = before; basis = 'verified_collision'; blocked = null; } else blocked = collisionReason;
      };
      if (!chosen && !candidate && !blocked) {
        const urls = [...new Set([...(canonicalHint ? [canonicalHint] : []), ...envelope.assets.filter(asset => asset.kind === 'link' || asset.kind === 'embed').map(asset => asset.url)])];
        for (const target of urls.slice(0, 10)) candidates.push(...await evidence.structured(target)); candidates = dedupeCandidates(candidates); await resolveCandidates();
      }
      if (!chosen && !candidate && !blocked) {
        const batch = await evidence.web(extracted); candidates = dedupeCandidates([...candidates, ...batch.candidates]); searchReason = batch.unresolved_reason;
        for (const target of candidateVerificationUrls(extracted, batch.candidates)) candidates.push(...await evidence.structured(target)); candidates = dedupeCandidates(candidates); await resolveCandidates();
      }
      let pageBlocked: string | null = null;
      if (!chosen && !candidate && !blocked && automatic) {
        const excluded = usingDiscovery ? discovery?.source_urls ?? [] : []; candidate = topWebCandidate(extracted, candidates, usingDiscovery, excluded);
        if (candidate) [candidate, pageBlocked] = await evidence.verifyPage(extracted, candidate, candidates, excluded);
        if (usingDiscovery && candidate) {
          const [shop, matchReason] = await shops.afterConfirmation(extracted, candidate);
          if (shop) { chosen = shop; reason = matchReason; selected = candidate; basis = 'verified_candidate'; }
          else if (matchReason) { candidate = null; blocked = matchReason; }
        }
        if (!chosen && candidate) { const before = candidate; const [created, collision, collisionReason] = await shops.guardCreation(extracted, candidate, blocked); candidate = created; if (collision) { chosen = collision; reason = collisionReason; selected = before; basis = 'verified_collision'; blocked = null; } else if (collisionReason) blocked = collisionReason; }
      }
      let imageReason: string | null = null; let imageAssessment = { codes: [] as ReturnType<typeof imageEvidenceAssessment>['codes'], reasons: [] as readonly string[] };
      if (!chosen && !candidate && !blocked && imageUrls.length && mentions.length === 1 && !untrusted && automatic) {
        imageResult ??= await analyzeImage(this.ai, journal, id, imageUrls); imageAssessment = imageEvidenceAssessment(imageResult.clues.subject_kind);
        if (imageAssessment.codes.includes('event_excluded')) { await events.process({ ...extracted, subject_kind: 'event', event_origin: imageResult.clues.event_origin }, input, occurrence, source, 'responses_vision'); continue; }
        const imageAutomatic = imageUrls.length === 1 && !imageAssessment.codes.length; imageReason = [imageResult.clues.reason, ...imageAssessment.reasons, ...(imageUrls.length !== 1 ? ['複数画像の対応関係を検証できないため自動確定しない'] : [])].join(' / ');
        const firstImage = imageUrls[0]; if (!firstImage) throw new Error('Image URL disappeared during processing'); candidates = dedupeCandidates([...candidates, ...imageCandidates(imageResult, firstImage)]); const image = singleImageCandidate(candidates);
        if (imageAutomatic && image) { const [shop, verified, matchReason] = await existingShopFromImage(connection, extracted, image); if (shop && verified) { chosen = shop; basis = 'image_evidence'; reason = matchReason || reason; selected = verified; candidates = dedupeCandidates([...candidates, verified]); } else if (matchReason) blocked = matchReason; }
        if (!chosen && imageAutomatic && !blocked && image && imageMatchesMention(extracted, image)) {
          for (const target of imageVerificationUrls(image, candidates)) candidates.push(...await evidence.structured(target)); candidates = dedupeCandidates(candidates);
          const [shop, matchReason] = await shops.fromCandidates(extracted, candidates); chosen = shop; if (shop) basis = 'image_evidence';
          if (!chosen) { let imageCandidateReason: string | null; [candidate, imageCandidateReason] = structuredCandidateFromImage(image, candidates); const area = canonicalArea(extracted.area); if (candidate && area && !candidateMatchesArea(area, candidate)) { candidate = null; imageCandidateReason = '画像と構造化候補の地域が投稿エリアと一致しない'; } if (imageCandidateReason) { if (!candidate) blocked = imageCandidateReason; else imageReason += ' / ' + imageCandidateReason; } }
          reason = matchReason || reason; if (matchReason && !chosen) blocked = matchReason;
        }
        const before = candidate; const [created, collision, collisionReason] = await shops.guardCreation(extracted, candidate, blocked); candidate = created; if (collision) { chosen = collision; reason = collisionReason; selected = before; basis = 'verified_collision'; blocked = null; } else blocked = collisionReason;
      }
      if (!chosen && !candidate && !blocked && pageBlocked) blocked = pageBlocked;
      if (!chosen && candidate) { chosen = await createCandidateShop(connection, extracted, candidate); basis = candidate.provenance === 'image' ? 'image_evidence' : 'verified_candidate'; }
      const [metadata, difference] = metadataReviewState(chosen ? chosen.area : extracted.area, chosen ? chosen.category : extracted.category); const reasons = [extracted.confidence_reason];
      if (ambiguousAssets) reasons.push('複数の店舗ID付きURLと投稿内店舗の対応を一意に確認できない'); reason = blocked || reason; if (reason) reasons.push(reason);
      if (candidate) { reasons.push(candidate.verification_reason || '検証済み候補で確定'); candidates = candidate.provenance === 'web_search' && candidate.is_verified ? replaceWebCandidate(candidates, candidate) : dedupeCandidates([candidate, ...candidates]); }
      if (searchReason) reasons.push('Web検索未解決: ' + searchReason); if (imageReason) reasons.push('画像補助: ' + imageReason);
      if (difference === 'unknown_category') reasons.push('未知カテゴリ: ' + (chosen ? chosen.category : extracted.category)); if (difference === 'unknown_area') reasons.push('未知エリア: ' + (chosen ? chosen.area : extracted.area));
      const now = new Date().toISOString(); const row = await saveMention(connection, { ...newMention(id, occurrence, extracted, url, extractionSource), shop_id: chosen?.id ?? null, resolution_status: chosen ? 'resolved' : 'ambiguous', review_status: chosen ? 'approved' : 'pending', metadata_review_status: metadata, metadata_difference_type: difference, resolution_method: chosen ? 'automatic' : null, resolution_basis: basis, difference_type: imageAssessment.codes.length ? 'evidence_review' : chosen ? null : 'new_ambiguous', extraction_error: evidenceReviewError(imageAssessment), confidence_reason: reasons.join(' / '), reviewed_at: chosen ? now : null, metadata_reviewed_at: metadata === 'approved' ? now : null });
      await storeCandidates(connection, row, extracted, candidates, candidate || selected);
    }
    return this.finish(connection, id);
  }
}
