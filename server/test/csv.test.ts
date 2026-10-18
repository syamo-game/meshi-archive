import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { parse } from 'csv-parse/sync';
import { z } from 'zod';
import { feedbackSchema } from '@meshi/contracts';
import { createApp } from '../src/app.js';
import { csvDocument, CsvUpdates, CsvValidationError, escapeCsvValue, parseCsvDate, parseCsvUpdate } from '../src/csv.js';
import { fixture, headers } from './helpers.js';
import type { IssuedSession } from '../src/auth.js';

const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);
const rowSchema = z.array(z.array(z.string()));
const shopSchema = z.object({ version: z.number(), memo: z.string().nullable(), shop_name: z.string(), rating: z.number().nullable(), is_visited: z.union([z.number(), z.boolean()]), visited_at: z.string().nullable() });
function file(text: string): FormData { const form = new FormData(); form.set('file', new File([text], 'update.csv', { type: 'text/csv' })); return form; }
function updateCsv(rows: readonly (readonly (string | number | null)[])[]): string { return csvDocument([['_id', 'shop.version', 'message_id', 'memo'], ...rows]); }
const idFor = (id: number): string => '9' + String(id).padStart(16, '0');

test('CSV text and formula prefixes round trip without trimming', () => {
  for (const memo of ['  1行目\n\n3行目  ', '=SUM(1,2)', "'=1", "''@value", '\tformula', '-1', 'ordinary', '']) {
    const parsed = parseCsvUpdate(bytes(updateCsv([[1, 1, idFor(1), escapeCsvValue(memo)]])), '店舗.csv');
    assert.equal(parsed.rows[0]?.values.memo, memo || null);
    assert.equal(parsed.rows[0]?.messageId, idFor(1));
  }
});

test('CSV rejects duplicate IDs, unsafe URLs, invalid encoding and review columns', () => {
  assert.throws(() => parseCsvUpdate(bytes(updateCsv([[1, 1, idFor(1), 'a'], [1, 1, idFor(1), 'b']])), 'a.csv'), /重複/);
  assert.throws(() => parseCsvUpdate(bytes(csvDocument([['_id', 'shop.version', 'message_id', 'canonical_url'], [1, 1, idFor(1), 'https://name:secret@example.com/']])), 'a.csv'), /URL/);
  assert.throws(() => parseCsvUpdate(bytes('_id,shop.version,message_id,review_status\r\n1,1,90000000000000001,approved\r\n'), 'a.csv'), /審査状態/);
  assert.throws(() => parseCsvUpdate(new Uint8Array([0xff, 0xfe]), 'a.csv'), /UTF-8/);
  assert.throws(() => parseCsvUpdate(bytes('_id,shop.version,message_id,memo,memo\r\n1,1,90000000000000001,a,b'), 'a.csv'), /列名が重複/);
  assert.throws(() => parseCsvUpdate(bytes('_id,shop.version,message_id,memo\r\n1,1,9e16,a'), 'a.csv'), /17〜20桁/);
  assert.throws(() => parseCsvUpdate(bytes('_id,shop.version,message_id,memo\r\n1,1,90000000000000001,"bad'), 'a.csv'), /形式/);
});

test('CSV dates preserve offsets and fractional seconds and reject invalid dates', () => {
  assert.equal(parseCsvDate('2026-10-18'), '2026-10-18T00:00:00Z');
  assert.equal(parseCsvDate('2026-10-18T12:34:56.123456+09:00'), '2026-10-18T12:34:56.123456+09:00');
  assert.equal(parseCsvDate('2026-10-18T12:34:56+0900'), '2026-10-18T12:34:56+09:00');
  for (const value of ['2026-02-29', '2026-10-18T24:00:00Z', '2026-10-18T23:00:99Z', 'tomorrow']) assert.throws(() => parseCsvDate(value));
});

