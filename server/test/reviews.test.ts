import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { parse } from 'csv-parse/sync';
import { z } from 'zod';
import { failuresSchema, reviewSchema, reviewsSchema } from '@meshi/contracts';
import { createApp } from '../src/app.js';
import { candidateRecord, mentionRecord, statusMatches } from '../src/records.js';
import { reviewGroupKeys, reviewGroups } from '../src/review-groups.js';
import { fixture, headers } from './helpers.js';

const referenceSchema = z.object({ reference: z.literal('c790aa246f6ff9ce2aec7fc6a2d9e8fe16c4184d'), synthetic: z.literal(true), groups: z.array(z.object({ name: z.string(), mentions: z.array(mentionRecord), candidates: z.array(candidateRecord), keys: z.array(z.object({ id: z.number(), values: z.array(z.tuple([z.string(), z.string()])) })), index: z.array(z.object({ id: z.number(), key: z.string(), reason: z.string(), mention_ids: z.array(z.number()) })) })), statuses: z.array(z.object({ mention: mentionRecord, expected: z.object({ all: z.array(z.string()), identity: z.array(z.string()), metadata: z.array(z.string()) }) })) });
test('review groups and statuses match the Python reference without Python at runtime', async () => {
  const reference = referenceSchema.parse(JSON.parse(await readFile(new URL('./review-parity.json', import.meta.url), 'utf8')));
  for (const scenario of reference.groups) {
    for (const mention of scenario.mentions) assert.deepEqual(reviewGroupKeys(mention, scenario.candidates.filter(candidate => candidate.mention_id === mention.id)), scenario.keys.find(item => item.id === mention.id)?.values, scenario.name);
    const actual = [...reviewGroups(scenario.mentions, scenario.candidates)].map(([id, group]) => ({ id, ...group }));
    assert.deepEqual(actual, scenario.index, scenario.name);
  }
  for (const scenario of reference.statuses) for (const scope of ['all', 'identity', 'metadata'] as const) {
    const statuses = (['pending', 'approved', 'deferred', 'rejected'] as const).filter(status => statusMatches(scenario.mention, status, scope));
    assert.deepEqual(statuses, scenario.expected[scope], `mention=${scenario.mention.id} scope=${scope}`);
  }
});

test('review queue protects access and keeps global counters independent of filters', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    assert.equal((await app.request('/api/admin/reviews', { headers: headers(viewer) })).status, 403);
    await database.execute("UPDATE shop_mentions SET review_status='approved',metadata_review_status='pending' WHERE shop_id=2");
    await database.execute("UPDATE shop_mentions SET review_status='deferred' WHERE shop_id=3");
    const response = await app.request('/api/admin/reviews?status=unresolved&limit=1', { headers: headers(admin) });
    const first = reviewsSchema.parse(await response.json()); assert.equal(response.status, 200);
    assert.equal(first.total_count, 3); assert.equal(first.items.length, 1); assert.equal(first.items[0]?.shop?.id, 2); assert.ok(first.next_cursor);
    assert.deepEqual(first.counters, { pending: 2, deferred: 1, approved: 28, rejected: 0, source_unavailable: 0, failed: 0, unresolved: 3 });
    const next = reviewsSchema.parse(await (await app.request(`/api/admin/reviews?status=unresolved&limit=1&cursor=${first.next_cursor}`, { headers: headers(admin) })).json());
    assert.equal(next.total_count, 3); assert.equal(next.items[0]?.shop?.id, 3); assert.deepEqual(next.counters, first.counters);
    const filtered = reviewsSchema.parse(await (await app.request('/api/admin/reviews?status=unresolved&q=店舗31', { headers: headers(admin) })).json());
    assert.equal(filtered.total_count, 1); assert.equal(filtered.items[0]?.shop?.id, 31); assert.deepEqual(filtered.counters, first.counters);
    assert.equal((await app.request('/api/admin/reviews?scope=metadata&status=rejected', { headers: headers(admin) })).status, 400);
  } finally { await database.close(); }
});

test('review detail keeps exact evidence, category issues, current shop and safe photo choices', async () => {
  const { app, database, admin } = await fixture();
  try {
    await database.execute('UPDATE shops SET area=NULL,category=NULL WHERE id=31');
    await database.execute('UPDATE messages SET content=$1,fetch_error=$2 WHERE message_id=$3', ['  本文\n\n末尾  ', 'synthetic fetch error', '90000000000000031']);
    const mention = (await database.query('SELECT id FROM shop_mentions WHERE shop_id=31', z.object({ id: z.number() })))[0]; assert.ok(mention);
    const response = await app.request('/api/admin/reviews/' + mention.id, { headers: headers(admin) });
    const item = reviewSchema.parse(await response.json()); assert.equal(response.status, 200);
    assert.equal(item.message_content, '  本文\n\n末尾  ');
    assert.deepEqual(item.issues.map(issue => issue.code), ['identity_review', 'missing_area', 'missing_category', 'source_unavailable']);
    assert.equal(item.shop?.area, null);
    assert.equal((await app.request('/api/admin/reviews/999999', { headers: headers(admin) })).status, 404);
  } finally { await database.close(); }
});

