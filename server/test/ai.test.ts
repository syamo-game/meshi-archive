import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import type { Message } from '@anthropic-ai/sdk/resources/messages/messages';
import { z } from 'zod';
import contract from '../data/ai-contract.json' with { type: 'json' };
import { AICallLimiter, callMetrics, completedSearchSources, estimateCost, outputSchemas, readAISettings, RestaurantAI, searchSources, wireSchema } from '../src/ai.js';
import { categories } from '../src/categories.js';
import { extractedMentionSchema, extractedMessageSchema, requireExtractionEvidence } from '../src/ai-models.js';
import { assessMentionEvidence, containsEvidenceName, eventOriginQuotesMatch, imageEvidenceAssessment, inputEvidenceAssessment } from '../src/extraction-safety.js';

const expected = z.object({ codes: z.array(z.string()), reasons: z.array(z.string()) });
const origin = z.object({ name: z.string(), branch: z.string().nullable(), area: z.string().nullable(), relation_evidence: z.string(), permanent_evidence: z.string(), relation_text: z.string(), permanent_text: z.string() });
const referenceSchema = z.object({ reference: z.literal('c790aa246f6ff9ce2aec7fc6a2d9e8fe16c4184d'), synthetic: z.literal(true), evidence: z.array(z.object({ mention: extractedMentionSchema, content: z.string(), omitted_assets: z.number(), expected })), contains: z.array(z.object({ name: z.string(), text: z.string(), expected: z.boolean() })), images: z.array(z.object({ subject: z.enum(['restaurant', 'product', 'event', 'unknown']), content: z.string(), expected })), origins: z.array(z.object({ input: origin, expected: z.boolean() })) });
test('extraction evidence and event safeguards match 269 original Python cases', async () => {
  const reference = referenceSchema.parse(JSON.parse(await readFile(new URL('./extraction-parity.json', import.meta.url), 'utf8')));
  for (const item of reference.evidence) assert.deepEqual(assessMentionEvidence(item.mention, item.content, inputEvidenceAssessment(item.content, item.omitted_assets)), item.expected);
  for (const item of reference.contains) assert.equal(containsEvidenceName(item.name, item.text), item.expected, item.text);
  for (const item of reference.images) assert.deepEqual(imageEvidenceAssessment(item.subject, item.content), item.expected);
  for (const item of reference.origins) assert.equal(eventOriginQuotesMatch(item.input), item.expected, JSON.stringify(item.input));
});

type Block = Message['content'][number];
const direct = { type: 'direct' } as const;
const use = (id: string): Block => ({ type: 'server_tool_use', id, name: 'web_search', input: { query: 'synthetic' }, caller: direct });
const found = (id: string, urls: readonly string[]): Block => ({ type: 'web_search_tool_result', tool_use_id: id, caller: direct, content: urls.map(url => ({ type: 'web_search_result', url, title: 'synthetic', encrypted_content: 'synthetic', page_age: null })) });
const failed = (id: string, code: 'max_uses_exceeded' | 'unavailable'): Block => ({ type: 'web_search_tool_result', tool_use_id: id, caller: direct, content: { type: 'web_search_tool_result_error', error_code: code } });
const usage = (searches: number, input = 100, output = 20): Message['usage'] => ({ input_tokens: input, output_tokens: output, cache_creation_input_tokens: null, cache_read_input_tokens: null, cache_creation: null, inference_geo: null, server_tool_use: { web_search_requests: searches, web_fetch_requests: 0 }, service_tier: 'standard' } as Message['usage']);
function searchResponse(content: Block[], billed = 1): Pick<Message, 'content' | 'usage'> { return { content, usage: usage(billed) }; }
test('web search requires one completed bounded call and real public source URLs', () => {
  assert.deepEqual(completedSearchSources(searchResponse([use('a'), found('a', ['https://example.com/shop', 'https://example.com/shop'])]), 'test'), ['https://example.com/shop']);
  // A follow-up past max_uses only returns max_uses_exceeded and is not a second search.
  assert.deepEqual(completedSearchSources(searchResponse([use('a'), found('a', ['https://example.com/shop']), use('b'), failed('b', 'max_uses_exceeded')]), 'test'), ['https://example.com/shop']);
  assert.throws(() => completedSearchSources(searchResponse([], 0), 'test'), /exactly once/);
  assert.throws(() => completedSearchSources(searchResponse([use('a'), found('a', ['https://example.com/a']), use('b'), found('b', ['https://example.com/b'])], 2), 'test'), /exactly once/);
  assert.throws(() => completedSearchSources(searchResponse([use('a'), failed('a', 'unavailable')], 1), 'test'), /exactly once/);
  assert.throws(() => completedSearchSources(searchResponse([use('a')], 1), 'test'), /exactly once/);
  assert.throws(() => completedSearchSources(searchResponse([use('a'), found('a', ['https://example.com/shop'])], 2), 'test'), /usage count/);
  assert.throws(() => completedSearchSources(searchResponse([use('a'), found('a', ['http://127.0.0.1/internal'])]), 'test'), /non-public/);
  assert.throws(() => completedSearchSources(searchResponse([use('a'), found('a', ['https://name:secret@example.com/'])]), 'test'), /invalid source/);
  assert.deepEqual(searchSources(searchResponse([use('a'), found('a', ['https://example.com/a']), use('b'), failed('b', 'unavailable'), use('c'), found('c', ['https://example.com/b', 'https://example.com/a'])], 3), 'test'), ['https://example.com/a', 'https://example.com/b']);
});

