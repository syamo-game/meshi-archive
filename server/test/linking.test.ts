import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { linkPreviewSchema } from '@meshi/contracts';
import { mentionRecord, shopRecord } from '../src/records.js';
import { fixture, headers } from './helpers.js';

test('link preview and commit move one mention and keep both shops and annotations', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    const mention = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(mention);
    assert.equal((await app.request(`/api/admin/reviews/${mention.id}/link-preview?target_shop_id=1`, { headers: headers(viewer) })).status, 403);
    const previewResponse = await app.request(`/api/admin/reviews/${mention.id}/link-preview?target_shop_id=1`, { headers: headers(admin) });
    const preview = linkPreviewSchema.parse(await previewResponse.json()); assert.equal(preview.source_will_be_empty, true); assert.equal(preview.target_mention_count, 1);
    const before = await database.query('SELECT * FROM shops WHERE id IN (1,31) ORDER BY id', shopRecord);
    const response = await app.request(`/api/admin/reviews/${mention.id}/link`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_version: preview.expected_version, source_shop_id: preview.source_shop?.id, source_shop_version: preview.source_shop?.version, target_shop_id: preview.target_shop.id, target_shop_version: preview.target_shop.version, note: '根拠を確認したため' }) });
    assert.equal(response.status, 200);
    const after = await database.query('SELECT * FROM shops WHERE id IN (1,31) ORDER BY id', shopRecord); assert.equal(after.length, 2);
    for (const [index, shop] of after.entries()) { assert.equal(shop.version, (before[index]?.version ?? 0) + 1); assert.equal(shop.memo, before[index]?.memo); assert.equal(shop.rating, before[index]?.rating); }
    const moved = (await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [mention.id]))[0]; assert.equal(moved?.shop_id, 1); assert.equal(moved?.review_status, 'approved'); assert.equal(moved?.version, 2);
    assert.deepEqual(await database.query('SELECT action,note FROM review_events WHERE mention_id=$1', z.object({ action: z.string(), note: z.string() }), [mention.id]), [{ action: 'link_existing', note: '根拠を確認したため' }]);
  } finally { await database.close(); }
});

test('link rejects stale target, changed source, same shop and empty justification', async () => {
  const { app, database, admin } = await fixture();
  try {
    const mention = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(mention);
    const post = (changes: object): Promise<Response> => app.request(`/api/admin/reviews/${mention.id}/link`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_version: 1, source_shop_id: 31, source_shop_version: 1, target_shop_id: 1, target_shop_version: 1, note: '確認済み', ...changes }) });
    assert.equal((await post({ target_shop_version: 99 })).status, 409);
    assert.equal((await post({ source_shop_id: null, source_shop_version: null })).status, 409);
    assert.equal((await post({ target_shop_id: 31 })).status, 409);
    assert.equal((await post({ note: '  ' })).status, 422);
    assert.equal((await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [mention.id]))[0]?.version, 1);
  } finally { await database.close(); }
});
