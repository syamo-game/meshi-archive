import type { Hono } from 'hono';
import { z } from 'zod';
import type { WebEnvironment } from './app.js';
import { requireAdmin, requireWrite } from './auth.js';
import type { Config } from './config.js';
import type { Database, SqlValue } from './db/database.js';
import { idRow } from './db/database.js';
import { AppError } from './errors.js';
import { canonicalArea } from './geography.js';
import { canonicalCategory } from './categories.js';
import { maxImageBytes, storeUploadedImage } from './images.js';
import { safeHttpUrl } from './links.js';
import { normalizePhone } from './resolution.js';
import { Shops } from './shops.js';
import type { StoredShop } from './shops.js';
import { formValue } from './users.js';

const fields = ['shop_name', 'branch_name', 'area', 'category', 'url', 'address', 'phone', 'memo', 'rating', 'visited_at'] as const;
type Field = typeof fields[number];
type Draft = Record<Field, string>;
const versionSchema = z.string().regex(/^[1-9]\d*$/).transform(Number).pipe(z.number().int().positive().max(Number.MAX_SAFE_INTEGER));
const deleteSchema = z.strictObject({ version: z.number().int().positive() });
function values(shop: StoredShop): Draft {
  return { shop_name: shop.shop_name, branch_name: shop.branch_name ?? '', area: shop.area ?? '', category: canonicalCategory(shop.category) ?? shop.category ?? '', url: shop.canonical_url ?? '', address: shop.address ?? '', phone: shop.phone ?? '', memo: shop.memo ?? '', rating: shop.rating ? String(shop.rating) : '', visited_at: shop.visited_at?.slice(0, 10) ?? '' };
}
function returnTo(value: string): string {
  const target = value || '/';
  if (!/^\/(?:\?[^\u0000-\u001f\u007f]*)?$/.test(target)) throw new AppError(422, '戻り先のURLが不正です。');
  return target;
}
function visitDate(value: string): string | null {
  if (!value) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new AppError(422, '訪問日はYYYY-MM-DD形式で指定してください。');
  const parsed = new Date(value + 'T00:00:00.000Z');
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) throw new AppError(422, '訪問日が実在する日付ではありません。');
  return parsed.toISOString();
}
export class ShopEditor {
  private readonly shops: Shops;
  constructor(private readonly database: Database, private readonly config: Config) { this.shops = new Shops(database, config); }
  async save(id: number, form: FormData): Promise<string> {
    const originalVersion = versionSchema.parse(formValue(form, 'expected_version'));
    const target = returnTo(formValue(form, 'return_to'));
    const input = Object.fromEntries(fields.map(field => [field, formValue(form, field)])) as Draft;
    await this.database.transaction(async connection => {
      const shop = await this.shops.find(id, true, connection);
      if (shop.id !== id) throw new AppError(409, '店舗が統合されています。統合先の店舗を開き直してください。');
      const current = values(shop);
      const draft = { ...input };
      let expected = originalVersion;
      if (originalVersion !== shop.version) {
        const confirmed = versionSchema.safeParse(formValue(form, 'confirmed_version'));
        if (formValue(form, 'confirm_conflict') !== 'on' || !confirmed.success || confirmed.data !== shop.version) throw new AppError(409, '他のユーザーが更新しました。最新情報を確認してください。');
        const choices = new Map<Field, 'input' | 'latest'>();
        for (const item of form.getAll('conflict_choices')) {
          if (typeof item !== 'string') throw new AppError(422, '競合の選択内容が不正です。');
          const parts = item.split(':');
          if (parts[0] === 'is_visited') continue;
          const field = z.enum(fields).parse(parts[0]); const choice = z.enum(['input', 'latest']).parse(parts[1]);
          if (choices.has(field) || parts.length !== 2) throw new AppError(422, '競合の選択内容が重複しています。');
          choices.set(field, choice);
        }
        for (const field of fields) {
          if (draft[field] === current[field]) continue;
          const choice = choices.get(field);
          if (!choice) throw new AppError(409, '変更する項目ごとに、入力と最新のどちらを保存するか選択してください。');
          if (choice === 'latest') draft[field] = current[field];
        }
        expected = confirmed.data;
      }
      const name = z.string().trim().min(1, '店名を入力してください。').max(500).parse(draft.shop_name);
      const branch = z.string().trim().max(255).parse(draft.branch_name) || null;
      const area = draft.area === current.area ? shop.area : canonicalArea(draft.area);
      if (draft.area.trim() && !area) throw new AppError(422, '候補にある市区町村・エリアを選択してください。');
      const rawCategory = z.string().trim().max(255).parse(draft.category) || null;
      const normalizedCategory = canonicalCategory(rawCategory);
      const unchangedCategory = rawCategory === shop.category?.trim();
      const category = normalizedCategory ?? (rawCategory && unchangedCategory ? shop.category : rawCategory);
      if (rawCategory && !unchangedCategory && !normalizedCategory) throw new AppError(422, 'カテゴリは候補から選択してください。');
      const url = draft.url.trim() ? safeHttpUrl(draft.url) : null;
      if (draft.url.trim() && !url) throw new AppError(422, 'http:// または https:// で始まる有効なURLを指定してください。');
      const address = z.string().trim().max(2000).parse(draft.address) || null;
      const phone = normalizePhone(z.string().trim().max(64).parse(draft.phone));
      if (phone && phone.length > 32) throw new AppError(422, '電話番号が長すぎます。');
      const memo = z.string().max(20000).parse(draft.memo) || null;
      const rating = draft.rating ? z.enum(['1', '2', '3', '4', '5']).transform(Number).parse(draft.rating) : null;
      const visitedAt = draft.visited_at === current.visited_at ? shop.visited_at : visitDate(draft.visited_at);
      const params: SqlValue[] = [id, expected, name, branch, area, category, url, address, phone, memo, rating, rating !== null, visitedAt, new Date().toISOString()];
      const changed = await connection.execute('UPDATE shops SET shop_name=$3,branch_name=$4,area=$5,category=$6,canonical_url=$7,address=$8,phone=$9,memo=$10,rating=$11,is_visited=$12,visited_at=$13,updated_at=$14,version=version+1 WHERE id=$1 AND version=$2', params);
      if (!changed) throw new AppError(409, '他のユーザーが更新しました。最新情報を確認してください。');
      const photo = form.get('photo');
      if (photo instanceof File && photo.name) {
        if (photo.size > maxImageBytes) throw new AppError(422, '写真は20MiB以下のファイルを選択してください。');
        const uploaded = await storeUploadedImage(new Uint8Array(await photo.arrayBuffer()), this.config.imageDirectory);
        await connection.execute('UPDATE shops SET image_key=$1 WHERE id=$2', [uploaded.key, id]);
      }
      if (!area) await connection.execute("UPDATE shop_mentions SET metadata_review_status='pending',metadata_difference_type='missing_area',metadata_reviewed_at=NULL,version=version+1 WHERE shop_id=$1 AND (metadata_review_status<>'pending' OR metadata_difference_type IS NULL OR metadata_difference_type<>'missing_area' OR metadata_reviewed_at IS NOT NULL)", [id]);
    });
    return `/shop/${id}?` + new URLSearchParams({ saved: 'true', return_to: target }).toString();
  }
  async delete(id: number, version: number): Promise<void> {
    await this.database.transaction(async connection => {
      const target = await connection.query('SELECT target_shop_id AS id FROM shop_redirects WHERE target_shop_id=$1', idRow, [id]);
      if (target.length) throw new AppError(409, 'この店舗は統合先のため削除できません。');
      const shop = await this.shops.find(id, true, connection);
      if (shop.id !== id) throw new AppError(409, '店舗が統合されています。統合先を開き直してください。');
      if (await connection.execute('DELETE FROM shops WHERE id=$1 AND version=$2', [id, version]) !== 1) throw new AppError(409, '他のユーザーが更新しました。最新情報を確認してください。');
    });
  }
}
export function registerEditorRoutes(app: Hono<WebEnvironment>, database: Database, config: Config): void {
  const editor = new ShopEditor(database, config);
  const id = z.coerce.number().int().positive().max(Number.MAX_SAFE_INTEGER);
  app.post('/shop/:id/edit', async context => {
    const form = await context.req.formData();
    requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token') ?? formValue(form, 'csrf_token'), config));
    const redirect = await editor.save(id.parse(context.req.param('id')), form);
    return context.req.header('X-Requested-With') === 'XMLHttpRequest' ? context.json({ redirect_url: redirect }) : context.redirect(redirect, 302);
  });
  app.post('/api/ui/admin/shops/:id/delete', async context => {
    requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token'), config));
    const body = deleteSchema.parse(await context.req.json());
    await editor.delete(id.parse(context.req.param('id')), body.version);
    return context.json({ ok: true });
  });
}
