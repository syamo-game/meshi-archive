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
import { anthropicMessage, fixture, withJsonServer } from './helpers.js';

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
    await assert.rejects(processor.processMessage(envelope), /ANTHROPIC_API_KEY/);
    const message = (await database.query('SELECT * FROM messages WHERE message_id=$1', messageRecord, [envelope.message_id]))[0]; assert.equal(message?.processing_status, 'failed'); assert.match(message?.fetch_error ?? '', /ANTHROPIC_API_KEY/);
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
  const responseBody = anthropicMessage([{ type: 'text', text: JSON.stringify(extraction) }]);
  const previous = process.env.ANTHROPIC_BASE_URL;
  try {
    await withJsonServer(() => ({ status: 200, body: responseBody }), async (baseUrl, requests) => {
      process.env.ANTHROPIC_BASE_URL = baseUrl;
      const link = 'https://x.com/synthetic_user/status/1900000000000000002';
      const envelope = envelopeSchema.parse({ message_id: '90000000000000200', channel_id: '123456789012345678', content: link + '\n[Embed Description] 合成の雑談です', created_at: '2026-11-17T00:00:00Z', assets: [{ kind: 'link', url: link }, { kind: 'embed', url: link, description: '合成の雑談です' }], omitted_asset_count: 0 });
      const processor = new RestaurantProcessor(database, readAISettings({ ANTHROPIC_API_KEY: 'test-key' }), '.local/no-image-fixture');
      const result = await processor.processMessage(envelope, { allowSourceDiscovery: true });
      assert.equal(result.ignored, true); assert.deepEqual(result.shop_ids, []);
      assert.equal(requests.length, 1, 'only the extraction call runs');
      assert.doesNotMatch(requests[0]?.body ?? '', /web_search/);
      assert.equal((await database.query('SELECT processing_status FROM messages WHERE message_id=$1', z.object({ processing_status: z.string() }), [envelope.message_id]))[0]?.processing_status, 'ignored');
    });
  } finally {
    if (previous === undefined) delete process.env.ANTHROPIC_BASE_URL; else process.env.ANTHROPIC_BASE_URL = previous;
    await database.close();
  }
});

test('processing never sends a service tier the Anthropic API does not accept', async () => {
  // Sync and mention runs send the same request; the Messages API has no discounted tier for calls that wait for the answer.
  const extraction = { is_restaurant_message: false, ignore_reason: '合成の対象外です。', unresolved_reason: null, mentions: [] };
  const responseBody = anthropicMessage([{ type: 'text', text: JSON.stringify(extraction) }]);
  const previous = process.env.ANTHROPIC_BASE_URL;
  try {
    await withJsonServer(() => ({ status: 200, body: responseBody }), async (baseUrl, requests) => {
      process.env.ANTHROPIC_BASE_URL = baseUrl;
      for (const messageId of ['90000000000000210', '90000000000000211']) {
        const { database } = await fixture();
        try {
          const envelope = envelopeSchema.parse({ message_id: messageId, channel_id: '123456789012345678', content: '合成の雑談です', created_at: '2026-11-17T00:00:00Z', assets: [], omitted_asset_count: 0 });
          const processor = new RestaurantProcessor(database, readAISettings({ ANTHROPIC_API_KEY: 'test-key' }), '.local/no-image-fixture');
          assert.equal((await processor.processMessage(envelope)).ignored, true);
        } finally { await database.close(); }
      }
      const tiers = requests.map(request => z.object({ service_tier: z.string().optional() }).parse(JSON.parse(request.body)).service_tier ?? null);
      assert.deepEqual(tiers, [null, null]);
      assert.ok(requests.every(request => request.url.endsWith('/v1/messages')));
    });
  } finally { if (previous === undefined) delete process.env.ANTHROPIC_BASE_URL; else process.env.ANTHROPIC_BASE_URL = previous; }
});