test('CSV preview and apply require the same file and consume its confirmation once', async () => {
  const { app, database, admin, viewer } = await fixture();
  const post = (action: string, text: string, session: IssuedSession = admin): Promise<Response> => app.request(`/api/ui/admin/import/${action}`, { method: 'POST', headers: headers(session), body: file(text) });
  try {
    const memo = '  CSVメモ\n\n末尾  '; const text = updateCsv([[2, 1, idFor(2), memo]]);
    assert.equal((await post('validate', text, viewer)).status, 403);
    assert.equal((await post('apply', text)).status, 409);
    const preview = await post('validate', text); assert.equal(preview.status, 200);
    const feedback = feedbackSchema.parse(await preview.json());
    assert.equal(feedback.preview?.updated, 1); assert.equal(feedback.changes[0]?.proposed, memo);
    assert.equal((await post('apply', updateCsv([[2, 1, idFor(2), '違う内容']]))).status, 409);
    const applied = await post('apply', text); assert.equal(applied.status, 200);
    assert.equal(feedbackSchema.parse(await applied.json()).message, '保存しました。');
    const shop = (await database.query('SELECT * FROM shops WHERE id=2', shopSchema))[0];
    assert.equal(shop?.memo, memo); assert.equal(shop?.version, 2);
    assert.equal((await post('apply', text)).status, 409);
  } finally { await database.close(); }
});

test('CSV stale versions and mismatched source links roll back every row', async () => {
  const { app, database, admin } = await fixture();
  try {
    const text = updateCsv([[1, 1, idFor(1), '変更1'], [2, 1, idFor(2), '変更2']]);
    assert.equal((await app.request('/api/ui/admin/import/validate', { method: 'POST', headers: headers(admin), body: file(text) })).status, 200);
    await database.execute('UPDATE shops SET memo=$1,version=version+1 WHERE id=2', ['別のユーザー']);
    const stale = await app.request('/api/ui/admin/import/apply', { method: 'POST', headers: headers(admin), body: file(text) });
    assert.equal(stale.status, 409);
    const shops = await database.query('SELECT * FROM shops WHERE id IN (1,2) ORDER BY id', shopSchema);
    assert.equal(shops[0]?.memo, null); assert.equal(shops[0]?.version, 1); assert.equal(shops[1]?.memo, '別のユーザー');
    const wrong = updateCsv([[1, 1, idFor(2), '変更']]);
    assert.equal((await app.request('/api/ui/admin/import/validate', { method: 'POST', headers: headers(admin), body: file(wrong) })).status, 400);
  } finally { await database.close(); }
});

test('CSV database failure rolls back earlier updates and confirmation consumption', async () => {
  const { app, database, admin } = await fixture();
  try {
    const text = updateCsv([[1, 1, idFor(1), '変更1'], [2, 1, idFor(2), '変更2']]);
    await app.request('/api/ui/admin/import/validate', { method: 'POST', headers: headers(admin), body: file(text) });
    await database.script("CREATE TRIGGER reject_csv_change BEFORE UPDATE ON shops WHEN NEW.id=2 BEGIN SELECT RAISE(ABORT,'synthetic database failure'); END");
    const updates = new CsvUpdates(database);
    await assert.rejects(updates.apply(parseCsvUpdate(bytes(text), 'update.csv'), admin.session), /synthetic database failure/);
    assert.equal((await database.query('SELECT * FROM shops WHERE id=1', shopSchema))[0]?.memo, null);
    await database.script('DROP TRIGGER reject_csv_change');
    assert.equal((await updates.apply(parseCsvUpdate(bytes(text), 'update.csv'), admin.session)).preview?.updated, 2);
  } finally { await database.close(); }
});

test('CSV rating controls visit status and never invents or clears a visit date', async () => {
  const { app, database, admin } = await fixture();
  try {
    await database.execute('UPDATE shops SET visited_at=$1 WHERE id=2', ['2020-01-02T03:04:05Z']);
    const text = csvDocument([['_id', 'shop.version', 'message_id', 'rating', 'status.is_visited'], [2, 1, idFor(2), 5, 'true']]);
    const post = (action: string, content: string): Promise<Response> => app.request('/api/ui/admin/import/' + action, { method: 'POST', headers: headers(admin), body: file(content) });
    assert.equal((await post('validate', text)).status, 200); assert.equal((await post('apply', text)).status, 200);
    const shop = (await database.query('SELECT * FROM shops WHERE id=2', shopSchema))[0];
    assert.equal(shop?.rating, 5); assert.equal(Boolean(shop?.is_visited), true); assert.equal(shop?.visited_at, '2020-01-02T03:04:05Z');
    assert.equal((await post('validate', csvDocument([['_id', 'shop.version', 'message_id', 'status.is_visited'], [2, 2, idFor(2), 'false']]))).status, 400);
  } finally { await database.close(); }
});

