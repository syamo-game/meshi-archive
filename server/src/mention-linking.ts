import type { Hono } from 'hono';
import { z } from 'zod';
import type { WebEnvironment } from './app.js';
import { requireAdmin, requireWrite } from './auth.js';
import type { Config } from './config.js';
import type { Database, Executor } from './db/database.js';
import { countRow } from './db/database.js';
import { AppError } from './errors.js';
import { mentionRecord, shopRecord } from './records.js';
import type { ShopRecord } from './records.js';
import type { ReviewDecisionResult } from './review-decisions.js';
import { lockShopCreation } from './shop-creation.js';

const positive = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
export const linkRequest = z.strictObject({ expected_version: positive, source_shop_id: positive.nullable(), source_shop_version: positive.nullable(), target_shop_id: positive, target_shop_version: positive, note: z.string().trim().min(1).max(2000) }).refine(value => (value.source_shop_id === null) === (value.source_shop_version === null), '元店舗のIDと更新番号を一緒に指定してください。');
type LinkRequest = z.infer<typeof linkRequest>;
interface LinkPreview { mention_id: number; expected_version: number; source_shop: ShopRecord | null; target_shop: ShopRecord; source_mention_count: number; target_mention_count: number; review_status: string; metadata_review_status: string; source_will_be_empty: boolean; }
export class MentionLinking {
  constructor(private readonly database: Database) {}
  async shop(id: number, connection: Executor = this.database): Promise<ShopRecord> {
    const shop = (await connection.query('SELECT * FROM shops WHERE id=$1', shopRecord, [id]))[0];
    if (!shop) throw new AppError(404, '店舗が見つかりません。'); return shop;
  }
  async preview(id: number, target: number): Promise<LinkPreview> {
    const mention = (await this.database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [id]))[0];
    if (!mention) throw new AppError(404, '確認項目が見つかりません。');
    if (mention.shop_id === target) throw new AppError(409, 'この投稿は、指定した店舗に関連付け済みです。');
    const destination = await this.shop(target); const source = mention.shop_id !== null ? await this.shop(mention.shop_id) : null;
    const count = async (shopId: number): Promise<number> => (await this.database.query('SELECT COUNT(*) AS count FROM shop_mentions WHERE shop_id=$1', countRow, [shopId]))[0]?.count ?? 0;
    const sourceCount = source ? await count(source.id) : 0;
    return { mention_id: id, expected_version: mention.version, source_shop: source, target_shop: destination, source_mention_count: sourceCount, target_mention_count: await count(destination.id), review_status: mention.review_status, metadata_review_status: mention.metadata_review_status, source_will_be_empty: source !== null && sourceCount === 1 };
  }
  async link(id: number, input: LinkRequest): Promise<ReviewDecisionResult & { source_shop: ShopRecord | null }> {
    return this.database.transaction(async connection => {
      await lockShopCreation(connection); const suffix = connection.dialect === 'postgres' ? ' FOR UPDATE' : '';
      const mention = (await connection.query('SELECT * FROM shop_mentions WHERE id=$1' + suffix, mentionRecord, [id]))[0];
      if (!mention) throw new AppError(404, '確認項目が見つかりません。');
      if (mention.version !== input.expected_version || mention.shop_id !== input.source_shop_id) throw new AppError(409, '投稿の関連付けが変更されています。最新情報を確認してください。');
      if (mention.shop_id === input.target_shop_id) throw new AppError(409, 'この投稿は、指定した店舗に関連付け済みです。');
      const ids = [input.source_shop_id, input.target_shop_id].filter((value): value is number => value !== null).sort((a, b) => a - b);
      const shops = await connection.query(`SELECT * FROM shops WHERE id IN (${ids.map((_, index) => '$' + (index + 1)).join(',')}) ORDER BY id${suffix}`, shopRecord, ids);
      const target = shops.find(shop => shop.id === input.target_shop_id); const source = shops.find(shop => shop.id === input.source_shop_id) ?? null;
      if (!target || (input.source_shop_id !== null && !source)) throw new AppError(404, '店舗が見つかりません。');
      if (target.version !== input.target_shop_version || (source !== null && source.version !== input.source_shop_version)) throw new AppError(409, '店舗が更新されています。最新情報を確認してください。');
      const now = new Date().toISOString();
      const changed = await connection.execute("UPDATE shop_mentions SET shop_id=$3,review_status='approved',resolution_status='resolved',resolution_method='manual',reviewed_at=$4,version=version+1 WHERE id=$1 AND version=$2", [id, input.expected_version, target.id, now]);
      if (changed !== 1) throw new AppError(409, '確認項目が更新されています。');
      for (const shop of shops) {
        if (await connection.execute('UPDATE shops SET version=version+1,updated_at=$3 WHERE id=$1 AND version=$2', [shop.id, shop.version, now]) !== 1) throw new AppError(409, '店舗が更新されています。');
        shop.version++; shop.updated_at = now;
      }
      await connection.execute("INSERT INTO review_events(mention_id,scope,action,previous_shop_id,selected_shop_id,note,created_at) VALUES($1,'identity','link_existing',$2,$3,$4,$5)", [id, input.source_shop_id, target.id, input.note, now]);
      return { mention_id: id, scope: 'identity', review_status: 'approved', metadata_review_status: mention.metadata_review_status, shop_id: target.id, version: mention.version + 1, automatically_resolved_count: 0, automatically_resolved_mention_ids: [], shop: target, source_shop: source };
    });
  }
}
export function registerLinkRoutes(app: Hono<WebEnvironment>, database: Database, config: Config): void {
  const linking = new MentionLinking(database); const id = z.coerce.number().int().positive().max(Number.MAX_SAFE_INTEGER);
  app.get('/api/admin/reviews/:id/link-preview', async context => { requireAdmin(context.get('session')); return context.json(await linking.preview(id.parse(context.req.param('id')), id.parse(context.req.query('target_shop_id')))); });
  app.post('/api/admin/reviews/:id/link', async context => {
    requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token'), config));
    return context.json(await linking.link(id.parse(context.req.param('id')), linkRequest.parse(await context.req.json())));
  });
  app.get('/api/admin/shops/:id/merge-preview', async context => { requireAdmin(context.get('session')); return context.json(await linking.shop(id.parse(context.req.param('id')))); });
}