test('structured extraction rejects invalid shapes and marks missing evidence for review', () => {
  const raw = { is_restaurant_message: true, mentions: [{ shop_name: 'テスト食堂', needs_review: false, confidence_reason: '根拠が不足' }] };
  const parsed = requireExtractionEvidence(extractedMessageSchema.parse(raw));
  assert.equal(parsed.mentions[0]?.subject_kind, 'unknown'); assert.equal(parsed.mentions[0]?.needs_review, true);
  assert.throws(() => extractedMessageSchema.parse({ ...raw, is_restaurant_message: false }));
  assert.throws(() => extractedMessageSchema.parse({ is_restaurant_message: false, unresolved_reason: '未確認', mentions: [] }));
  assert.throws(() => extractedMentionSchema.parse({ ...raw.mentions[0], source_url: 'javascript:alert(1)' }));
});

test('AI limiter caps real asynchronous work and releases cancelled queue entries', async () => {
  const limiter = new AICallLimiter(2); let running = 0; let maximum = 0;
  await Promise.all(Array.from({ length: 12 }, () => limiter.run(async () => { running++; maximum = Math.max(maximum, running); await setImmediate(); running--; })));
  assert.equal(maximum, 2); assert.equal(running, 0);
  const single = new AICallLimiter(1); const gate = Promise.withResolvers<void>(); const first = single.run(() => gate.promise);
  const controller = new AbortController(); const second = single.run(async () => { throw new Error('Cancelled work must not run'); }, controller.signal); controller.abort();
  await assert.rejects(second, /cancelled/); gate.resolve(); await first; assert.equal(await single.run(async () => 42), 42);
});

test('AI settings default to Claude models and fail without a configured key before network access', async () => {
  const settings = readAISettings({}); assert.equal(settings.EXTRACTION_MODEL, 'claude-sonnet-5-5'); assert.equal(settings.RESOLUTION_MODEL, 'claude-opus-5-5'); assert.equal(settings.RESOLUTION_REASONING_EFFORT, 'high');
  assert.throws(() => readAISettings({ AI_MAX_CONCURRENCY: 'invalid' }), /AI_MAX_CONCURRENCY/);
  const client = new RestaurantAI(settings, '.local/no-ai-fixture');
  await assert.rejects(client.extract(''), /empty message/);
  await assert.rejects(client.extract('テスト食堂'), error => error instanceof Error && error.message === 'ANTHROPIC_API_KEY is not configured');
});

test('model output schemas limit category to master values or null and keep other fields', () => {
  const choice = z.object({ anyOf: z.tuple([z.object({ type: z.literal('string'), enum: z.array(z.string()) }), z.object({ type: z.literal('null') })]) });
  for (const properties of [outputSchemas.message.$defs.ExtractedMention.properties, outputSchemas.candidates.$defs.SearchCandidate.properties]) {
    assert.deepEqual(choice.parse(properties.category).anyOf[0].enum, [...categories]);
    assert.deepEqual(properties.area, contract.schemas.message.$defs.ExtractedMention.properties.area);
  }
  assert.deepEqual(Object.keys(outputSchemas.message.$defs.ExtractedMention.properties), Object.keys(contract.schemas.message.$defs.ExtractedMention.properties));
  assert.deepEqual(outputSchemas.images, contract.schemas.images);
  for (const prompt of [contract.prompts.extraction, contract.prompts.image_extraction, contract.prompts.search, contract.prompts.image_search, contract.prompts.source_discovery]) { assert.ok(prompt.includes('末尾のカテゴリー一覧から1つ選びます')); assert.ok(!prompt.includes('簡潔な料理ジャンル')); }
  // An unreadable source must go to review, not be ignored.
  assert.ok(contract.prompts.source_discovery.includes('判断できない場合は対象外にせず、is_restaurant_message=true、mentions=[]、ignore_reason=nullにし、確認できなかった理由をunresolved_reasonに書きます'));
  assert.ok(!contract.prompts.source_discovery.includes('判断できない場合も同じです'));
});

