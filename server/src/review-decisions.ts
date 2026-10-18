import type { Hono } from 'hono';
import { z } from 'zod';
import type { WebEnvironment } from './app.js';
import { requireAdmin, requireWrite } from './auth.js';
import type { Config } from './config.js';
import type { Database, Executor } from './db/database.js';
import { countRow } from './db/database.js';
import { AppError } from './errors.js';
import { canonicalArea } from './geography.js';
import { canonicalCategory } from './categories.js';
import { ImageCache } from './image-cache.js';
import { storeUploadedImage, validateImageSourceUrl } from './images.js';
import { safeHttpUrl } from './links.js';
import { assetRecord, candidateRecord, eventExcluded, mentionRecord, shopRecord } from './records.js';
import type { MentionRecord, ShopRecord } from './records.js';
import { autoResolvePending, metadataAssessment } from './reevaluation.js';
import { normalizeAddress, normalizeExternalIdentity, normalizeName, normalizePhone, normalizeUrlIdentity } from './resolution.js';
import { collectExternalIdentities, externalIdentity, findShopCollisions, identitiesConflict, lockShopCreation } from './shop-creation.js';
import type { NewShopIdentity } from './shop-creation.js';

const version = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const optionalText = (maximum: number): z.ZodDefault<z.ZodNullable<z.ZodString>> => z.string().trim().max(maximum).nullable().default(null);
const url = z.string().trim().max(2048).refine(value => safeHttpUrl(value) !== null, '有効なhttpまたはhttpsのURLを入力してください。');
const editable = z.strictObject({ shop_name: z.string().trim().min(1).max(500), branch_name: z.string().trim().max(255).nullable().optional(), area: z.string().trim().min(1).max(255).transform((value, context) => { const canonical = canonicalArea(value); if (canonical) return canonical; context.addIssue({ code: 'custom', message: '登録済みの市区町村名・駅名を選択してください。' }); return z.NEVER; }), category: z.string().trim().min(1).max(255), address: optionalText(2000), phone: optionalText(64), canonical_url: url.nullable().default(null) });
const base = { scope: z.enum(['identity', 'metadata']).default('identity'), expected_version: version, shop_version: version.nullable().default(null) };
export const reviewDecisionSchema = z.discriminatedUnion('action', [
  z.strictObject({ ...base, action: z.literal('approve_current') }),
  z.strictObject({ ...base, action: z.literal('approve_candidate'), candidate_id: version }),
  z.strictObject({ ...base, action: z.literal('edit_and_approve'), shop: editable, photo_asset_id: version.nullable().default(null), confirm_metadata: z.boolean().default(false), suggestion_evidence_urls: z.array(url).max(5).default([]), suggestion_reason: z.string().max(2000).nullable().default(null), supplement: z.string().max(4000).nullable().default(null) }),
  z.strictObject({ ...base, action: z.literal('exclude'), reason: z.string().max(2000).nullable().default(null) }),
  z.strictObject({ ...base, action: z.literal('reject'), reason: z.string().max(2000).nullable().default(null) }),
  z.strictObject({ ...base, action: z.literal('defer'), note: z.string().max(2000).nullable().default(null) }),
  z.strictObject({ ...base, action: z.literal('merge'), target_shop_id: version, target_version: version, is_visited: z.boolean(), visited_at: z.iso.datetime({ offset: true }).nullable().default(null), rating: z.number().int().min(1).max(5).nullable().default(null), memo: z.string().max(20000).nullable().default(null) }),
]);
export type ReviewDecision = z.infer<typeof reviewDecisionSchema>;
interface PreparedPhoto { asset_id: number; message_id: string; source_url: string; image_key: string; }
export interface ReviewDecisionResult { mention_id: number; scope: 'identity' | 'metadata'; review_status: string; metadata_review_status: string; shop_id: number | null; version: number; automatically_resolved_count: number; automatically_resolved_mention_ids: readonly number[]; shop: ShopRecord | null; }
function shopSnapshot(shop: ShopRecord | null): Pick<ShopRecord, 'id' | 'version' | 'shop_name' | 'branch_name' | 'area' | 'category' | 'address' | 'phone' | 'canonical_url' | 'image_key'> | null {
  return shop ? { id: shop.id, version: shop.version, shop_name: shop.shop_name, branch_name: shop.branch_name, area: shop.area, category: shop.category, address: shop.address, phone: shop.phone, canonical_url: shop.canonical_url, image_key: shop.image_key } : null;
}
export function mergeSnapshot(shops: readonly ShopRecord[], mentions: readonly MentionRecord[], reason = 'manual_merge'): string {
  return JSON.stringify({ schema_version: 1, kind: 'shop_merge_snapshot', reason, shops, moved_mentions: mentions.map(mention => ({ id: mention.id, message_id: mention.message_id, occurrence_index: mention.occurrence_index, shop_id: mention.shop_id, version: mention.version, review_status: mention.review_status, metadata_review_status: mention.metadata_review_status, resolution_status: mention.resolution_status, resolution_method: mention.resolution_method, reviewed_at: mention.reviewed_at, metadata_reviewed_at: mention.metadata_reviewed_at })) });
}
async function validateCreation(connection: Executor, identity: NewShopIdentity, existing: ShopRecord | null): Promise<void> {
  normalizeUrlIdentity(identity.canonical_url); normalizeUrlIdentity(identity.evidence_url);
  if (identitiesConflict(collectExternalIdentities(identity.external_source, identity.external_id, [identity.canonical_url, identity.evidence_url]))) throw new AppError(409, '候補の店舗識別子が同じサービス内で矛盾しています。');
  if (existing && identity.evidence_url === null && identity.shop_name === existing.shop_name && (identity.branch_name ?? '') === (existing.branch_name ?? '') && canonicalArea(identity.area) === canonicalArea(existing.area) && normalizeAddress(identity.address) === normalizeAddress(existing.address) && normalizePhone(identity.phone) === normalizePhone(existing.phone) && normalizeUrlIdentity(identity.canonical_url) === normalizeUrlIdentity(existing.canonical_url) && identity.external_source === existing.external_source && identity.external_id === existing.external_id) return;
  const collision = (await findShopCollisions(connection, identity, existing?.id ?? null))[0];
  if (collision) throw new AppError(409, `既存の店舗と重複する可能性があります。店舗ID ${collision.shop_id} を確認し、関連付けまたは統合を選択してください。`);
}
async function event(connection: Executor, mention: MentionRecord, scope: 'identity' | 'metadata', action: string, previous: number | null, selected: number | null, note: string | null): Promise<void> {
  await connection.execute('INSERT INTO review_events(mention_id,scope,action,previous_shop_id,selected_shop_id,note,created_at) VALUES($1,$2,$3,$4,$5,$6,$7)', [mention.id, scope, action, previous, selected, note, new Date().toISOString()]);
}
async function persistShop(connection: Executor, shop: ShopRecord, expected: number): Promise<void> {
  const changed = await connection.execute('UPDATE shops SET shop_name=$3,branch_name=$4,area=$5,category=$6,address=$7,phone=$8,canonical_url=$9,external_source=$10,external_id=$11,is_visited=$12,visited_at=$13,rating=$14,memo=$15,image_key=$16,version=$17,updated_at=$18 WHERE id=$1 AND version=$2', [shop.id, expected, shop.shop_name, shop.branch_name, shop.area, shop.category, shop.address, shop.phone, shop.canonical_url, shop.external_source, shop.external_id, shop.rating !== null, shop.visited_at, shop.rating, shop.memo, shop.image_key, shop.version, new Date().toISOString()]);
  if (changed !== 1) throw new AppError(409, '店舗情報が更新されています。最新情報を確認してください。');
}
async function persistMention(connection: Executor, mention: MentionRecord, expected: number): Promise<void> {
  const changed = await connection.execute('UPDATE shop_mentions SET shop_id=$3,review_status=$4,resolution_status=$5,metadata_review_status=$6,resolution_method=$7,difference_type=$8,metadata_difference_type=$9,reviewed_at=$10,metadata_reviewed_at=$11,version=$12 WHERE id=$1 AND version=$2', [mention.id, expected, mention.shop_id, mention.review_status, mention.resolution_status, mention.metadata_review_status, mention.resolution_method, mention.difference_type, mention.metadata_difference_type, mention.reviewed_at, mention.metadata_reviewed_at, mention.version]);
  if (changed !== 1) throw new AppError(409, '確認項目が更新されています。最新情報を確認してください。');
}
function approve(mention: MentionRecord, shop: ShopRecord): void {
  mention.shop_id = shop.id; mention.review_status = 'approved'; mention.resolution_status = 'resolved'; mention.resolution_method = 'manual'; mention.reviewed_at = new Date().toISOString(); mention.version++;
  if (mention.difference_type === 'manual_excluded') mention.difference_type = null;
}
function approveMetadata(mention: MentionRecord, shop: ShopRecord | null): void {
  if (!shop || !shop.area || !shop.category?.trim()) throw new AppError(400, 'エリアとカテゴリを入力してから承認してください。');
  const canonical = canonicalArea(shop.area); if (!canonical) throw new AppError(400, 'エリアは登録済みの市区町村名・駅名を選択してください。');
  const category = canonicalCategory(shop.category); if (!category) throw new AppError(400, 'カテゴリは登録済みの分類を選択してください。');
  if (shop.area !== canonical) { shop.area = canonical; shop.version++; }
  if (shop.category !== category) { shop.category = category; shop.version++; }
  mention.metadata_review_status = 'approved'; mention.metadata_difference_type = null; mention.metadata_reviewed_at = new Date().toISOString(); mention.version++;
}
function editedCategory(value: string, current: string | null): string {
  const canonical = canonicalCategory(value);
  if (canonical) return canonical;
  if (current !== null && value === current.trim()) return current;
  throw new AppError(422, 'カテゴリは登録済みの分類を選択してください。');
}
function evidenceCategory(value: string | null, mentionId: number): string | null {
  const canonical = canonicalCategory(value);
  if (value && !canonical) console.warn('Review evidence category requires classification', { mentionId, category: value });
  return canonical;
}
function identityOf(shop: Pick<ShopRecord, 'shop_name' | 'branch_name' | 'area' | 'address' | 'phone' | 'canonical_url' | 'external_source' | 'external_id'>, evidence: string | null = null): NewShopIdentity { return { shop_name: shop.shop_name, branch_name: shop.branch_name, area: shop.area, address: shop.address, phone: shop.phone, canonical_url: shop.canonical_url, external_source: shop.external_source, external_id: shop.external_id, evidence_url: evidence }; }
export class ReviewDecisions {
  private readonly images: ImageCache;
  constructor(private readonly database: Database, private readonly config: Config) { this.images = new ImageCache(config.imageDirectory); }
  private async photo(id: number, decision: ReviewDecision): Promise<PreparedPhoto | null> {
    if (decision.action !== 'edit_and_approve' || decision.photo_asset_id === null) return null;
    const mention = (await this.database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [id]))[0];
    if (!mention) throw new AppError(404, '確認項目が見つかりません。');
    if (mention.version !== decision.expected_version) throw new AppError(409, '確認項目が更新されています。');
    if (mention.shop_id !== null) {
      const shop = (await this.database.query('SELECT * FROM shops WHERE id=$1', shopRecord, [mention.shop_id]))[0];
      if (!shop || shop.version !== decision.shop_version) throw new AppError(409, '店舗情報が更新されています。');
    }
    const asset = (await this.database.query('SELECT * FROM source_assets WHERE id=$1', assetRecord, [decision.photo_asset_id]))[0];
    if (!asset || asset.message_id !== mention.message_id || asset.kind !== 'image' || asset.fetch_status === 'unavailable') throw new AppError(400, '選択した写真は、この投稿の利用できる写真ではありません。');
    validateImageSourceUrl(asset.url);
    let bytes: Buffer;
    try { bytes = await this.images.processed(asset.url); }
    catch (error) { console.error('Review decision photo retrieval failed', { mentionId: id, assetId: asset.id, error }); throw new AppError(502, '写真を取得できませんでした。入力を保持したまま再試行してください。'); }
    const uploaded = await storeUploadedImage(bytes, this.config.imageDirectory);
    return { asset_id: asset.id, message_id: mention.message_id, source_url: asset.url, image_key: uploaded.key };
  }
  async apply(id: number, decision: ReviewDecision): Promise<ReviewDecisionResult> {
    if (decision.scope === 'metadata' && !['approve_current', 'edit_and_approve', 'defer'].includes(decision.action)) throw new AppError(400, '登録情報の確認では、この操作はできません。');
    const photo = await this.photo(id, decision);
    return this.database.transaction(async connection => {
      await lockShopCreation(connection);
      const suffix = connection.dialect === 'postgres' ? ' FOR UPDATE' : '';
      const mention = (await connection.query('SELECT * FROM shop_mentions WHERE id=$1' + suffix, mentionRecord, [id]))[0];
      if (!mention) throw new AppError(404, '確認項目が見つかりません。');
      if (mention.version !== decision.expected_version) throw new AppError(409, '確認項目が更新されています。最新情報を確認してください。');
      if (photo) {
        const asset = (await connection.query('SELECT * FROM source_assets WHERE id=$1' + suffix, assetRecord, [photo.asset_id]))[0];
        if (!asset || asset.message_id !== mention.message_id || asset.message_id !== photo.message_id || asset.kind !== 'image' || asset.url !== photo.source_url || asset.fetch_status === 'unavailable') throw new AppError(409, '元投稿の写真が変更されています。最新情報を確認してください。');
      }
      if (eventExcluded(mention)) {
        const sameIdentity = decision.action === 'edit_and_approve' && decision.scope === 'identity' && normalizeName(decision.shop.shop_name) === normalizeName(mention.extracted_name) && normalizeName(decision.shop.branch_name ?? '') === normalizeName(mention.extracted_branch_name ?? '') && !decision.shop.address && !normalizePhone(decision.shop.phone) && !decision.shop.canonical_url;
        if (['approve_current', 'approve_candidate'].includes(decision.action) || (decision.action === 'defer' && decision.scope === 'identity') || sameIdentity) throw new AppError(409, '催事会場は登録対象外です。出店元の常設店舗を根拠付きで確認し、店名・支店名を修正するか住所・電話・店舗URLを追加してください。');
      }
      const sourceId = mention.shop_id; const requestedIds = [sourceId, decision.action === 'merge' ? decision.target_shop_id : null].filter((value): value is number => value !== null).sort((a, b) => a - b);
      const stored = requestedIds.length ? await connection.query(`SELECT * FROM shops WHERE id IN (${requestedIds.map((_, index) => '$' + (index + 1)).join(',')}) ORDER BY id${suffix}`, shopRecord, requestedIds) : [];
      let shop = sourceId === null ? null : stored.find(item => item.id === sourceId) ?? null;
      if (sourceId !== null && !shop) throw new AppError(404, '店舗が見つかりません。');
      const before = shop ? { ...shop } : null; const expectedShopVersion = shop?.version ?? null;
      if (decision.action !== 'defer' && shop && decision.shop_version !== shop.version) throw new AppError(409, '店舗情報が更新されています。最新情報を確認してください。');
      let note: string | null = null; let deleteSource: number | null = null; let targetExpected: number | null = null;
      const create = async (values: NewShopIdentity & { category: string | null }): Promise<ShopRecord> => {
        const now = new Date().toISOString();
        const created = (await connection.query('INSERT INTO shops(shop_name,branch_name,area,category,address,phone,canonical_url,external_source,external_id,is_visited,created_at,updated_at,version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,1) RETURNING *', shopRecord, [values.shop_name, values.branch_name, values.area, values.category, values.address, values.phone, values.canonical_url, values.external_source, values.external_id, false, now, now]))[0];
        if (!created) throw new Error(`Shop creation returned no row: mention_id=${id}`); return created;
      };
      if (decision.scope === 'metadata') {
        if (decision.action === 'defer') { mention.metadata_review_status = 'deferred'; mention.metadata_reviewed_at = new Date().toISOString(); mention.version++; note = decision.note; }
        else {
          if (!shop) throw new AppError(409, '登録情報を確認する店舗がありません。');
          if (decision.action === 'edit_and_approve') {
            const input = decision.shop;
            if (input.shop_name !== shop.shop_name || (input.branch_name !== undefined && input.branch_name !== shop.branch_name) || normalizeUrlIdentity(input.canonical_url) !== normalizeUrlIdentity(shop.canonical_url)) throw new AppError(400, '登録情報の確認では店名・支店名・店舗URLを変更できません。店舗の同定から操作してください。');
            await validateCreation(connection, { ...identityOf(shop), area: input.area, address: input.address, phone: input.phone, canonical_url: input.canonical_url }, shop);
            Object.assign(shop, { area: input.area, category: editedCategory(input.category, shop.category), address: input.address, phone: normalizePhone(input.phone), version: shop.version + 1 });
          }
          approveMetadata(mention, shop);
        }
      } else if (decision.action === 'approve_current') {
        if (!shop) {
          if (mention.difference_type === 'extraction_not_found' || mention.extracted_name === '（店舗名未特定）') throw new AppError(409, '店舗名が未特定です。店舗情報を入力してから登録してください。');
          const values = { shop_name: mention.extracted_name, branch_name: mention.extracted_branch_name, area: canonicalArea(mention.extracted_area), category: evidenceCategory(mention.extracted_category, id), address: null, phone: null, canonical_url: null, evidence_url: null, external_source: null, external_id: null };
          await validateCreation(connection, values, null); shop = await create(values);
        }
        approve(mention, shop); Object.assign(mention, metadataAssessment(shop, mention));
      } else if (decision.action === 'approve_candidate') {
        const candidate = (await connection.query('SELECT * FROM resolution_candidates WHERE id=$1 AND mention_id=$2', candidateRecord, [decision.candidate_id, id]))[0];
        if (!candidate) throw new AppError(404, '候補が見つかりません。');
        if (identitiesConflict(collectExternalIdentities(candidate.external_source, candidate.external_id, [candidate.canonical_url, candidate.evidence_url]))) throw new AppError(409, '候補の店舗識別子が矛盾しています。');
        const fromUrl = externalIdentity(candidate.canonical_url) ?? externalIdentity(candidate.evidence_url);
        const external = normalizeExternalIdentity(candidate.external_source, candidate.external_id) ?? (fromUrl ? normalizeExternalIdentity(...fromUrl) : null);
        const values = { shop_name: candidate.name, branch_name: shop ? shop.branch_name : mention.extracted_branch_name, area: canonicalArea(shop ? shop.area : mention.extracted_area) ?? canonicalArea(candidate.area) ?? canonicalArea(mention.extracted_area), category: candidate.category === null ? shop?.category ?? null : evidenceCategory(candidate.category, id), address: candidate.address ?? shop?.address ?? null, phone: candidate.phone !== null ? normalizePhone(candidate.phone) : shop?.phone ?? null, canonical_url: candidate.canonical_url ?? shop?.canonical_url ?? null, external_source: external?.[0] ?? shop?.external_source ?? null, external_id: external?.[1] ?? shop?.external_id ?? null, evidence_url: candidate.evidence_url };
        await validateCreation(connection, values, shop);
        if (!shop) shop = await create(values);
        Object.assign(shop, values, { version: shop.version + 1 }); approve(mention, shop); Object.assign(mention, metadataAssessment(shop, mention));
      } else if (decision.action === 'edit_and_approve') {
        const input = decision.shop; const external = externalIdentity(input.canonical_url);
        const values = { shop_name: input.shop_name, branch_name: input.branch_name !== undefined ? input.branch_name : shop?.branch_name ?? null, area: input.area, category: editedCategory(input.category, shop?.category ?? null), address: input.address, phone: normalizePhone(input.phone), canonical_url: input.canonical_url, external_source: external?.[0] ?? null, external_id: external?.[1] ?? null, evidence_url: null };
        await validateCreation(connection, values, shop); if (!shop) shop = await create(values);
        Object.assign(shop, values, { version: shop.version + 1 }); approve(mention, shop); Object.assign(mention, metadataAssessment(shop, mention));
      } else if (decision.action === 'merge') {
        if (sourceId === decision.target_shop_id) throw new AppError(409, '同じ店舗には統合できません。');
        const target = stored.find(item => item.id === decision.target_shop_id); if (!target) throw new AppError(404, '統合先が見つかりません。');
        if (target.version !== decision.target_version) throw new AppError(409, '統合先の店舗が更新されています。最新情報を確認してください。');
        targetExpected = target.version;
        const moved = shop ? await connection.query('SELECT * FROM shop_mentions WHERE shop_id=$1 ORDER BY id' + suffix, mentionRecord, [shop.id]) : [mention];
        note = mergeSnapshot(shop ? [shop, target] : [target], moved);
        if (shop && shop.image_key && target.image_key && shop.image_key !== target.image_key) throw new AppError(409, '統合元と統合先に異なる写真が登録されています。写真を確認してから統合してください。');
        if (!target.image_key) target.image_key = shop?.image_key ?? null;
        if (shop) {
          await connection.execute('UPDATE shop_redirects SET target_shop_id=$2 WHERE target_shop_id=$1', [shop.id, target.id]);
          await connection.execute("INSERT INTO shop_redirects(source_shop_id,target_shop_id,reason,created_at) VALUES($1,$2,'merge',$3)", [shop.id, target.id, new Date().toISOString()]);
          for (const related of moved) {
            if (related.id === mention.id) continue;
            const movedVersion = related.version; related.shop_id = target.id; related.version++;
            await persistMention(connection, related, movedVersion); await event(connection, related, 'identity', 'merge_related', shop.id, target.id, note);
          }
          deleteSource = shop.id;
        }
        target.rating = decision.rating; target.is_visited = decision.rating !== null; target.visited_at = decision.visited_at; target.memo = decision.memo; target.version++; shop = target;
        approve(mention, target); Object.assign(mention, metadataAssessment(target, mention));
      } else if (decision.action === 'exclude') {
        note = JSON.stringify({ reason: decision.reason, previous_reason: mention.difference_type, preserved_shop: shopSnapshot(shop) });
        mention.review_status = 'rejected'; mention.difference_type = 'manual_excluded'; mention.resolution_status = 'invalid'; mention.resolution_method = 'manual'; mention.reviewed_at = new Date().toISOString(); mention.version++;
      } else if (decision.action === 'reject') {
        mention.shop_id = null; mention.review_status = 'rejected'; mention.resolution_status = 'invalid'; mention.resolution_method = 'manual'; mention.reviewed_at = new Date().toISOString(); mention.version++; note = decision.reason?.trim() ? decision.reason : null;
      } else if (decision.action === 'defer') { mention.review_status = 'deferred'; mention.reviewed_at = new Date().toISOString(); mention.version++; note = decision.note; }
      if (decision.action === 'edit_and_approve') {
        if (photo && shop) { shop.image_key = photo.image_key; if (shop.version === expectedShopVersion) shop.version++; }
        if (decision.confirm_metadata && decision.scope === 'identity') { approveMetadata(mention, shop); await event(connection, mention, 'metadata', 'edit_and_approve', sourceId, mention.shop_id, '店舗情報と写真を確認して登録'); }
        note = JSON.stringify({ before: shopSnapshot(before), after: shopSnapshot(shop), photo_asset_id: decision.photo_asset_id, supplement: decision.supplement, selected_reference_urls: decision.suggestion_evidence_urls, selected_suggestion_reason: decision.suggestion_reason });
      }
      if (shop) {
        if (shop.phone && shop.phone.length > 32) throw new AppError(400, '電話番号が長すぎます。');
        const expected = targetExpected ?? expectedShopVersion ?? 1;
        if (shop.version !== expected || photo !== null) await persistShop(connection, shop, expected);
      }
      await persistMention(connection, mention, decision.expected_version);
      if (deleteSource !== null) await connection.execute('DELETE FROM shops WHERE id=$1', [deleteSource]);
      if (decision.action === 'reject' && sourceId !== null) {
        const count = (await connection.query('SELECT COUNT(*) AS count FROM shop_mentions WHERE shop_id=$1', countRow, [sourceId]))[0]?.count;
        if (count === 0) await connection.execute('DELETE FROM shops WHERE id=$1', [sourceId]);
      }
      await event(connection, mention, decision.scope, decision.action, sourceId, mention.shop_id, note);
      const automatic = decision.scope === 'identity' && mention.review_status === 'approved' && shop ? await autoResolvePending(connection, mention.id, shop) : [];
      return { mention_id: mention.id, scope: decision.scope, review_status: mention.review_status, metadata_review_status: mention.metadata_review_status, shop_id: mention.shop_id, version: mention.version, automatically_resolved_count: automatic.length, automatically_resolved_mention_ids: automatic, shop: mention.shop_id !== null ? shop : null };
    });
  }
}
export function registerDecisionRoutes(app: Hono<WebEnvironment>, database: Database, config: Config): void {
  const decisions = new ReviewDecisions(database, config);
  app.post('/api/admin/reviews/:id/decision', async context => {
    requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token'), config));
    return context.json(await decisions.apply(z.coerce.number().int().positive().max(Number.MAX_SAFE_INTEGER).parse(context.req.param('id')), reviewDecisionSchema.parse(await context.req.json())));
  });
}
