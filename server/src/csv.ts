import { createHash } from 'node:crypto';
import { basename } from 'node:path';
import type { Hono } from 'hono';
import { parse } from 'csv-parse/sync';
import { stringify } from 'csv-stringify/sync';
import { z } from 'zod';
import type { Feedback } from '@meshi/contracts';
import type { WebEnvironment } from './app.js';
import { requireAdmin, requireWrite } from './auth.js';
import type { Session } from './auth.js';
import type { Config } from './config.js';
import type { Database, Executor, SqlValue } from './db/database.js';
import { booleanColumn, idRow, timestampColumn } from './db/database.js';
import { canonicalArea } from './geography.js';
import { canonicalCategory, categories } from './categories.js';
import { safeHttpUrl, tabelogIdentity } from './links.js';
import { normalizeExternalIdentity } from './resolution.js';
import { listQuerySchema, Shops } from './shops.js';
import { formValue } from './users.js';

export const csvMaxBytes = 5 * 1024 * 1024;
const columns = { 'shop.name': 'shop_name', 'shop.branch_name': 'branch_name', 'shop.area': 'area', 'shop.category': 'category', 'shop.address': 'address', 'shop.phone': 'phone', canonical_url: 'canonical_url', 'status.is_visited': 'is_visited', visited_at: 'visited_at', rating: 'rating', memo: 'memo' } as const;
type Column = keyof typeof columns;
type Field = typeof columns[Column];
type ChangeValue = string | number | boolean | null;
type Changes = Partial<Record<Field, ChangeValue>>;
const labels: Record<Field, string> = { shop_name: '店名', branch_name: '支店', area: 'エリア', category: 'カテゴリ', address: '住所', phone: '電話番号', canonical_url: '店舗URL', is_visited: '訪問状況', visited_at: '訪問日', rating: '評価', memo: 'メモ' };
const limits: Partial<Record<Field, number>> = { shop_name: 500, branch_name: 255, area: 255, category: 255, address: 2000, phone: 32, memo: 20000 };
const required = ['_id', 'shop.version', 'message_id'] as const;
const headers = [...required, ...Object.keys(columns)];
const fields = Object.values(columns);
const formula = /^'*[=+@\-\t\r]/;
const shopRow = z.object({ id: z.number(), version: z.number(), shop_name: z.string(), branch_name: z.string().nullable(), area: z.string().nullable(), category: z.string().nullable(), address: z.string().nullable(), phone: z.string().nullable(), canonical_url: z.string().nullable(), is_visited: booleanColumn, visited_at: timestampColumn.nullable(), rating: z.number().nullable(), memo: z.string().nullable(), external_source: z.string().nullable(), external_id: z.string().nullable(), image_key: z.string().nullable(), created_at: timestampColumn, updated_at: timestampColumn });
type CsvShop = z.infer<typeof shopRow>;
const mentionRow = z.object({ id: z.number(), shop_id: z.number(), version: z.number(), message_id: z.string(), review_status: z.string(), resolution_status: z.string(), metadata_review_status: z.string(), resolution_method: z.string().nullable(), difference_type: z.string().nullable(), metadata_difference_type: z.string().nullable(), reviewed_at: timestampColumn.nullable(), metadata_reviewed_at: timestampColumn.nullable(), source_url: z.string().nullable(), extraction_source: z.string(), extraction_error: z.string().nullable(), confidence_reason: z.string().nullable() });
type CsvMention = z.infer<typeof mentionRow>;
export class CsvValidationError extends Error {
  constructor(readonly errors: readonly string[]) { super(errors.join('\n')); this.name = 'CsvValidationError'; }
}
interface CsvRow { row: number; id: number; version: number; messageId: string; values: Changes; }
export interface ParsedCsv { filename: string; hash: string; rows: readonly CsvRow[]; }
export function escapeCsvValue(value: string): string { return formula.test(value) ? "'" + value : value; }
function unescapeCsvValue(value: string): string { return value.startsWith("'") && formula.test(value) ? value.slice(1) : value; }
function positiveInteger(value: string): number {
  const normalized = value.trim();
  if (!/^[0-9]+$/.test(normalized)) throw new Error('店舗の識別情報と更新番号は正の整数にしてください。');
  const number = Number(normalized);
  if (!Number.isSafeInteger(number) || number <= 0) throw new Error('店舗の識別情報と更新番号は正の整数にしてください。');
  return number;
}
export function parseCsvDate(value: string): string | null {
  const text = value.trim();
  if (!text) return null;
  const parts = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:?\d{2})?)?$/.exec(text);
  if (!parts) throw new Error('訪問日はYYYY-MM-DD形式、またはISO形式の日時にしてください。');
  const day = `${parts[1]}-${parts[2]}-${parts[3]}`;
  const date = new Date(day + 'T00:00:00Z');
  if (Number(parts[1]) === 0 || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== day || Number(parts[4] ?? 0) > 23 || Number(parts[5] ?? 0) > 59 || Number(parts[6] ?? 0) > 59) throw new Error('訪問日が実在する日時ではありません。');
  const offset = (parts[8] ?? 'Z').replace(/^([+-]\d{2})(\d{2})$/, '$1:$2');
  const normalized = parts[4] ? day + 'T' + parts[4] + ':' + parts[5] + ':' + parts[6] + (parts[7] ? '.' + parts[7] : '') + offset : day + 'T00:00:00Z';
  if (!Number.isFinite(Date.parse(normalized))) throw new Error('訪問日の時差が不正です。');
  // Keep microseconds when present; PostgreSQL can store more than JS Date.
  return normalized;
}
function parseValue(field: Field, raw: string): ChangeValue {
  if (field === 'is_visited') {
    const value = raw.trim().toLowerCase();
    if (['true', '1', 'yes', 'on'].includes(value)) return true;
    if (['false', '0', 'no', 'off'].includes(value)) return false;
    throw new Error('訪問状況はtrueまたはfalseにしてください。');
  }
  if (field === 'rating') {
    if (!raw.trim()) return null;
    if (!/^[0-9]+$/.test(raw.trim()) || Number(raw) < 1 || Number(raw) > 5) throw new Error('評価は1〜5の整数にしてください。');
    return Number(raw);
  }
  if (field === 'visited_at') return parseCsvDate(raw);
  const text = unescapeCsvValue(raw);
  if (field === 'shop_name' && !text.trim()) throw new Error('店名を入力してください。');
  const limit = limits[field];
  if (limit && [...text].length > limit) throw new Error(`${labels[field]}は${limit.toLocaleString('en-US')}文字以内にしてください。`);
  if (field === 'canonical_url' && text && !safeHttpUrl(text)) throw new Error('店舗URLはhttpまたはhttpsで始まる有効なURLにしてください。');
  return text || null;
}
export function parseCsvUpdate(bytes: Uint8Array, filename: string): ParsedCsv {
  if (bytes.length > csvMaxBytes) throw new CsvValidationError(['CSVファイルは5MB以下にしてください。']);
  if (!filename.toLowerCase().endsWith('.csv')) throw new CsvValidationError(['CSVファイルを指定してください。']);
  let text: string;
  try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch (error) { console.warn('CSV UTF-8 decoding rejected', { filename: basename(filename), size: bytes.length, error }); throw new CsvValidationError(['UTF-8のCSVファイルを指定してください。']); }
  let source: string[][];
  try { source = z.array(z.array(z.string())).parse(parse(text, { bom: true, skip_empty_lines: true, max_record_size: csvMaxBytes })); }
  catch (error) { console.warn('CSV syntax rejected', { filename: basename(filename), size: bytes.length, error }); throw new CsvValidationError(['CSVの形式を読み取れません。CSV形式で保存し直してください。']); }
  const header = source.shift() ?? []; const errors: string[] = [];
  if (source.length > 5000) throw new CsvValidationError(['CSVは5,000行以下にしてください。']);
  if (new Set(header).size !== header.length) errors.push('同じ列名が重複しています。');
  if (required.some(field => !header.includes(field))) errors.push('店舗・投稿の識別情報が不足しています。最新のCSVをダウンロードしてください。');
  const unsupported = header.filter(field => !headers.includes(field));
  if (unsupported.length) errors.push(`差分更新では扱えない列です: ${unsupported.join(', ')}。店舗情報の列だけを残してください。審査状態は確認画面で変更してください。`);
  if (!Object.keys(columns).some(column => header.includes(column))) errors.push('変更する店舗情報の列を1つ以上含めてください。');
  if (errors.length) throw new CsvValidationError(errors);
  const rows: CsvRow[] = []; const seen = new Set<number>();
  for (const [index, sourceRow] of source.entries()) {
    const row = index + 2;
    try {
      if (sourceRow.length !== header.length) throw new Error('列数がヘッダーと一致しません。');
      const get = (column: string): string => { const value = sourceRow[header.indexOf(column)]; if (value === undefined) throw new Error(`列が見つかりません: ${column}`); return value; };
      const id = positiveInteger(get('_id')); const version = positiveInteger(get('shop.version')); const messageId = get('message_id');
      if (!/^\d{17,20}$/.test(messageId)) throw new Error('投稿の識別情報は17〜20桁の数字のままにしてください。');
      if (seen.has(id)) throw new Error('同じ店舗の行が重複しています。1店舗につき1行にしてください。');
      seen.add(id); const values: Changes = {};
      for (const column of Object.keys(columns) as Column[]) if (header.includes(column)) values[columns[column]] = parseValue(columns[column], get(column));
      rows.push({ row, id, version, messageId, values });
    } catch (error) { if (!(error instanceof Error)) throw error; errors.push(`${row}行目：${error.message}`); }
  }
  if (!rows.length && !errors.length) errors.push('CSVにデータ行がありません。');
  if (errors.length) throw new CsvValidationError(errors.slice(0, 100));
  return { filename: basename(filename.replaceAll('\\', '/')), hash: createHash('sha256').update(bytes).digest('hex').toUpperCase(), rows };
}
function changedValues(shop: CsvShop, row: CsvRow): Changes {
  const changes: Changes = {};
  for (const field of fields) {
    if (field === 'is_visited') continue;
    let value = row.values[field]; if (value === undefined) continue;
    if (field === 'area' && value !== shop.area && typeof value === 'string') value = canonicalArea(value) ?? value;
    if (field === 'category' && typeof value === 'string') value = canonicalCategory(value) ?? (shop.category !== null && value.trim() === shop.category.trim() ? shop.category : value);
    if (field === 'visited_at' && typeof value === 'string' && shop.visited_at && preciseTimestamp(value) === preciseTimestamp(shop.visited_at)) continue;
    if (value !== shop[field]) changes[field] = value;
  }
  if (changes.rating !== undefined) changes.is_visited = changes.rating !== null;
  return changes;
}
function preciseTimestamp(value: string): bigint {
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? '';
  return BigInt(Date.parse(value)) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3, 6));
}
function display(field: Field, value: ChangeValue): string {
  if (value === null || value === '') return '（空欄）';
  if (field === 'is_visited') return value ? '訪問済み' : '未訪問';
  if (field === 'rating') return `${value} / 5`;
  if (field === 'visited_at') return String(value).replace(/^(\d{4})-(\d{2})-(\d{2})/, '$1/$2/$3').replace('T', ' ');
  return String(value);
}
export class CsvUpdates {
  constructor(private readonly database: Database) {}
  async preview(parsed: ParsedCsv, connection: Executor = this.database): Promise<Feedback> {
    const errors: string[] = []; const changes: Feedback['changes'] = []; const updated = new Set<number>();
    for (const row of parsed.rows) {
      const shop = (await connection.query('SELECT * FROM shops WHERE id=$1', shopRow, [row.id]))[0];
      const prefix = `${row.row}行目：`;
      if (!shop) { errors.push(prefix + 'この店舗が見つかりません。最新のCSVをダウンロードしてください。'); continue; }
      if (shop.version !== row.version) errors.push(prefix + 'この店舗は更新済みです。最新のCSVをダウンロードして編集し直してください。');
      if (!(await connection.query('SELECT id FROM shop_mentions WHERE shop_id=$1 AND message_id=$2', idRow, [row.id, row.messageId])).length) errors.push(prefix + '店舗と投稿の識別情報が一致しません。');
      const rating = row.values.rating !== undefined ? row.values.rating : shop.rating;
      if (row.values.is_visited !== undefined && row.values.is_visited !== (rating !== null)) errors.push(prefix + '訪問状況は評価から決まります。評価の有無に合わせてください。');
      const changed = changedValues(shop, row);
      if (changed.area !== undefined && (typeof changed.area !== 'string' || !canonicalArea(changed.area))) errors.push(prefix + 'エリアは登録済みの市区町村名・駅名を入力してください。');
      if (changed.category !== undefined && changed.category !== null && (typeof changed.category !== 'string' || !categories.has(changed.category))) errors.push(prefix + 'カテゴリは登録済みの分類を指定してください。');
      if (row.values.canonical_url !== undefined) {
        const explicit = normalizeExternalIdentity(shop.external_source, shop.external_id);
        const proposedId = tabelogIdentity(typeof row.values.canonical_url === 'string' ? row.values.canonical_url : null);
        if (explicit?.[0] === 'tabelog' && proposedId && explicit[1] !== proposedId) errors.push(prefix + '店舗URLが既存の店舗識別子と一致しません。');
      }
      for (const field of fields) {
        const proposed = changed[field]; if (proposed === undefined) continue;
        updated.add(row.id);
        changes.push({ shop_id: row.id, shop_name: [shop.shop_name, shop.branch_name].filter(Boolean).join(' '), field_label: labels[field], previous: display(field, field === 'is_visited' ? shop.rating !== null : shop[field]), proposed: display(field, proposed) });
      }
    }
    if (errors.length) throw new CsvValidationError(errors.slice(0, 100));
    return { ok: true, errors: [], message: null, preview: { filename: parsed.filename, row_count: parsed.rows.length, updated: updated.size, inserted: 0, deleted: 0 }, changes };
  }
  async apply(parsed: ParsedCsv, session: Session): Promise<Feedback> {
    return this.database.transaction(async connection => {
      if (connection.dialect === 'postgres') {
        const key = createHash('sha256').update('meshi-archive:new-shop-creation').digest().readBigInt64BE().toString();
        await connection.execute('SELECT pg_advisory_xact_lock($1::bigint)', [key]);
        const ids = parsed.rows.map(row => row.id).sort((a, b) => a - b); const placeholders = ids.map((_, index) => '$' + (index + 1)).join(',');
        await connection.query(`SELECT id FROM shop_mentions WHERE shop_id IN (${placeholders}) ORDER BY id FOR UPDATE`, idRow, ids);
        await connection.query(`SELECT id FROM shops WHERE id IN (${placeholders}) ORDER BY id FOR UPDATE`, idRow, ids);
      }
      const consumed = await connection.execute('UPDATE web_sessions SET csv_update_sha256=NULL WHERE token_hash=$1 AND csv_update_sha256=$2 AND expires_at>$3', [session.stored.token_hash, parsed.hash, Date.now()]);
      if (consumed !== 1) throw new CsvValidationError(['確認したCSVと一致しません。変更内容を確認し直してください。']);
      const result = await this.preview(parsed, connection);
      for (const row of [...parsed.rows].sort((a, b) => a.id - b.id)) {
        const shop = (await connection.query('SELECT * FROM shops WHERE id=$1', shopRow, [row.id]))[0];
        if (!shop) throw new CsvValidationError([`${row.row}行目：店舗が削除されています。`]);
        const changes = changedValues(shop, row); const assignments: string[] = []; const params: SqlValue[] = [row.id, row.version, row.messageId, new Date().toISOString()];
        for (const field of fields) { const value = changes[field]; if (value !== undefined) { params.push(value); assignments.push(`${field}=$${params.length}`); } }
        if (!assignments.length) continue;
        const changed = await connection.execute(`UPDATE shops SET ${assignments.join(',')},version=version+1,updated_at=$4 WHERE id=$1 AND version=$2 AND EXISTS(SELECT 1 FROM shop_mentions WHERE shop_id=shops.id AND message_id=$3)`, params);
        if (changed !== 1) throw new CsvValidationError([`${row.row}行目：店舗が変更されたため、全行の更新を取り消しました。`]);
      }
      return { ...result, message: '保存しました。' };
    });
  }
}
function primaryMention(mentions: readonly CsvMention[]): CsvMention | undefined {
  const approved = mentions.filter(mention => mention.review_status === 'approved'); const candidates = approved.length ? approved : mentions;
  const first = candidates[0];
  return first?.extraction_source === 'legacy_import' && first.source_url ? candidates.find(mention => mention.source_url === first.source_url && mention.extraction_source !== 'legacy_import') ?? first : first;
}
export function csvDocument(rows: readonly (readonly (string | number | boolean | null)[])[]): string {
  return '\ufeff' + stringify(rows.map(row => [...row]), { record_delimiter: '\r\n', cast: { boolean: value => value ? 'true' : 'false' } });
}
function failure(error: CsvValidationError): Feedback { return { ok: false, errors: [...error.errors], message: null, preview: null, changes: [] }; }
export function registerCsvRoutes(app: Hono<WebEnvironment>, database: Database, config: Config): void {
  const updates = new CsvUpdates(database); const shops = new Shops(database, config);
  app.get('/admin/import/update/template.csv', async context => {
    requireAdmin(context.get('session'));
    const rows: (string | number | boolean | null)[][] = [headers];
    const stored = await database.query('SELECT * FROM shops ORDER BY id', shopRow);
    const mentions = await database.query('SELECT * FROM shop_mentions WHERE shop_id IS NOT NULL ORDER BY id', mentionRow);
    for (const shop of stored) {
      const mention = primaryMention(mentions.filter(item => item.shop_id === shop.id)); if (!mention) continue;
      rows.push([shop.id, shop.version, mention.message_id, ...fields.map(field => { const value = field === 'is_visited' ? shop.rating !== null : shop[field]; return typeof value === 'string' ? escapeCsvValue(value) : value; })]);
    }
    return context.body(csvDocument(rows), 200, { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="meshi_update_template.csv"' });
  });
  for (const action of ['validate', 'apply'] as const) app.post(`/api/ui/admin/import/${action}`, async context => {
    const form = await context.req.formData(); const session = requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token') ?? formValue(form, 'csrf_token'), config));
    if (action === 'validate') await database.execute('UPDATE web_sessions SET csv_update_sha256=NULL WHERE token_hash=$1', [session.stored.token_hash]);
    try {
      const file = form.get('file');
      if (!(file instanceof File)) throw new CsvValidationError(['CSVファイルを指定してください。']);
      if (file.size > csvMaxBytes) throw new CsvValidationError(['CSVファイルは5MB以下にしてください。']);
      const parsed = parseCsvUpdate(new Uint8Array(await file.arrayBuffer()), file.name);
      if (action === 'apply') return context.json(await updates.apply(parsed, session));
      const result = await updates.preview(parsed);
      await database.execute('UPDATE web_sessions SET csv_update_sha256=$1 WHERE token_hash=$2', [parsed.hash, session.stored.token_hash]);
      return context.json(result);
    } catch (error) { if (!(error instanceof CsvValidationError)) throw error; console.warn('CSV request rejected', { action, errors: error.errors }); return context.json(failure(error), action === 'validate' ? 400 : 409); }
  });
  app.get('/export.csv', async context => {
    requireAdmin(context.get('session'));
    const query = listQuerySchema.parse(context.req.query()); const selection = await shops.selection(query, false);
    const stored = await database.query(`SELECT s.* FROM shops s WHERE ${selection.predicate} ORDER BY ${selection.order}`, shopRow, selection.values);
    const mentions = await database.query('SELECT * FROM shop_mentions WHERE shop_id IS NOT NULL ORDER BY id', mentionRow);
    const rows: (string | number | boolean | null)[][] = [['_id', '@timestamp', 'message_id', 'shop.name', 'shop.branch_name', 'shop.area', 'shop.category', 'status.is_visited', 'visited_at', 'rating', 'memo', 'source_url', 'canonical_url', 'shop.address', 'shop.phone', 'shop.external_source', 'shop.external_id', 'review_status', 'resolution_status', 'metadata_review_status', 'resolution_method', 'difference_type', 'metadata_difference_type', 'reviewed_at', 'metadata_reviewed_at', 'needs_review', 'extraction_source', 'extraction_error', 'confidence_reason', 'shop.image_key', 'shop.version', 'shop.updated_at', 'mention_id', 'mention.version', 'shop.mention_count', 'shop.identity_pending_count', 'shop.identity_deferred_count', 'shop.metadata_pending_count', 'shop.metadata_deferred_count', 'shop.needs_review', 'shop.mention_versions']];
    for (const shop of stored) {
      const related = mentions.filter(item => item.shop_id === shop.id); const mention = primaryMention(related); if (!mention) continue;
      const pending = (value: string): boolean => value === 'pending' || value === 'deferred';
      rows.push([shop.id, shop.created_at, mention.message_id, shop.shop_name, shop.branch_name, shop.area, shop.category, shop.rating !== null, shop.visited_at?.slice(0, 10) ?? '', shop.rating, shop.memo, mention.source_url, shop.canonical_url, shop.address, shop.phone, shop.external_source, shop.external_id, mention.review_status, mention.resolution_status, mention.metadata_review_status, mention.resolution_method, mention.difference_type, mention.metadata_difference_type, mention.reviewed_at, mention.metadata_reviewed_at, pending(mention.review_status) || pending(mention.metadata_review_status), mention.extraction_source, mention.extraction_error, mention.confidence_reason, shop.image_key, shop.version, shop.updated_at, mention.id, mention.version, related.length, related.filter(item => item.review_status === 'pending').length, related.filter(item => item.review_status === 'deferred').length, related.filter(item => item.metadata_review_status === 'pending').length, related.filter(item => item.metadata_review_status === 'deferred').length, related.some(item => pending(item.review_status) || pending(item.metadata_review_status)), related.map(item => `${item.id}:${item.version}`).join(';')].map(value => typeof value === 'string' ? escapeCsvValue(value) : value));
    }
    const content = csvDocument(rows); const hash = createHash('sha256').update(content).digest('hex'); const now = new Date().toISOString();
    return context.body(content, 200, { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': `attachment; filename="meshi_archive_${now.replace(/[-:.]/g, '')}_${hash.slice(0, 12)}.csv"`, Pragma: 'no-cache', Expires: '0', 'X-Export-Generated-At': now, 'X-Export-Content-SHA256': hash, 'X-Export-Row-Count': String(rows.length - 1), 'X-Export-Row-Unit': 'shop', 'X-Export-Review-Scope': 'representative-mention' });
  });
}
