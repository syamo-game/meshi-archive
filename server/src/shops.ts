import { z } from 'zod';
import { annotationUpdateSchema } from '@meshi/contracts';
import type { Shop, ShopDetail, ShopPage } from '@meshi/contracts';
import type { Config } from './config.js';
import type { Database, Executor, SqlValue } from './db/database.js';
import { countRow, timestampColumn } from './db/database.js';
import { AppError } from './errors.js';
import { areaLabel, areaMatches, areas, areaSort, canonicalArea } from './geography.js';
import { googleMapsUrl, safeHttpUrl, sourceLabel, tabelogIdentity, urlIdentity } from './links.js';
import { normalizeExternalIdentity } from './resolution.js';

const shopRow = z.object({ id: z.number().int(), version: z.number().int(), shop_name: z.string(), branch_name: z.string().nullable(), area: z.string().nullable(), category: z.string().nullable(), rating: z.number().nullable(), memo: z.string().nullable(), image_key: z.string().nullable(), address: z.string().nullable(), phone: z.string().nullable(), canonical_url: z.string().nullable(), visited_at: timestampColumn.nullable(), external_source: z.string().nullable(), external_id: z.string().nullable() });
export type StoredShop = z.infer<typeof shopRow>;
const mentionRow = z.object({ id: z.number(), shop_id: z.number(), message_id: z.string(), source_url: z.string().nullable(), extraction_source: z.string() });
const assetRow = z.object({ shop_id: z.number(), url: z.string() });
const areaRow = z.object({ area: z.string().nullable(), count: z.coerce.number() });
const categoryRow = z.object({ category: z.string() });
const publicPredicate = "EXISTS(SELECT 1 FROM shop_mentions public_m WHERE public_m.shop_id=s.id AND public_m.review_status='approved' AND public_m.metadata_review_status='approved')";
const orderMap = { created_at_desc: 's.created_at DESC,s.id DESC', name_asc: 's.shop_name ASC,s.id ASC', name_desc: 's.shop_name DESC,s.id DESC', area_asc: 's.area ASC,s.id ASC', area_desc: 's.area DESC,s.id DESC', rating_desc: 's.rating DESC NULLS LAST,s.id DESC', rating_asc: 's.rating ASC NULLS LAST,s.id ASC', area_order: '' } as const;
export const listQuerySchema = z.object({ page: z.coerce.number().int().min(1).default(1), q: z.string().max(500).default(''), area: z.string().max(255).default(''), category: z.string().max(255).default(''), status: z.enum(['all', 'visited', 'unvisited']).default('all'), sort: z.enum(Object.keys(orderMap) as [keyof typeof orderMap, ...(keyof typeof orderMap)[]]).default('created_at_desc') });
export type ListQuery = z.infer<typeof listQuerySchema>;
function compareArea(left: string, right: string): number {
  const a = areaSort(left); const b = areaSort(right);
  return a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0);
}
export class Shops {
  constructor(readonly database: Database, private readonly config: Config) {}
  async find(id: number, admin: boolean, connection: Executor = this.database): Promise<StoredShop> {
    let rows = await connection.query(`SELECT s.* FROM shops s WHERE s.id=$1${admin ? '' : ' AND ' + publicPredicate}`, shopRow, [id]);
    if (!rows.length) rows = await connection.query(`SELECT s.* FROM shops s JOIN shop_redirects redirect ON redirect.target_shop_id=s.id WHERE redirect.source_shop_id=$1${admin ? '' : ' AND ' + publicPredicate}`, shopRow, [id]);
    const shop = rows[0];
    if (!shop) throw new AppError(404, '店舗が見つかりません。');
    return shop;
  }
  private async decorate(shops: readonly StoredShop[], connection: Executor): Promise<ShopDetail[]> {
    if (!shops.length) return [];
    const ids = shops.map(shop => shop.id); const placeholders = ids.map((_, index) => '$' + (index + 1)).join(',');
    const mentions = await connection.query(`SELECT id,shop_id,message_id,source_url,extraction_source FROM shop_mentions WHERE shop_id IN (${placeholders}) AND review_status='approved' AND metadata_review_status='approved' ORDER BY id`, mentionRow, ids);
    const assets = await connection.query(`SELECT m.shop_id,a.url FROM source_assets a JOIN shop_mentions m ON m.message_id=a.message_id WHERE m.shop_id IN (${placeholders}) AND m.review_status='approved' AND m.metadata_review_status='approved' AND a.kind IN ('link','embed') ORDER BY m.id,a.id`, assetRow, ids);
    return shops.map(shop => {
      const matching = mentions.filter(mention => mention.shop_id === shop.id);
      const first = matching[0];
      if (first?.extraction_source === 'legacy_import' && first.source_url) {
        const reparsed = matching.find(mention => mention.source_url === first.source_url && mention.extraction_source !== 'legacy_import');
        if (reparsed) { matching.splice(matching.indexOf(reparsed), 1); matching.unshift(reparsed); }
      }
      let source: string | null = null;
      for (const mention of matching) {
        if (!mention.source_url) continue;
        source = safeHttpUrl(mention.source_url);
        if (source) break;
        console.error('Unsafe public source URL omitted', { shopId: shop.id, mentionId: mention.id });
      }
      let canonical = safeHttpUrl(shop.canonical_url);
      if (shop.canonical_url && !canonical) console.error('Unsafe canonical URL omitted', { shopId: shop.id });
      if (!source) {
        const expected = new Set<string>();
        const canonicalId = tabelogIdentity(canonical);
        if (canonicalId) expected.add(canonicalId);
        const explicit = normalizeExternalIdentity(shop.external_source, shop.external_id);
        if (explicit?.[0] === 'tabelog') expected.add(explicit[1]);
        if (expected.size === 1) {
          source = assets.filter(asset => asset.shop_id === shop.id).map(asset => safeHttpUrl(asset.url)).find(url => url !== null && expected.has(tabelogIdentity(url) ?? '')) ?? null;
          if (source && tabelogIdentity(source) === canonicalId) canonical = null;
        } else if (expected.size > 1) console.error('Conflicting Tabelog identities omitted', { shopId: shop.id });
      }
      if (source && canonical && urlIdentity(source) === urlIdentity(canonical)) canonical = null;
      const query = [shop.shop_name, shop.branch_name, shop.address ?? shop.area].filter((part): part is string => Boolean(part?.trim())).map(part => part.trim()).join(' ');
      return { id: shop.id, version: shop.version, name: shop.shop_name, branch: shop.branch_name, area: shop.area, area_label: areaLabel(shop.area, true), category: shop.category, rating: shop.rating, memo: shop.memo ?? '', image_url: shop.image_key ? `/media/shop-uploads/${shop.image_key}.webp` : null, maps_url: 'https://www.google.com/maps/search/?' + new URLSearchParams({ api: '1', query }).toString(), source_url: googleMapsUrl(source) ? null : source, source_label: source ? sourceLabel(source) : null, canonical_url: canonical, discord_url: this.config.guildId && this.config.channelId && matching[0] ? `https://discord.com/channels/${this.config.guildId}/${this.config.channelId}/${matching[0].message_id}` : null, address: shop.address, phone: shop.phone, visited_at: shop.visited_at };
    });
  }
  async detail(id: number, admin: boolean, connection: Executor = this.database): Promise<ShopDetail> {
    const result = (await this.decorate([await this.find(id, admin, connection)], connection))[0];
    if (!result) throw new Error(`Shop detail was not produced: shop_id=${id}`);
    return result;
  }
  async selection(query: ListQuery, publicOnly: boolean): Promise<{ predicate: string; order: string; values: SqlValue[] }> {
    const areaValues = (await this.database.query('SELECT DISTINCT area,0 AS count FROM shops WHERE area IS NOT NULL', areaRow)).flatMap(row => row.area ? [row.area] : []);
    const values: SqlValue[] = [];
    const bind = (value: SqlValue): string => { values.push(value); return '$' + values.length; };
    const where = [publicOnly ? publicPredicate : '1=1'];
    for (const term of query.q.trim().split(/\s+/u).filter(Boolean)) {
      const pattern = '%' + term.toLowerCase().replace(/[\\%_]/g, value => '\\' + value) + '%';
      const placeholder = bind(pattern);
      const matches = areaValues.filter(value => [value, areaLabel(value), areas.get(canonicalArea(value) ?? value)?.group ?? ''].some(label => label.toLowerCase().includes(term.toLowerCase())));
      const parts = ['shop_name', 'branch_name', 'area', 'category', 'address', 'memo'].map(field => `lower(COALESCE(s.${field},'')) LIKE ${placeholder} ESCAPE '\\'`);
      if (matches.length) parts.push('s.area IN (' + matches.map(bind).join(',') + ')');
      where.push('(' + parts.join(' OR ') + ')');
    }
    if (query.area === '__none__') where.push('s.area IS NULL');
    else if (query.area) {
      const selected = areaValues.filter(value => areaMatches(value, query.area));
      where.push(selected.length ? 's.area IN (' + selected.map(bind).join(',') + ')' : '1=0');
    }
    if (query.category) where.push('s.category=' + bind(query.category));
    if (query.status === 'visited') where.push('s.rating>0');
    if (query.status === 'unvisited') where.push('(s.rating IS NULL OR s.rating=0)');
    const predicate = where.join(' AND ');
    let order: string = orderMap[query.sort];
    if (query.sort === 'area_order') {
      const ordered = [...areaValues].sort(compareArea);
      order = 'CASE ' + ordered.map((area, index) => `WHEN s.area=${bind(area)} THEN ${index}`).join(' ') + ` ELSE ${ordered.length} END,s.shop_name ASC,s.id ASC`;
      if (!ordered.length) order = 's.shop_name ASC,s.id ASC';
    }
    return { predicate, order, values };
  }
  async list(query: ListQuery): Promise<ShopPage> {
    const globalAreas = await this.database.query(`SELECT s.area,COUNT(s.id) AS count FROM shops s WHERE ${publicPredicate} GROUP BY s.area`, areaRow);
    const { predicate, order, values } = await this.selection(query, true);
    // Sort parameters can appear only in ORDER BY, so count with the same subquery.
    const total = (await this.database.query(`SELECT COUNT(*) AS count FROM (SELECT s.id FROM shops s WHERE ${predicate} ORDER BY ${order}) selected`, countRow, values))[0]?.count ?? 0;
    const pages = Math.max(1, Math.ceil(total / 24)); const page = Math.min(query.page, pages);
    const limit = '$' + (values.length + 1); const offset = '$' + (values.length + 2);
    const rows = await this.database.query(`SELECT s.* FROM shops s WHERE ${predicate} ORDER BY ${order} LIMIT ${limit} OFFSET ${offset}`, shopRow, [...values, 24, (page - 1) * 24]);
    const categories = await this.database.query(`SELECT DISTINCT s.category FROM shops s WHERE s.category IS NOT NULL AND s.category<>'' AND ${publicPredicate} ORDER BY s.category`, categoryRow);
    const options = globalAreas.flatMap(row => row.area ? [{ value: row.area, label: areaLabel(row.area), count: row.count }] : []).sort((a, b) => b.count - a.count || (a.label < b.label ? -1 : a.label > b.label ? 1 : 0));
    const details = await this.decorate(rows, this.database);
    const items: Shop[] = details.map(({ id, version, name, branch, area, area_label, category, rating, memo, image_url, maps_url, source_url, source_label }) => ({ id, version, name, branch, area, area_label, category, rating, memo, image_url, maps_url, source_url, source_label }));
    return { items, total, page, pages, per_page: 24, areas: options, categories: categories.map(row => row.category) };
  }
  async annotate(id: number, input: z.infer<typeof annotationUpdateSchema>, admin: boolean): Promise<ShopDetail> {
    const body = annotationUpdateSchema.parse(input);
    return this.database.transaction(async connection => {
      const shop = await this.find(id, admin, connection);
      const values: SqlValue[] = [shop.id, body.version, new Date().toISOString()];
      const fields = ['version=version+1', 'updated_at=$3'];
      if (body.rating !== undefined) { values.push(body.rating, body.rating !== null); fields.push('rating=$4', 'is_visited=$5'); }
      if (body.memo !== undefined) { values.push(body.memo); fields.push('memo=$' + values.length); }
      const changed = await connection.execute(`UPDATE shops SET ${fields.join(',')} WHERE id=$1 AND version=$2`, values);
      if (!changed) throw new AppError(409, '他のユーザーが更新しました。最新情報を確認してください。');
      return this.detail(shop.id, admin, connection);
    });
  }
}
