import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { discordPayloadSchema, restoredAssets, saveRestoreOutcome } from '../src/operations/restore.js';
import { assetRecord, mentionRecord } from '../src/records.js';
import { fixture } from './helpers.js';

test('source restoration preserves extracted text and review history, and records unavailable sources', async () => {
  const { database } = await fixture();
  try {
    const id = '90000000000000001'; const url = 'https://x.com/example/status/1234567890123456789'; const now = new Date().toISOString();
    await database.execute("INSERT INTO source_assets(message_id,kind,url,fetch_status,extracted_text,created_at) VALUES($1,'link',$2,'unavailable',$3,$4)", [id, url, '解析済み\n原文', now]);
    await database.execute("UPDATE shop_mentions SET difference_type='source_unavailable' WHERE message_id=$1", [id]);
    const payload = discordPayloadSchema.parse({ id, channel_id: '123456789012345678', content: url, timestamp: now, embeds: [{ url, title: '店舗情報' }], attachments: [] });
    assert.equal(restoredAssets(payload).length, 2);
    await database.transaction(connection => saveRestoreOutcome(connection, id, { payload, error: null, http_status: 200 }, 12));
    const restored = await database.query('SELECT * FROM source_assets WHERE message_id=$1 ORDER BY id', assetRecord, [id]);
    assert.equal(restored[0]?.extracted_text, '解析済み\n原文'); assert.equal(restored[0]?.fetch_status, 'available'); assert.equal(restored.length, 2);
    assert.equal((await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [id]))[0]?.difference_type, 'source_restored');
    await database.transaction(connection => saveRestoreOutcome(connection, id, { payload: null, error: 'source_unavailable http_status=404', http_status: 404 }, 15));
    const mention = (await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [id]))[0];
    assert.equal(mention?.shop_id, 1); assert.equal(mention?.review_status, 'pending');
    assert.deepEqual((await database.query('SELECT status FROM processing_runs WHERE message_id=$1 ORDER BY id', z.object({ status: z.string() }), [id])).map(row => row.status), ['succeeded', 'failed']);
  } finally { await database.close(); }
});
