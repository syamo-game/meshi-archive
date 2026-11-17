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
import { fixture, withJsonServer } from './helpers.js';

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

test('verified candidate category wins only as a master value and never erases the extracted one', async () => {
  const { database } = await fixture();
  try {
    for (const [index, fromCandidate, fromMention, verified, expected] of [[0, '居酒屋、寿司、海鮮', '寿司', true, '居酒屋'], [1, '架空分類', '寿司', true, '寿司'], [2, null, '鮨', true, '寿司'], [3, 'ラーメン', '寿司', false, '寿司'], [4, '架空分類', '別の架空分類', true, null]] as const) {
      const extracted = extractedMentionSchema.parse({ shop_name: `決定順序店舗${index}`, area: '木場', category: fromMention, needs_review: false, confidence_reason: 'Synthetic category order' });
      const candidate = pipelineCandidateSchema.parse({ name: extracted.shop_name, area: '木場', category: fromCandidate, provenance: verified ? 'structured_data' : 'web_search', is_verified: verified });
      assert.equal((await createCandidateShop(database, extracted, candidate)).category, expected, `case ${index}`);
    }
  } finally { await database.close(); }
});

test('a post the extraction rules out is ignored without a web search', async () => {
  const { database } = await fixture();
  const extraction = { is_restaurant_message: false, ignore_reason: '飲食店への言及ではない雑談です。', unresolved_reason: null, mentions: [] };
  const responseBody = { id: 'resp_synthetic', object: 'response', created_at: 0, model: 'synthetic', status: 'completed', output: [{ type: 'message', id: 'msg_synthetic', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text: JSON.stringify(extraction), annotations: [] }] }], usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } } };
  const previous = process.env.OPENAI_BASE_URL;
  try {
    await withJsonServer(() => ({ status: 200, body: responseBody }), async (baseUrl, requests) => {
      process.env.OPENAI_BASE_URL = baseUrl + '/v1';
      const link = 'https://x.com/synthetic_user/status/1900000000000000002';
      const envelope = envelopeSchema.parse({ message_id: '90000000000000200', channel_id: '123456789012345678', content: link + '\n[Embed Description] 合成の雑談です', created_at: '2026-11-17T00:00:00Z', assets: [{ kind: 'link', url: link }, { kind: 'embed', url: link, description: '合成の雑談です' }], omitted_asset_count: 0 });
      const processor = new RestaurantProcessor(database, readAISettings({ OPENAI_API_KEY: 'test-key' }), '.local/no-image-fixture');
      const result = await processor.processMessage(envelope, { allowSourceDiscovery: true });
      assert.equal(result.ignored, true); assert.deepEqual(result.shop_ids, []);
      assert.equal(requests.length, 1, 'only the extraction call runs');
      assert.doesNotMatch(requests[0]?.body ?? '', /web_search/);
      assert.equal((await database.query('SELECT processing_status FROM messages WHERE message_id=$1', z.object({ processing_status: z.string() }), [envelope.message_id]))[0]?.processing_status, 'ignored');
    });
  } finally {
    if (previous === undefined) delete process.env.OPENAI_BASE_URL; else process.env.OPENAI_BASE_URL = previous;
    await database.close();
  }
});
