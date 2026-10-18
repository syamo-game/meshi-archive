import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { readAISettings } from '../src/ai.js';
import { envelopeSchema } from '../src/ingestion.js';
import { RestaurantProcessor } from '../src/pipeline.js';
import { replaceSourceAssets } from '../src/pipeline-storage.js';
import { mentionRecord, messageRecord } from '../src/records.js';
import { fixture } from './helpers.js';

test('actual processor reuses one approved source without AI and keeps repeat processing idempotent', async () => {
  const { database } = await fixture();
  try {
    const source = 'https://x.com/example/status/1234567890123456789';
    const original = envelopeSchema.parse({ message_id: '90000000000000001', channel_id: '123456789012345678', content: source, created_at: '2026-10-18T00:00:00Z', assets: [{ kind: 'link', url: source }], omitted_asset_count: 0 });
    await replaceSourceAssets(database, original);
    const incoming = { ...original, message_id: '90000000000000100' };
    const processor = new RestaurantProcessor(database, readAISettings({}), '.local/no-image-fixture');
    const result = await processor.processMessage(incoming);
    assert.deepEqual(result.shop_ids, [1]); assert.equal(result.ignored, false); assert.deepEqual(result.pending_mention_ids, []);
    const rows = await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [incoming.message_id]); assert.equal(rows.length, 1); assert.equal(rows[0]?.resolution_basis, 'source_reuse');
    assert.equal((await database.query('SELECT id FROM processing_runs', z.object({ id: z.number() }))).length, 0);
    assert.deepEqual(await processor.processMessage(incoming), result);
    assert.equal((await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [incoming.message_id]))[0]?.id, rows[0]?.id);
    const results = await Promise.all([processor.processMessage(incoming), processor.processMessage(incoming)]); assert.deepEqual(results, [result, result]);
  } finally { await database.close(); }
});

test('actual processing failure retains message, source assets and stage records while rolling back mentions', async () => {
  const { database } = await fixture();
  try {
    const processor = new RestaurantProcessor(database, readAISettings({}), '.local/no-image-fixture');
    const envelope = envelopeSchema.parse({ message_id: '90000000000000101', channel_id: '123456789012345678', content: '架空食堂に行きました', created_at: '2026-10-18T00:00:00Z', assets: [{ kind: 'link', url: 'https://example.com/shop' }], omitted_asset_count: 0 });
    await assert.rejects(processor.processMessage(envelope), /OPENAI_API_KEY/);
    const message = (await database.query('SELECT * FROM messages WHERE message_id=$1', messageRecord, [envelope.message_id]))[0]; assert.equal(message?.processing_status, 'failed'); assert.match(message?.fetch_error ?? '', /OPENAI_API_KEY/);
    assert.equal((await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [envelope.message_id])).length, 0);
    assert.equal((await database.query('SELECT url FROM source_assets WHERE message_id=$1', z.object({ url: z.string() }), [envelope.message_id]))[0]?.url, 'https://example.com/shop');
    const stages = await database.query('SELECT stage FROM processing_runs WHERE message_id=$1 ORDER BY id', z.object({ stage: z.string() }), [envelope.message_id]); assert.deepEqual(stages.map(row => row.stage), ['message_extraction', 'message_pipeline']);
  } finally { await database.close(); }
});