test('CSV metadata validates known values and existing external identity', async () => {
  const { database } = await fixture();
  try {
    const updates = new CsvUpdates(database);
    for (const [column, value] of [['shop.area', '未登録の駅'], ['shop.category', '架空分類'], ['shop.area', '']] as const) {
      const parsed = parseCsvUpdate(bytes(csvDocument([['_id', 'shop.version', 'message_id', column], [1, 1, idFor(1), value]])), 'a.csv');
      await assert.rejects(updates.preview(parsed), CsvValidationError);
    }
    await database.execute('UPDATE shops SET external_source=$1,external_id=$2 WHERE id=1', ['tabelog', '12345678']);
    await assert.rejects(updates.preview(parseCsvUpdate(bytes(csvDocument([['_id', 'shop.version', 'message_id', 'canonical_url'], [1, 1, idFor(1), 'https://tabelog.com/tokyo/A1301/A130101/87654321/']])), 'a.csv')), /識別子/);
  } finally { await database.close(); }
});

test('CSV preserves legacy category round trips and stores canonical aliases or unclassified values', async () => {
  const { app, database, admin } = await fixture();
  try {
    await database.execute('UPDATE shops SET category=$1 WHERE id=1', ['  寿司・回転寿司  ']);
    const csv = (version: number, category: string | null): string => csvDocument([['_id', 'shop.version', 'message_id', 'shop.category'], [1, version, idFor(1), category]]);
    const updates = new CsvUpdates(database);
    for (const value of ['  寿司・回転寿司  ', '寿司・回転寿司']) {
      const unchanged = await updates.preview(parseCsvUpdate(bytes(csv(1, value)), 'a.csv'));
      assert.equal(unchanged.preview?.updated, 0);
    }
    for (const [version, category, expected] of [[1, '鮨', '寿司'], [2, null, null]] as const) {
      const content = csv(version, category);
      for (const action of ['validate', 'apply']) assert.equal((await app.request('/api/ui/admin/import/' + action, { method: 'POST', headers: headers(admin), body: file(content) })).status, 200);
      const stored = (await database.query('SELECT category,version FROM shops WHERE id=1', z.object({ category: z.string().nullable(), version: z.number() })))[0];
      assert.equal(stored?.category, expected); assert.equal(stored?.version, version + 1);
    }
  } finally { await database.close(); }
});

test('CSV export includes all matching shops, exact memo, BOM and content checksum', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    await database.execute('UPDATE shops SET memo=$1 WHERE id=1', ['  改行\n\n保持  ']);
    assert.equal((await app.request('/export.csv', { headers: headers(viewer) })).status, 403);
    const response = await app.request('/export.csv?sort=area_order', { headers: headers(admin) });
    assert.equal(response.status, 200); assert.equal(response.headers.get('x-export-row-count'), '31');
    const content = new Uint8Array(await response.arrayBuffer());
    assert.deepEqual([...content.slice(0, 3)], [0xef, 0xbb, 0xbf]);
    assert.equal(response.headers.get('x-export-content-sha256'), createHash('sha256').update(content).digest('hex'));
    const rows = rowSchema.parse(parse(content, { bom: true })); const header = rows[0] ?? [];
    const first = rows.find(row => row[0] === '1'); assert.equal(first?.[header.indexOf('memo')], '  改行\n\n保持  ');
    assert.equal(first?.[header.indexOf('status.is_visited')], 'true');
    const filtered = await app.request('/export.csv?area=木場&status=visited', { headers: headers(admin) });
    assert.equal(filtered.headers.get('x-export-row-count'), '1');
    assert.equal((await app.request('/export.csv?sort=invalid', { headers: headers(admin) })).status, 422);
  } finally { await database.close(); }
});

test('CSV template round trips as a no-op and keeps review states unchanged', async () => {
  const { app, database, admin, config } = await fixture();
  try {
    const response = await app.request('/admin/import/update/template.csv', { headers: headers(admin) });
    const parsed = parseCsvUpdate(new Uint8Array(await response.arrayBuffer()), 'template.csv');
    assert.equal(parsed.rows.length, 31);
    const result = await new CsvUpdates(database).preview(parsed); assert.equal(result.preview?.updated, 0);
    assert.equal((await database.query('SELECT review_status FROM shop_mentions WHERE shop_id=31', z.object({ review_status: z.string() })))[0]?.review_status, 'pending');
    const readonlyApp = createApp(database, { ...config, readOnly: true });
    assert.equal((await readonlyApp.request('/api/ui/admin/import/validate', { method: 'POST', headers: headers(admin), body: file(updateCsv([[2, 1, idFor(2), 'changed']])) })).status, 403);
  } finally { await database.close(); }
});