test('a restaurant post with no shop name gets clue-search candidates for review, keeping only searched evidence', async () => {
  const extraction = { is_restaurant_message: true, ignore_reason: null, unresolved_reason: '店名の記載がない。', mentions: [] };
  const clues = { is_list_post: false, clues: ['合成町', '合成鍋'], unresolved_reason: null, candidates: [
    { shop_name: '合成食堂', branch_name: null, area: '東京都中央区', matched_clues: ['写真の看板が一致'], evidence_urls: ['https://shop.example.com/a/#menu'], confidence: 'high', area_matches_post: 'match' },
    { shop_name: '架空亭', branch_name: null, area: null, matched_clues: ['料理が似ている'], evidence_urls: ['https://not-searched.example.com/'], confidence: 'medium', area_matches_post: 'unknown' },
  ] };
  const responses = [
    anthropicMessage([{ type: 'text', text: JSON.stringify(extraction) }]),
    anthropicMessage([
      { type: 'server_tool_use', id: 'srvtoolu_1', name: 'web_search', input: { query: '合成町 合成鍋' }, caller: { type: 'direct' } },
      { type: 'web_search_tool_result', tool_use_id: 'srvtoolu_1', caller: { type: 'direct' }, content: [{ type: 'web_search_result', url: 'https://shop.example.com/a', title: '合成食堂', encrypted_content: 'synthetic', page_age: null }] },
      { type: 'text', text: JSON.stringify(clues) },
    ], 1),
  ];
  const { database } = await fixture(); const previous = process.env.ANTHROPIC_BASE_URL; let calls = 0;
  try {
    await withJsonServer(() => ({ status: 200, body: responses[Math.min(calls++, 1)] ?? {} }), async (baseUrl, requests) => {
      process.env.ANTHROPIC_BASE_URL = baseUrl;
      const envelope = envelopeSchema.parse({ message_id: '90000000000000220', channel_id: '123456789012345678', content: '合成町の名店で合成鍋', created_at: '2026-11-17T00:00:00Z', assets: [], omitted_asset_count: 0 });
      const processor = new RestaurantProcessor(database, readAISettings({ ANTHROPIC_API_KEY: 'test-key' }), '.local/no-image-fixture');
      const result = await processor.processMessage(envelope, { allowClueSearch: true });
      assert.equal(result.ignored, false); assert.equal(result.pending_mention_ids.length, 1);
      assert.equal(requests.length, 2);
      const searchRequest = z.object({ tools: z.array(z.object({ type: z.string(), max_uses: z.number() })) }).parse(JSON.parse(requests[1]?.body ?? '{}'));
      assert.deepEqual(searchRequest.tools.map(tool => [tool.type, tool.max_uses]), [['web_search_20250305', 3]]);
      const mention = (await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [envelope.message_id]))[0];
      assert.equal(mention?.review_status, 'pending'); assert.equal(mention?.shop_id, null);
      assert.match(mention?.confidence_reason ?? '', /手がかり検索: 候補1件。検索で根拠を確認できない候補1件を除外/);
      const candidates = await database.query('SELECT name,area,evidence_url,provenance,is_verified,verification_reason FROM resolution_candidates WHERE mention_id=$1 ORDER BY rank', z.object({ name: z.string(), area: z.string().nullable(), evidence_url: z.string().nullable(), provenance: z.string(), is_verified: z.union([z.boolean(), z.number()]), verification_reason: z.string().nullable() }), [mention?.id ?? 0]);
      assert.equal(candidates.length, 1); assert.equal(candidates[0]?.name, '合成食堂'); assert.equal(candidates[0]?.evidence_url, 'https://shop.example.com/a/#menu');
      assert.equal(candidates[0]?.provenance, 'web_search'); assert.equal(Boolean(candidates[0]?.is_verified), false); assert.match(candidates[0]?.verification_reason ?? '', /確信度=高 \/ 一致: 写真の看板が一致/);
    });
  } finally { if (previous === undefined) delete process.env.ANTHROPIC_BASE_URL; else process.env.ANTHROPIC_BASE_URL = previous; await database.close(); }
});

test('a post is read with its numbered photos, and a shop named only in a photo is registered like a named one', async () => {
  const { mkdir, mkdtemp, rm, writeFile } = await import('node:fs/promises');
  const { dirname, join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const sharp = (await import('sharp')).default;
  const { sourceImagePath } = await import('../src/image-cache.js');
  const photo = 'https://pbs.twimg.com/media/synthetic-screenshot.jpg';
  // The photo shows the name of fixture shop 5 (木場); the text names no shop.
  const extraction = { is_restaurant_message: true, ignore_reason: null, unresolved_reason: null, mentions: [{ shop_name: '店舗05', branch_name: null, area: '木場', category: null, source_url: null, needs_review: false, confidence_reason: '写真1の店舗ページ画面に店名がある', subject_kind: 'restaurant', identity_evidence: 'explicit', name_evidence: '店舗05 木場/和食', branch_evidence: null, operating_status: 'unknown', operating_status_evidence: null, event_origin: null, name_photo: 1, card_photo: 0 }] };
  const { database } = await fixture(); const directory = await mkdtemp(join(tmpdir(), 'meshi-photo-pipeline-')); const previous = process.env.ANTHROPIC_BASE_URL;
  try {
    const cached = sourceImagePath(photo, directory); await mkdir(dirname(cached), { recursive: true });
    await writeFile(cached, await sharp({ create: { width: 64, height: 48, channels: 3, background: '#c08040' } }).jpeg().toBuffer());
    await withJsonServer(() => ({ status: 200, body: anthropicMessage([{ type: 'text', text: JSON.stringify(extraction) }]) }), async (baseUrl, requests) => {
      process.env.ANTHROPIC_BASE_URL = baseUrl;
      const envelope = envelopeSchema.parse({ message_id: '90000000000000230', channel_id: '123456789012345678', content: '木場の町寿司 本当においしい', created_at: '2026-11-17T00:00:00Z', assets: [{ kind: 'image', url: photo, is_embed_preview: true }], omitted_asset_count: 0 });
      const processor = new RestaurantProcessor(database, readAISettings({ ANTHROPIC_API_KEY: 'test-key' }), directory);
      const result = await processor.processMessage(envelope);
      assert.equal(requests.length, 1, 'the existing shop matched, so no web search ran');
      const body = z.object({ system: z.union([z.string(), z.array(z.object({ text: z.string() }))]), messages: z.array(z.object({ content: z.array(z.object({ type: z.string(), text: z.string().optional() })) })) }).parse(JSON.parse(requests[0]?.body ?? '{}'));
      const system = typeof body.system === 'string' ? body.system : body.system.map(block => block.text).join('');
      assert.match(system, /「写真1」「写真2」のように番号/, 'the photo extraction prompt is used');
      assert.deepEqual(body.messages[0]?.content.map(block => block.type === 'text' ? block.text?.startsWith('写真') ? block.text : 'post' : block.type), ['post', '写真1', 'image']);
      assert.deepEqual(result.shop_ids, [5]); assert.deepEqual(result.pending_mention_ids, []);
      const mention = (await database.query('SELECT * FROM shop_mentions WHERE message_id=$1', mentionRecord, [envelope.message_id]))[0];
      assert.equal(mention?.review_status, 'approved'); assert.equal(mention?.resolution_basis, 'existing_shop'); assert.equal(mention?.extraction_error, null);
    });
  } finally { if (previous === undefined) delete process.env.ANTHROPIC_BASE_URL; else process.env.ANTHROPIC_BASE_URL = previous; await database.close(); await rm(directory, { recursive: true, force: true }); }
});