test('model accounting preserves estimates and retries without claiming current API prices', () => {
  assert.equal(estimateCost('claude-sonnet-5-5', 100, 20, 1, {}), 10400);
  assert.equal(estimateCost('claude-opus-5-5', 100, 20, 0, {}), 800);
  assert.equal(estimateCost('synthetic', 1, 0, 0, { SYNTHETIC_INPUT_USD_PER_M: '2.5' }), 2);
  const metrics = callMetrics({ usage: usage(1) }, 'claude-sonnet-5-5', 15.9, 2, 1, true);
  assert.equal(metrics.web_search_calls, 2); assert.equal(metrics.estimated_cost_microusd, 510400); assert.equal(metrics.latency_ms, 15);
  // Cached prompt tokens are still input tokens.
  assert.equal(callMetrics({ usage: { ...usage(0), cache_creation_input_tokens: 5, cache_read_input_tokens: 7 } }, 'claude-sonnet-5-5', 1, 1).input_tokens, 112);
});

test('wire schemas stay within the API compile limits and drop only keywords zod still checks', () => {
  const unions = (node: unknown): number => Array.isArray(node) ? node.reduce((sum: number, item: unknown) => sum + unions(item), 0) : node && typeof node === 'object' ? Number('anyOf' in node) + Object.values(node).reduce((sum: number, item: unknown) => sum + unions(item), 0) : 0;
  for (const name of ['message', 'candidates', 'images', 'preflight', 'clue_search'] as const) {
    const schema = wireSchema(name); const serialized = JSON.stringify(schema);
    assert.ok(unions(schema) <= 16, `${name} has ${unions(schema)} union fields`);
    for (const keyword of ['"maxLength"', '"minLength"', '"maxItems"']) assert.ok(!serialized.includes(keyword), `${name} still sends ${keyword}`);
  }
  const mention = z.object({ $defs: z.object({ ExtractedMention: z.object({ properties: z.record(z.string(), z.object({ anyOf: z.array(z.unknown()).optional(), enum: z.array(z.string()).optional() })) }) }) }).parse(wireSchema('message')).$defs.ExtractedMention.properties;
  for (const field of ['subject_kind', 'identity_evidence', 'operating_status']) { assert.equal(mention[field]?.anyOf, undefined); assert.ok(mention[field]?.enum?.includes('unknown')); }
  assert.ok(mention.branch_name?.anyOf, 'nullable text keeps null');
  assert.ok(mention.category?.anyOf, 'category keeps null because it has no unknown value');
  const category = z.object({ anyOf: z.tuple([z.object({ enum: z.array(z.string()) }), z.object({ type: z.literal('null') })]) }).parse(mention.category);
  assert.deepEqual(category.anyOf[0].enum, [...categories], 'the category list must reach the API as an enum');
});


// A local server stands in for the Anthropic API so the real client and retry loop run.
async function withAnthropicServer(status: number, body: object, run: (requests: () => number) => Promise<void>): Promise<void> {
  const { createServer } = await import('node:http');
  let count = 0;
  const server = createServer((request, response) => { count++; request.resume(); response.writeHead(status, { 'content-type': 'application/json', 'retry-after': '1', 'request-id': 'req_synthetic' }).end(JSON.stringify(body)); });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address(); if (!address || typeof address === 'string') throw new Error('Test server has no port');
  const previous = process.env.ANTHROPIC_BASE_URL; process.env.ANTHROPIC_BASE_URL = `http://127.0.0.1:${address.port}`;
  try { await run(() => count); }
  finally { if (previous === undefined) delete process.env.ANTHROPIC_BASE_URL; else process.env.ANTHROPIC_BASE_URL = previous; await new Promise<void>(resolve => server.close(() => resolve())); }
}
test('a low credit balance fails at once without retries and keeps the API message', async () => {
  await withAnthropicServer(400, { type: 'error', error: { type: 'invalid_request_error', message: 'Your credit balance is too low to access the Anthropic API.' } }, async requests => {
    const client = new RestaurantAI(readAISettings({ ANTHROPIC_API_KEY: 'test-key' }), '.local/no-ai-fixture');
    await assert.rejects(client.preflightModels(), /stage=model_preflight attempt=1 status=400 request_id=req_synthetic error=.*credit balance is too low/);
    assert.equal(requests(), 1);
  });
});
test('ordinary rate limits are still retried three times', async () => {
  await withAnthropicServer(429, { type: 'error', error: { type: 'rate_limit_error', message: 'Rate limit reached.' } }, async requests => {
    const client = new RestaurantAI(readAISettings({ ANTHROPIC_API_KEY: 'test-key' }), '.local/no-ai-fixture');
    await assert.rejects(client.preflightModels(), /stage=model_preflight attempt=3 status=429 request_id=req_synthetic /);
    assert.equal(requests(), 3);
  });
});

