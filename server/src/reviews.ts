import { createHash } from 'node:crypto';
import type { Hono } from 'hono';
import { z } from 'zod';
import type { Review } from '@meshi/contracts';
import { countersSchema, failuresSchema, reviewsSchema } from '@meshi/contracts';
import type { WebEnvironment } from './app.js';
import { requireAdmin, requireWrite } from './auth.js';
import type { Config } from './config.js';
import type { Database, Executor } from './db/database.js';
import { idRow } from './db/database.js';
import { AppError } from './errors.js';
import { canonicalArea, canonicalCategory } from './geography.js';
import { validateImageSourceUrl } from './images.js';
import { ImageCache } from './image-cache.js';
import { csvDocument } from './csv.js';
import { assetRecord, candidateRecord, eventExcluded, mentionRecord, messageRecord, openReview, primaryMention, reviewEventRecord, shopRecord, statusMatches } from './records.js';
import type { AssetRecord, CandidateRecord, MentionRecord, MessageRecord, ReviewEventRecord, ShopRecord } from './records.js';
import { reviewGroups } from './review-groups.js';
import type { ReviewGroup } from './review-groups.js';

interface ReviewData { mentions: readonly MentionRecord[]; messages: ReadonlyMap<string, MessageRecord>; shops: ReadonlyMap<number, ShopRecord>; candidates: readonly CandidateRecord[]; assets: readonly AssetRecord[]; events: readonly ReviewEventRecord[]; }
interface ReviewView extends Review { source_created_at: string | null; processing_status: string; fetch_error: string | null; resolution_status: string; difference_type: string | null; metadata_difference_type: string | null; extraction_source: string; review_group_key: string | null; review_group_mention_ids: readonly number[]; }
const queueQuery = z.object({ scope: z.enum(['all', 'identity', 'metadata']).default('all'), status: z.enum(['pending', 'approved', 'deferred', 'rejected', 'unresolved']).default('pending'), reason: z.string().max(255).default(''), source: z.string().max(64).default(''), q: z.string().max(500).default(''), cursor: z.coerce.number().int().positive().optional(), limit: z.coerce.number().int().min(1).max(50).default(25) });
type QueueQuery = z.infer<typeof queueQuery>;
const failureQuery = z.object({ cursor: z.string().regex(/^[1-9][0-9]{16,19}$/).optional(), limit: z.coerce.number().int().min(1).max(50).default(25), active_only: z.enum(['true', 'false']).default('false').transform(value => value === 'true') });
const positiveId = z.coerce.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const evidenceError = z.object({ codes: z.array(z.string().trim().min(1)).min(1), reasons: z.array(z.string().trim().min(1)).min(1) });
function errorDisplay(mention: MentionRecord): string | null {
  const error = mention.extraction_error; if (!error?.startsWith('evidence_review:')) return error;
  try { const parsed = evidenceError.parse(JSON.parse(error.slice('evidence_review:'.length))); return [...new Set(parsed.reasons)].join(' / '); }
  catch (cause) { console.warn('Malformed stored evidence review details', { mentionId: mention.id, cause }); return 'この項目は確認待ちです。原文・添付と候補を確認してください。'; }
}
function issueList(mention: MentionRecord, shop: ShopRecord | undefined, message: MessageRecord): Review['issues'] {
  const issues: Review['issues'] = []; const name = shop ? shop.shop_name : mention.extracted_name; const area = shop ? shop.area : mention.extracted_area; const category = shop ? shop.category : mention.extracted_category;
  const add = (code: string, label: string): void => { issues.push({ code, label }); };
  if (!name || name === '（店舗名未特定）') add('missing_name', '店舗名未特定'); else if (mention.review_status === 'pending' || mention.review_status === 'deferred') add('identity_review', '店・支店要確認');
  if (!area) add('missing_area', 'エリア未入力'); else if (!canonicalArea(area)) add('unknown_area', 'エリア要確認');
  if (!category) add('missing_category', 'カテゴリ未入力'); else if (!canonicalCategory(category)) add('unknown_category', 'カテゴリ要確認');
  else if ((mention.metadata_review_status === 'pending' || mention.metadata_review_status === 'deferred') && !issues.some(issue => ['missing_area', 'unknown_area'].includes(issue.code))) add('metadata_review', '登録情報要確認');
  if (mention.difference_type && ['new_pipeline_difference', 'new_ambiguous'].includes(mention.difference_type)) add('identity_difference', '店舗情報に不一致・候補あり');
  if (message.fetch_error) add('source_unavailable', '元投稿の取得失敗');
  if (message.processing_status === 'failed') add('processing_failed', '登録処理失敗');
  return issues;
}
export class Reviews {
  constructor(private readonly database: Database, private readonly config: Config) {}
  private async data(connection: Executor = this.database): Promise<ReviewData> {
    const mentions = await connection.query('SELECT * FROM shop_mentions ORDER BY id', mentionRecord);
    const messages = await connection.query('SELECT * FROM messages', messageRecord);
    const shops = await connection.query('SELECT * FROM shops', shopRecord);
    const candidates = await connection.query('SELECT * FROM resolution_candidates ORDER BY rank,id', candidateRecord);
    const assets = await connection.query('SELECT * FROM source_assets ORDER BY id', assetRecord);
    const events = await connection.query('SELECT * FROM review_events ORDER BY id', reviewEventRecord);
    return { mentions, messages: new Map(messages.map(message => [message.message_id, message])), shops: new Map(shops.map(shop => [shop.id, shop])), candidates, assets, events };
  }
  private discordUrl(message: MessageRecord): string | null {
    const channel = message.channel_id || this.config.channelId;
    return this.config.guildId && channel ? `https://discord.com/channels/${this.config.guildId}/${channel}/${message.message_id}` : null;
  }
  private item(mention: MentionRecord, data: ReviewData, group?: ReviewGroup): ReviewView {
    const message = data.messages.get(mention.message_id); if (!message) throw new Error(`Review message is missing: mention_id=${mention.id} message_id=${mention.message_id}`);
    const shop = mention.shop_id === null ? undefined : data.shops.get(mention.shop_id);
    if (mention.shop_id !== null && !shop) throw new Error(`Review shop is missing: mention_id=${mention.id} shop_id=${mention.shop_id}`);
    return { id: mention.id, version: mention.version, message_id: mention.message_id, discord_url: this.discordUrl(message), message_content: message.content, source_created_at: message.source_created_at, processing_status: message.processing_status, fetch_error: message.fetch_error, resolution_status: mention.resolution_status, difference_type: mention.difference_type, metadata_difference_type: mention.metadata_difference_type, extraction_source: mention.extraction_source, review_group_key: group?.key ?? null, review_group_mention_ids: group?.mention_ids ?? [mention.id], source_url: mention.source_url, extracted_name: mention.extracted_name, extracted_branch_name: mention.extracted_branch_name, extracted_area: mention.extracted_area, extracted_category: mention.extracted_category, review_status: mention.review_status, metadata_review_status: mention.metadata_review_status, extraction_error: errorDisplay(mention), confidence_reason: mention.confidence_reason, issues: issueList(mention, shop, message), shop: shop ? { ...shop } : null,
      assets: data.assets.filter(asset => asset.message_id === message.message_id).map(asset => {
        let photo: string | null = null;
        if (asset.kind === 'image' && asset.fetch_status !== 'unavailable') {
          try { validateImageSourceUrl(asset.url); photo = `/api/admin/evidence/photo/${asset.id}`; }
          catch (error) { if (!(error instanceof AppError)) throw error; console.warn('Review photo is unavailable', { assetId: asset.id, messageId: message.message_id }); }
        }
        return { ...asset, proxy_url: asset.kind === 'image' ? `/api/admin/evidence/image/${asset.id}` : null, photo_url: photo };
      }),
      candidates: data.candidates.filter(candidate => candidate.mention_id === mention.id).map(candidate => ({ ...candidate, name_similarity: candidate.name_similarity_milli / 1000 })), review_group_count: group?.mention_ids.length ?? 1, review_group_reason: group?.reason ?? null };
  }
  async detail(id: number): Promise<ReviewView> {
    const data = await this.data(); const mention = data.mentions.find(item => item.id === id);
    if (!mention) throw new AppError(404, '確認項目が見つかりません。');
    return this.item(mention, data, reviewGroups(data.mentions, data.candidates).get(id));
  }
  async queue(query: QueueQuery): Promise<z.infer<typeof reviewsSchema>> {
    if (query.scope === 'metadata' && query.status === 'rejected') throw new AppError(400, '不正なデータ確認状態です。');
    const data = await this.data();
    const matches = (mention: MentionRecord): boolean => {
      const status = query.status === 'unresolved' ? statusMatches(mention, 'pending', query.scope) || statusMatches(mention, 'deferred', query.scope) : statusMatches(mention, query.status, query.scope);
      if (!status) return false;
      if (query.reason === 'source_unavailable' && data.messages.get(mention.message_id)?.fetch_error === null) return false;
      if (query.reason && query.reason !== 'source_unavailable') {
        if (query.scope === 'identity' && mention.difference_type !== query.reason) return false;
        if (query.scope === 'metadata' && mention.metadata_difference_type !== query.reason) return false;
        if (query.scope === 'all' && mention.difference_type !== query.reason && mention.metadata_difference_type !== query.reason) return false;
      }
      if (query.source && mention.extraction_source !== query.source) return false;
      const term = query.q.trim().toLowerCase();
      return !term || mention.extracted_name.toLowerCase().includes(term) || Boolean(mention.shop_id !== null && data.shops.get(mention.shop_id)?.shop_name.toLowerCase().includes(term));
    };
    const matching = data.mentions.filter(matches); const visible = matching.filter(mention => !query.cursor || mention.id > query.cursor).slice(0, query.limit + 1);
    const hasMore = visible.length > query.limit; const page = visible.slice(0, query.limit); const groups = query.scope === 'metadata' ? new Map<number, ReviewGroup>() : reviewGroups(data.mentions, data.candidates);
    const count = (status: MentionRecord['review_status']): number => data.mentions.filter(mention => statusMatches(mention, status, query.scope)).length;
    const counters = countersSchema.parse({ pending: count('pending'), approved: count('approved'), deferred: count('deferred'), rejected: count('rejected'), source_unavailable: data.mentions.filter(mention => data.messages.get(mention.message_id)?.fetch_error !== null).length, failed: [...data.messages.values()].filter(message => message.processing_status === 'failed').length, unresolved: data.mentions.filter(openReview).length });
    return { scope: query.scope, total_count: matching.length, counters, items: page.map(mention => this.item(mention, data, groups.get(mention.id))), next_cursor: hasMore ? page.at(-1)?.id ?? null : null };
  }
  async failures(query: z.infer<typeof failureQuery>): Promise<{ items: (z.infer<typeof failuresSchema>['items'][number] & { mention_ids: number[] })[]; next_cursor: string | null }> {
    const data = await this.data();
    const open = (mention: MentionRecord): boolean => ['pending', 'deferred'].includes(mention.review_status) || (mention.review_status === 'approved' && ['pending', 'deferred'].includes(mention.metadata_review_status));
    const failed = [...data.messages.values()].filter(message => message.processing_status === 'failed' && (!query.cursor || message.message_id > query.cursor) && (!query.active_only || !data.mentions.some(mention => mention.message_id === message.message_id) || data.mentions.some(mention => mention.message_id === message.message_id && open(mention)))).sort((a, b) => a.message_id < b.message_id ? -1 : a.message_id > b.message_id ? 1 : 0).slice(0, query.limit + 1);
    const visible = failed.slice(0, query.limit);
    return { items: visible.map(message => {
      const related = data.mentions.filter(mention => mention.message_id === message.message_id); const active = related.find(open);
      return { message_id: message.message_id, message_content: message.content, discord_url: this.discordUrl(message), error: message.fetch_error ? '元投稿を取得できませんでした。' : '店舗情報の登録処理に失敗しました。', mention_ids: related.map(mention => mention.id), review_mention_id: active?.id ?? null, recovery_status: !related.length ? 'not_prepared' : active ? 'in_review' : 'handled' };
    }), next_cursor: failed.length > query.limit ? visible.at(-1)?.message_id ?? null : null };
  }
  async prepareFailure(messageId: string): Promise<ReviewView> {
    const id = await this.database.transaction(async connection => {
      if (connection.dialect === 'postgres') {
        const lock = createHash('sha256').update('meshi-archive:new-shop-creation').digest().readBigInt64BE().toString();
        await connection.execute('SELECT pg_advisory_xact_lock($1::bigint)', [lock]);
      }
      const message = (await connection.query('SELECT * FROM messages WHERE message_id=$1' + (connection.dialect === 'postgres' ? ' FOR UPDATE' : ''), messageRecord, [messageId]))[0];
      if (!message) throw new AppError(404, '投稿が見つかりません。');
      if (message.processing_status !== 'failed') throw new AppError(409, '投稿の処理状態が変わりました。一覧を再読み込みしてください。');
      const existing = (await connection.query('SELECT id FROM shop_mentions WHERE message_id=$1 ORDER BY id LIMIT 1', idRow, [messageId]))[0];
      if (existing) return existing.id;
      const now = new Date().toISOString();
      const created = (await connection.query("INSERT INTO shop_mentions(message_id,occurrence_index,extracted_name,review_status,metadata_review_status,metadata_difference_type,difference_type,resolution_status,extraction_source,version,created_at) VALUES($1,0,'（店舗名未特定）','pending','pending','missing_area','extraction_not_found','not_found','manual_recovery',1,$2) RETURNING id", idRow, [messageId, now]))[0];
      if (!created) throw new Error(`Failed review preparation returned no id: message_id=${messageId}`);
      await connection.execute("INSERT INTO review_events(mention_id,scope,action,note,created_at) VALUES($1,'identity','prepare_failed','失敗した投稿を手作業で確認する項目を作成',$2)", [created.id, now]);
      return created.id;
    });
    return this.detail(id);
  }
  async audit(): Promise<{ content: string; rowCount: number; messageCount: number; unlinkedCount: number }> {
    const data = await this.data(); const attachmentUrls = new Set(data.assets.filter(asset => ['attachment', 'image'].includes(asset.kind)).map(asset => asset.url.split('?')[0]?.toLowerCase() ?? ''));
    const urlPattern = /https?:\/\/[^\s<>"']+/gi;
    for (const message of data.messages.values()) for (const line of (message.content ?? '').split(/\r?\n/)) if (line.startsWith('[Attachment]')) for (const match of line.matchAll(urlPattern)) attachmentUrls.add(match[0].replace(/[.,;\)\]\}]+$/, '').split('?')[0]?.toLowerCase() ?? '');
    const cell = (value: string | null): string => {
      const text = (value ?? '').replace(urlPattern, original => {
        const url = original.replace(/[.,;\)\]\}]+$/, ''); const separator = url.indexOf('?'); if (separator < 0) return original;
        const base = url.slice(0, separator); const query = url.slice(separator + 1);
        return attachmentUrls.has(base.toLowerCase()) || /(?:^|[&;])(?:x-amz-[^=]+|x-goog-[^=]+|sig|signature|token|access_token|auth|authorization|password|key|ex|is|hm)=/i.test(query) ? base + '?__redacted__' + original.slice(url.length) : original;
      });
      return /^[\t\r\n]/.test(text) || /^[=+\-@]/.test(text.trimStart()) ? "'" + text : text;
    };
    const rows: (string | number | boolean | null)[][] = [['mention_id', 'mention.version', 'message_id', 'occurrence_index', 'shop.id', 'shop.version', 'shop.name', 'shop.branch_name', 'shop.area', 'shop.category', 'shop.is_public', 'is_representative_mention', 'extracted_name', 'extracted_branch_name', 'extracted_area', 'extracted_category', 'review_status', 'metadata_review_status', 'resolution_status', 'needs_review', 'difference_type', 'metadata_difference_type', 'resolution_method', 'resolution_basis', 'reused_from_mention_id', 'source_url', 'extraction_source', 'extraction_error', 'confidence_reason', 'reviewed_at', 'metadata_reviewed_at', 'message.source_created_at', 'message.content', 'message.processing_status', 'message.fetch_error', 'message.asset_count', 'mention.review_event_count']];
    for (const mention of data.mentions) {
      const shop = mention.shop_id === null ? undefined : data.shops.get(mention.shop_id); const message = data.messages.get(mention.message_id);
      if (!message) throw new Error(`Audit message is missing: message_id=${mention.message_id}`);
      const related = data.mentions.filter(item => item.shop_id === mention.shop_id && item.shop_id !== null);
      rows.push([mention.id, mention.version, mention.message_id, mention.occurrence_index, shop?.id ?? null, shop?.version ?? null, cell(shop?.shop_name ?? null), cell(shop?.branch_name ?? null), cell(shop?.area ?? null), cell(shop?.category ?? null), Boolean(shop && related.some(item => item.review_status === 'approved' && item.metadata_review_status === 'approved')), Boolean(shop && primaryMention(related)?.id === mention.id), cell(mention.extracted_name), cell(mention.extracted_branch_name), cell(mention.extracted_area), cell(mention.extracted_category), mention.review_status, mention.metadata_review_status, mention.resolution_status, !eventExcluded(mention) && !(mention.review_status === 'rejected' && mention.difference_type === 'manual_excluded') && (['pending', 'deferred'].includes(mention.review_status) || ['pending', 'deferred'].includes(mention.metadata_review_status)), cell(mention.difference_type), cell(mention.metadata_difference_type), cell(mention.resolution_method), cell(mention.resolution_basis), mention.reused_from_mention_id, cell(mention.source_url), cell(mention.extraction_source), cell(mention.extraction_error), cell(mention.confidence_reason), mention.reviewed_at, mention.metadata_reviewed_at, message.source_created_at, cell(message.content), message.processing_status, cell(message.fetch_error), data.assets.filter(asset => asset.message_id === mention.message_id).length, data.events.filter(event => event.mention_id === mention.id).length]);
    }
    return { content: csvDocument(rows), rowCount: data.mentions.length, messageCount: new Set(data.mentions.map(mention => mention.message_id)).size, unlinkedCount: data.mentions.filter(mention => mention.shop_id === null).length };
  }
}
export function registerReviewRoutes(app: Hono<WebEnvironment>, database: Database, config: Config): void {
  const reviews = new Reviews(database, config);
  const images = new ImageCache(config.imageDirectory);
  for (const kind of ['image', 'photo'] as const) app.get(`/api/admin/evidence/${kind}/:id`, async context => {
    requireAdmin(context.get('session'));
    const id = positiveId.parse(context.req.param('id'));
    const asset = (await database.query('SELECT * FROM source_assets WHERE id=$1', assetRecord, [id]))[0];
    if (!asset || asset.kind !== 'image' || (kind === 'photo' && asset.fetch_status === 'unavailable')) throw new AppError(404, '利用できる投稿写真がありません。');
    validateImageSourceUrl(asset.url);
    if (kind === 'image' && !['cdn.discordapp.com', 'media.discordapp.net', 'images-ext-1.discordapp.net', 'images-ext-2.discordapp.net'].includes(new URL(asset.url).hostname)) throw new AppError(400, '許可されていない画像URLです。');
    try {
      if (kind === 'photo') return context.body(new Uint8Array(await images.processed(asset.url)), 200, { 'Content-Type': 'image/webp' });
      const result = await images.original(asset.url);
      if (result.bytes.length > 10 * 1024 * 1024) throw new Error(`Evidence image exceeds preview limit: asset_id=${asset.id} bytes=${result.bytes.length}`);
      return context.body(new Uint8Array(result.bytes), 200, { 'Content-Type': result.contentType });
    } catch (error) { console.error('Review image retrieval failed', { assetId: asset.id, host: new URL(asset.url).hostname, kind, error }); throw new AppError(502, '写真を取得できませんでした。再試行してください。'); }
  });
  app.get('/api/admin/reviews', async context => { requireAdmin(context.get('session')); return context.json(await reviews.queue(queueQuery.parse(context.req.query()))); });
  app.get('/api/admin/reviews/:id', async context => { requireAdmin(context.get('session')); return context.json(await reviews.detail(positiveId.parse(context.req.param('id')))); });
  app.get('/api/admin/review-failures', async context => { requireAdmin(context.get('session')); return context.json(await reviews.failures(failureQuery.parse(context.req.query()))); });
  app.post('/api/admin/review-failures/:id/prepare', async context => {
    requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token'), config));
    return context.json(await reviews.prepareFailure(z.string().regex(/^[1-9][0-9]{16,19}$/).parse(context.req.param('id'))));
  });
  app.get('/admin/review/audit.csv', async context => {
    requireAdmin(context.get('session')); const result = await reviews.audit(); const hash = createHash('sha256').update(result.content).digest('hex'); const now = new Date().toISOString();
    return context.body(result.content, 200, { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': `attachment; filename="meshi_review_audit_${now.replace(/[-:.]/g, '')}_${hash.slice(0, 12)}.csv"`, Pragma: 'no-cache', Expires: '0', 'X-Export-Generated-At': now, 'X-Export-Content-SHA256': hash, 'X-Export-Row-Count': String(result.rowCount), 'X-Export-Row-Unit': 'mention', 'X-Export-Message-Count': String(result.messageCount), 'X-Export-Unlinked-Count': String(result.unlinkedCount), 'X-Export-Scope': 'all-mentions' });
  });
}
