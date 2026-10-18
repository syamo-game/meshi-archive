import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { detailSchema } from '@meshi/contracts';
import { mentionRecord, shopRecord } from '../src/records.js';
import { fixture, headers } from './helpers.js';

test('approval keeps identity and metadata separate and checks both versions', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    const mention = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(mention);
    const post = (body: object): Promise<Response> => app.request(`/api/admin/reviews/${mention.id}/decision`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    assert.equal((await app.request(`/api/admin/reviews/${mention.id}/decision`, { method: 'POST', headers: { ...headers(viewer), 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'approve_current', expected_version: 1, shop_version: 1 }) })).status, 403);
    assert.equal((await post({ action: 'approve_current', expected_version: 1, shop_version: 99 })).status, 409);
    const approved = await post({ action: 'approve_current', expected_version: 1, shop_version: 1 }); assert.equal(approved.status, 200);
    const item = (await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [mention.id]))[0]; assert.ok(item);
    assert.equal(item.review_status, 'approved'); assert.equal(item.metadata_review_status, 'pending'); assert.equal(item.metadata_difference_type, 'unknown_category');
    assert.equal((await app.request('/api/ui/shops/31', { headers: headers(viewer) })).status, 404);
    assert.equal((await post({ action: 'approve_current', expected_version: 1, shop_version: 1 })).status, 409);
    const metadata = await post({ action: 'edit_and_approve', scope: 'metadata', expected_version: item.version, shop_version: 1, shop: { shop_name: '店舗31', branch_name: null, area: '門前仲町', category: '丼・定食', canonical_url: null } });
    assert.equal(metadata.status, 200);
    assert.equal((await app.request('/api/ui/shops/31', { headers: headers(viewer) })).status, 200);
    const audit = await database.query('SELECT scope,action FROM review_events WHERE mention_id=$1 ORDER BY id', z.object({ scope: z.string(), action: z.string() }), [mention.id]);
    assert.deepEqual(audit, [{ scope: 'identity', action: 'approve_current' }, { scope: 'metadata', action: 'edit_and_approve' }]);
  } finally { await database.close(); }
});

test('approval automatically resolves only matching pending identities with an audit event', async () => {
  const { app, database, admin } = await fixture();
  try {
    await database.execute("UPDATE shops SET category='丼・定食' WHERE id=31");
    await database.execute("UPDATE shop_mentions SET review_status='pending',shop_id=NULL,extracted_name='店舗31',extracted_area='門前仲町' WHERE shop_id=30");
    await database.execute("UPDATE shop_mentions SET review_status='pending',shop_id=NULL,extracted_name='店舗31',extracted_area='門前仲町',extraction_error='evidence_review:synthetic' WHERE shop_id=29");
    const source = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(source);
    const response = await app.request(`/api/admin/reviews/${source.id}/decision`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'approve_current', expected_version: 1, shop_version: 1 }) });
    assert.equal(response.status, 200); const result = z.object({ automatically_resolved_count: z.number(), automatically_resolved_mention_ids: z.array(z.number()) }).parse(await response.json());
    assert.equal(result.automatically_resolved_count, 1);
    const reused = (await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, ['90000000000000030']))[0]; assert.ok(reused);
    assert.equal(reused.shop_id, 31); assert.equal(reused.review_status, 'approved'); assert.equal(reused.metadata_review_status, 'approved'); assert.equal(reused.reused_from_mention_id, source.id); assert.equal(reused.resolution_basis, 'existing_shop');
    const audit = await database.query('SELECT action FROM review_events WHERE mention_id=$1', z.object({ action: z.string() }), [reused.id]); assert.deepEqual(audit, [{ action: 'auto_reuse_approved_identity' }]);
    const manual = (await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, ['90000000000000029']))[0]; assert.equal(manual?.review_status, 'pending');
  } finally { await database.close(); }
});

