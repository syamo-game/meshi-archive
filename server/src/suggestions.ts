import { stat } from 'node:fs/promises';
import { isIP } from 'node:net';
import type { Hono } from 'hono';
import { z } from 'zod';
import type { Suggestion } from '@meshi/contracts';
import type { WebEnvironment } from './app.js';
import { readAISettings, RestaurantAI } from './ai.js';
import { extractedMentionSchema } from './ai-models.js';
import type { ExtractedMention } from './ai-models.js';
import { requireAdmin, requireWrite } from './auth.js';
import type { Config } from './config.js';
import type { Database } from './db/database.js';
import { AppError } from './errors.js';
import { ImageCache, publicAddress, sourceImagePath } from './image-cache.js';
import { imageHosts } from './images.js';
import { safeHttpUrl } from './links.js';
import { assetRecord, mentionRecord, messageRecord, shopRecord } from './records.js';
import { sanitizeCitedCandidate } from './source-evidence.js';

export function validateReferenceUrl(value: string | null): string | null {
  if (!value?.trim()) return null;
  const safe = safeHttpUrl(value); if (!safe) throw new AppError(422, '参考URLは公開されたHTTP/HTTPSのURLで指定してください。');
  const url = new URL(safe); const host = url.hostname.replace(/^\[|\]$/g, '');
  if ((url.port && !['80', '443'].includes(url.port)) || host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') || !host.includes('.')) throw new AppError(422, '参考URLは公開されたHTTP/HTTPSのURLで指定してください。');
  if (isIP(host) && !publicAddress(host)) throw new AppError(422, '参考URLに内部ネットワークのアドレスは使用できません。'); return safe;
}
const referenceUrl = z.string().trim().max(2048).nullable().transform(validateReferenceUrl);
const draftSchema = z.strictObject({ shop_name: z.string().trim().max(500).nullable().optional(), branch_name: z.string().trim().max(255).nullable().optional(), area: z.string().trim().max(255).nullable().optional(), category: z.string().trim().max(255).nullable().optional(), address: z.string().trim().max(2000).nullable().optional(), phone: z.string().trim().max(64).nullable().optional(), canonical_url: referenceUrl.optional() });
const draftFields = ['shop_name', 'branch_name', 'area', 'category', 'address', 'phone', 'canonical_url'] as const;
type CompleteDraft = Record<typeof draftFields[number], string | null>;
export const suggestionRequest = z.strictObject({ expected_version: z.number().int().positive(), shop_version: z.number().int().positive().nullable().default(null), draft: draftSchema.default({}), supplement: z.string().trim().max(4000).default(''), reference_url: referenceUrl.default(null) });
type SuggestionRequest = z.infer<typeof suggestionRequest>;
interface Context { mention_id: number; version: number; shop_id: number | null; shop_version: number | null; content: string; draft: CompleteDraft; image_sources: readonly string[]; }
interface Result { mention_id: number; expected_version: number; shop_version: number | null; candidates: readonly Suggestion[]; unresolved_reason: string | null; }
export class ReviewSuggestions {
  private readonly active = new Set<number>();
  private readonly images: ImageCache;
  constructor(private readonly database: Database, private readonly config: Config, private readonly ai: RestaurantAI) { this.images = new ImageCache(config.imageDirectory); }
  private async context(id: number, request: SuggestionRequest): Promise<Context> {
    const mention = (await this.database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [id]))[0];
    if (!mention) throw new AppError(404, '確認項目が見つかりません。一覧を更新してください。');
    if (mention.version !== request.expected_version) throw new AppError(409, '確認項目が変更されています。最新情報を読み込んでください。');
    if (mention.review_status === 'rejected') throw new AppError(409, 'この項目は対象外です。AI補完は実行できません。');
    const shop = mention.shop_id !== null ? (await this.database.query('SELECT * FROM shops WHERE id=$1', shopRecord, [mention.shop_id]))[0] : null;
    if ((shop?.version ?? null) !== request.shop_version) throw new AppError(409, '店舗情報が変更されています。最新情報を読み込んでください。');
    const source: CompleteDraft = { shop_name: shop ? shop.shop_name : mention.extracted_name, branch_name: shop ? shop.branch_name : mention.extracted_branch_name, area: shop ? shop.area : mention.extracted_area, category: shop ? shop.category : mention.extracted_category, address: shop?.address ?? null, phone: shop?.phone ?? null, canonical_url: shop?.canonical_url ?? null };
    for (const field of draftFields) { const value = request.draft[field]; if (value !== undefined) source[field] = value; }
    let draft: CompleteDraft;
    try { const checked = draftSchema.parse(source); draft = { ...source }; for (const field of draftFields) { const value = checked[field]; if (value !== undefined) draft[field] = value; } }
    catch (error) { console.warn('Stored suggestion input is invalid', { mentionId: id, error }); throw new AppError(400, '保存された店舗情報に補完へ送れない値があります。入力欄を確認・修正してください。'); }
    const message = (await this.database.query('SELECT * FROM messages WHERE message_id=$1', messageRecord, [mention.message_id]))[0]; if (!message) throw new Error(`Suggestion message is missing: mention_id=${id}`);
    const assets = await this.database.query("SELECT * FROM source_assets WHERE message_id=$1 AND kind='image' AND fetch_status<>'unavailable' ORDER BY id", assetRecord, [mention.message_id]);
    const images: string[] = [];
    for (const asset of assets) {
      if (!imageHosts.has(new URL(asset.url).hostname.toLowerCase().replace(/\.$/, ''))) continue;
      try { if ((await stat(sourceImagePath(asset.url, this.config.imageDirectory))).isFile()) images.push(asset.url); }
      catch (error) { if (!(error instanceof Error && 'code' in error && error.code === 'ENOENT')) throw error; }
      if (images.length === 2) break;
    }
    return { mention_id: id, version: mention.version, shop_id: mention.shop_id, shop_version: shop?.version ?? null, content: message.content ?? '', draft, image_sources: images };
  }
  private async search(context: Context, request: SuggestionRequest, signal: AbortSignal): Promise<Result> {
    const images = await this.images.aiImages(context.image_sources); const draft = context.draft; const name = draft.shop_name?.trim() ?? '';
    const result = (candidates: readonly Suggestion[], reason: string | null): Result => ({ mention_id: context.mention_id, expected_version: context.version, shop_version: context.shop_version, candidates, unresolved_reason: reason });
    let mention: ExtractedMention;
    if (!name || name === '（店舗名未特定）') {
      if (context.content.length > 16000) throw new AppError(400, '元投稿が長いため、対象の店名を入力してからAI補完してください。');
      if (!context.content.trim() && !request.supplement && !images.length) throw new AppError(400, '店名または店舗を特定する補足情報を入力してください。');
      const visible = Object.fromEntries(Object.entries(draft).filter(([, value]) => value !== null));
      const extraction = await this.ai.extract('[元の投稿]\n' + context.content + '\n[管理者の補足情報・原文とは別の未検証の手掛かり]\n' + request.supplement + '\n[管理者の入力欄]\n' + JSON.stringify(visible) + '\n[参考URL]\n' + (request.reference_url ?? ''), images, signal);
      if (extraction.message.mentions.length !== 1) return result([], extraction.message.mentions.length > 1 ? '複数の店舗が見つかりました。対象の店名を入力して再度補完してください。' : extraction.message.unresolved_reason || extraction.message.ignore_reason || '店舗を特定できませんでした。店名や所在地を追加してください。');
      const extracted = extraction.message.mentions[0]; if (!extracted) throw new Error('A single extracted mention was not available');
      if (extracted.subject_kind === 'event' || extracted.subject_kind === 'product') return result([], '催事・商品から店舗を確定できません。対象の常設店舗名を入力してください。');
      mention = { ...extracted, branch_name: draft.branch_name || extracted.branch_name, area: draft.area || extracted.area, category: draft.category || extracted.category };
    } else mention = extractedMentionSchema.parse({ shop_name: name, branch_name: draft.branch_name || null, area: draft.area || null, category: draft.category || null, needs_review: true, confidence_reason: '管理者の入力を検索の手掛かりに使用' });
    const references = [...new Set([request.reference_url, draft.canonical_url].filter((value): value is string => Boolean(value)))];
    const searched = await this.ai.search(mention, { category: draft.category, address: draft.address, phone: draft.phone, supplement: request.supplement, reference_urls: references, image_inputs: [...images] }, signal);
    if (searched.metrics.web_search_calls !== searched.metrics.api_attempts) throw new Error('Suggestion search did not complete the expected web lookup');
    const candidates: Suggestion[] = []; let rejected = 0;
    for (const candidate of searched.candidates.candidates) {
      const cited = sanitizeCitedCandidate(candidate, searched.source_urls); if (!cited) { rejected++; continue; }
      const evidence = [...new Set([cited.evidence_url, cited.canonical_url].filter((value): value is string => Boolean(value)))]; for (const url of evidence) validateReferenceUrl(url);
      candidates.push({ shop_name: cited.name, branch_name: null, area: cited.area, category: cited.category, address: cited.address, phone: cited.phone, canonical_url: cited.canonical_url, evidence_urls: evidence, reason: cited.confidence_reason });
    }
    let reason = searched.candidates.unresolved_reason;
    if (rejected) { console.warn('Uncited suggestion candidates excluded', { mentionId: context.mention_id, count: rejected }); reason = [reason, '検索の出典を確認できない候補は表示していません。'].filter(Boolean).join(' / '); }
    if (!candidates.length && !reason) reason = '根拠のある候補が見つかりませんでした。店名や所在地を追加してください。';
    return result(candidates, reason);
  }
  async suggest(id: number, request: SuggestionRequest): Promise<Result> {
    if (this.active.has(id)) throw new AppError(409, 'この項目はAIで確認中です。完了を待ってください。');
    if (this.active.size >= 2) throw new AppError(429, 'AI補完が混み合っています。少し待って再度お試しください。');
    this.active.add(id); const signal = AbortSignal.timeout(45000);
    try {
      const before = await this.context(id, request); const result = await this.search(before, request, signal); const after = await this.context(id, request);
      if (before.shop_id !== after.shop_id || before.content !== after.content || JSON.stringify(before.image_sources) !== JSON.stringify(after.image_sources)) throw new AppError(409, '元のデータが変更されています。最新情報を読み込んでください。');
      return result;
    } catch (error) {
      if (error instanceof AppError) throw error;
      console.error('AI suggestion failed', { mentionId: id, error });
      if (signal.aborted) throw new AppError(504, 'AI補完が制限時間を超えました。入力は保持しています。再度お試しください。');
      throw new AppError(502, 'AI補完に失敗しました。入力は保持しています。時間をおいて再度お試しください。');
    } finally { this.active.delete(id); }
  }
}
export function registerSuggestionRoutes(app: Hono<WebEnvironment>, database: Database, config: Config): void {
  const suggestions = new ReviewSuggestions(database, config, new RestaurantAI(readAISettings(), config.imageDirectory));
  app.post('/api/admin/reviews/:id/suggestions', async context => {
    requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token'), config));
    return context.json(await suggestions.suggest(z.coerce.number().int().positive().parse(context.req.param('id')), suggestionRequest.parse(await context.req.json())));
  });
}
