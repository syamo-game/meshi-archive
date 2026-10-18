import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { readAISettings } from '../src/ai.js';
import { extractedMentionSchema } from '../src/ai-models.js';
import { envelopeSchema } from '../src/ingestion.js';
import { RestaurantProcessor } from '../src/pipeline.js';
import { pipelineCandidateSchema } from '../src/pipeline-candidates.js';
import { createCandidateShop, metadataReviewState, newMention, replaceSourceAssets, saveMention, storeCandidates } from '../src/pipeline-storage.js';
import { candidateRecord, mentionRecord, messageRecord } from '../src/records.js';
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

test('candidate storage normalizes true aliases and retains unknown category evidence for review', async () => {
  const { database } = await fixture();
  try {
    for (const [index, raw, expected, difference] of [[0, '鮨', '寿司', null], [1, '寿司・回転寿司', null, 'unknown_category'], [2, '架空分類', null, 'unknown_category'], [3, null, null, 'missing_category']] as const) {
      const extracted = extractedMentionSchema.parse({ shop_name: `分類店舗${index}`, area: '木場', category: raw, needs_review: false, confidence_reason: 'Synthetic category evidence' });
      const candidate = pipelineCandidateSchema.parse({ name: extracted.shop_name, area: '木場', category: raw, provenance: 'structured_data', is_verified: true });
      const shop = await createCandidateShop(database, extracted, candidate);
      assert.equal(shop.category, expected);
      const [status, reason] = metadataReviewState(shop.area, shop.category, raw);
      assert.equal(status, expected === null ? 'pending' : 'approved'); assert.equal(reason, difference);
      const row = await saveMention(database, { ...newMention('90000000000000001', index + 1, extracted, null, 'synthetic-test'), shop_id: shop.id, metadata_review_status: status, metadata_difference_type: reason });
      await storeCandidates(database, row, extracted, [candidate], candidate);
      assert.equal((await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [row.id]))[0]?.extracted_category, raw);
      assert.equal((await database.query('SELECT * FROM resolution_candidates WHERE mention_id=$1', candidateRecord, [row.id]))[0]?.category, raw);
    }
  } finally { await database.close(); }
});