test('review edits reject collisions and metadata identity changes without partial writes', async () => {
  const { app, database, admin } = await fixture();
  try {
    const source = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(source);
    const post = (body: object): Promise<Response> => app.request(`/api/admin/reviews/${source.id}/decision`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_version: 1, shop_version: 1, ...body }) });
    assert.equal((await post({ action: 'edit_and_approve', shop: { shop_name: '店舗01', area: '木場', category: '丼・定食' } })).status, 409);
    assert.equal((await post({ action: 'edit_and_approve', scope: 'metadata', shop: { shop_name: '別の店舗', area: '門前仲町', category: '丼・定食' } })).status, 400);
    assert.equal((await database.query('SELECT * FROM shops WHERE id=31', shopRecord))[0]?.version, 1);
    assert.equal((await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [source.id]))[0]?.version, 1);
    assert.deepEqual(await database.query('SELECT id FROM review_events', z.object({ id: z.number() })), []);
    assert.equal((await post({ action: 'defer', note: '  保留\n理由  ' })).status, 200);
    const audit = (await database.query('SELECT note FROM review_events WHERE mention_id=$1', z.object({ note: z.string().nullable() }), [source.id]))[0]; assert.equal(audit?.note, '  保留\n理由  ');
  } finally { await database.close(); }
});

test('merge moves every source mention, preserves a snapshot and creates redirects', async () => {
  const { app, database, admin } = await fixture();
  try {
    const source = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(source);
    await database.execute("INSERT INTO shop_mentions(message_id,shop_id,occurrence_index,extracted_name,resolution_status,review_status,metadata_review_status,extraction_source,version,created_at) VALUES($1,31,1,'関連投稿','resolved','approved','approved','synthetic-test',1,$2)", [source.message_id, new Date().toISOString()]);
    const memo = '  統合メモ\n\n保持  ';
    const response = await app.request(`/api/admin/reviews/${source.id}/decision`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'merge', expected_version: 1, shop_version: 1, target_shop_id: 1, target_version: 1, rating: 5, is_visited: true, visited_at: null, memo }) });
    assert.equal(response.status, 200);
    const target = (await database.query('SELECT * FROM shops WHERE id=1', shopRecord))[0]; assert.equal(target?.memo, memo); assert.equal(target?.rating, 5); assert.equal(target?.version, 2);
    assert.deepEqual(await database.query('SELECT * FROM shops WHERE id=31', shopRecord), []);
    const moved = await database.query('SELECT * FROM shop_mentions WHERE message_id=$1 ORDER BY id', mentionRecord, [source.message_id]); assert.equal(moved.length, 2); assert.ok(moved.every(mention => mention.shop_id === 1 && mention.version === 2));
    const redirect = detailSchema.parse(await (await app.request('/api/ui/shops/31', { headers: headers(admin) })).json()); assert.equal(redirect.id, 1);
    const audit = await database.query('SELECT action,note FROM review_events WHERE mention_id IN ($1,$2) ORDER BY id', z.object({ action: z.string(), note: z.string() }), moved.map(mention => mention.id));
    assert.deepEqual(audit.map(event => event.action), ['merge_related', 'merge']);
    const snapshot = z.object({ kind: z.literal('shop_merge_snapshot'), shops: z.array(shopRecord), moved_mentions: z.array(z.object({ id: z.number() })) }).parse(JSON.parse(audit[0]?.note ?? 'null')); assert.equal(snapshot.shops.length, 2); assert.equal(snapshot.moved_mentions.length, 2);
  } finally { await database.close(); }
});

test('audit failures roll back approvals and automatic reuse', async () => {
  const { app, database, admin } = await fixture();
  try {
    const source = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(source);
    await database.script("CREATE TRIGGER reject_decision_audit BEFORE INSERT ON review_events BEGIN SELECT RAISE(ABORT,'synthetic decision audit failure'); END");
    const response = await app.request(`/api/admin/reviews/${source.id}/decision`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'approve_current', expected_version: 1, shop_version: 1 }) });
    assert.equal(response.status, 503);
    const after = (await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [source.id]))[0]; assert.equal(after?.review_status, 'pending'); assert.equal(after?.version, 1);
  } finally { await database.close(); }
});

test('exclude preserves the shop while reject removes only an orphan', async () => {
  for (const action of ['exclude', 'reject'] as const) {
    const { app, database, admin } = await fixture();
    try {
      const source = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(source);
      const response = await app.request(`/api/admin/reviews/${source.id}/decision`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ action, expected_version: 1, shop_version: 1, reason: '確認した理由' }) });
      assert.equal(response.status, 200);
      const mention = (await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [source.id]))[0]; assert.equal(mention?.review_status, 'rejected');
      assert.equal((await database.query('SELECT * FROM shops WHERE id=31', shopRecord)).length, action === 'exclude' ? 1 : 0);
      assert.equal(mention?.shop_id, action === 'exclude' ? 31 : null);
    } finally { await database.close(); }
  }
});