test('failed posts can be prepared once with a real review event and intact source', async () => {
  const { app, database, admin, config } = await fixture();
  const messageId = '90000000000000100';
  try {
    await database.execute("INSERT INTO messages(message_id,content,is_target,processing_status) VALUES($1,$2,$3,'failed')", [messageId, '  手動確認の原文\n\n保持  ', true]);
    const failures = failuresSchema.parse(await (await app.request('/api/admin/review-failures?active_only=true', { headers: headers(admin) })).json());
    assert.equal(failures.items.length, 1); assert.equal(failures.items[0]?.recovery_status, 'not_prepared');
    const path = `/api/admin/review-failures/${messageId}/prepare`;
    const first = await app.request(path, { method: 'POST', headers: headers(admin) });
    assert.equal(first.status, 200); const prepared = reviewSchema.parse(await first.json());
    assert.equal(prepared.message_content, '  手動確認の原文\n\n保持  '); assert.equal(prepared.extracted_name, '（店舗名未特定）'); assert.equal(prepared.shop, null);
    const second = reviewSchema.parse(await (await app.request(path, { method: 'POST', headers: headers(admin) })).json()); assert.equal(second.id, prepared.id);
    const events = await database.query('SELECT action FROM review_events WHERE mention_id=$1', z.object({ action: z.string() }), [prepared.id]); assert.deepEqual(events, [{ action: 'prepare_failed' }]);
    const readonly = createApp(database, { ...config, readOnly: true });
    assert.equal((await readonly.request(path, { method: 'POST', headers: headers(admin) })).status, 403);
    await database.execute("UPDATE messages SET processing_status='succeeded' WHERE message_id=$1", [messageId]);
    assert.equal((await app.request(path, { method: 'POST', headers: headers(admin) })).status, 409);
  } finally { await database.close(); }
});

test('review preparation audit failure rolls back the new mention', async () => {
  const { app, database, admin } = await fixture(); const messageId = '90000000000000101';
  try {
    await database.execute("INSERT INTO messages(message_id,is_target,processing_status) VALUES($1,$2,'failed')", [messageId, true]);
    await database.script("CREATE TRIGGER reject_review_audit BEFORE INSERT ON review_events BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END");
    assert.equal((await app.request(`/api/admin/review-failures/${messageId}/prepare`, { method: 'POST', headers: headers(admin) })).status, 503);
    assert.deepEqual(await database.query('SELECT id FROM shop_mentions WHERE message_id=$1', z.object({ id: z.number() }), [messageId]), []);
  } finally { await database.close(); }
});

test('review audit covers unlinked mentions and redacts signed URLs and CSV formulas', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    const messageId = '90000000000000031';
    await database.execute('UPDATE messages SET content=$1 WHERE message_id=$2', [' =FORMULA\n[Attachment] https://cdn.discordapp.com/attachments/a/b/photo.png?ex=synthetic&hm=synthetic\nhttps://example.com/public?normal=value\nhttps://example.com/file?token=synthetic-token', messageId]);
    await database.execute('UPDATE shop_mentions SET shop_id=NULL WHERE message_id=$1', [messageId]);
    assert.equal((await app.request('/admin/review/audit.csv', { headers: headers(viewer) })).status, 403);
    const response = await app.request('/admin/review/audit.csv', { headers: headers(admin) }); const content = new Uint8Array(await response.arrayBuffer());
    assert.equal(response.status, 200); assert.equal(response.headers.get('x-export-row-count'), '31'); assert.equal(response.headers.get('x-export-unlinked-count'), '1');
    assert.equal(response.headers.get('x-export-content-sha256'), createHash('sha256').update(content).digest('hex'));
    const text = new TextDecoder().decode(content); assert.ok(!text.includes('hm=synthetic')); assert.ok(!text.includes('token=synthetic-token')); assert.ok(text.includes('https://example.com/public?normal=value'));
    const rows = z.array(z.array(z.string())).parse(parse(content, { bom: true })); const header = rows[0] ?? []; const last = rows.at(-1);
    assert.equal(last?.[header.indexOf('shop.id')], ''); assert.equal(last?.[header.indexOf('shop.is_public')], 'false'); assert.ok(last?.[header.indexOf('message.content')]?.startsWith("' =FORMULA"));
  } finally { await database.close(); }
});
