import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers/promises';
import { test } from 'node:test';
import type { Response, ResponseFunctionWebSearch } from 'openai/resources/responses/responses';
import { z } from 'zod';
import { AICallLimiter, callMetrics, completedSearchSources, estimateCost, readAISettings, RestaurantAI } from '../src/ai.js';
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

function searchResponse(calls: ResponseFunctionWebSearch[]): Pick<Response, 'status' | 'parallel_tool_calls' | 'output'> & { max_tool_calls: number } { return { status: 'completed', max_tool_calls: 1, parallel_tool_calls: false, output: calls }; }
const completed: ResponseFunctionWebSearch = { id: 'synthetic-search', type: 'web_search_call', status: 'completed', action: { type: 'search', sources: [{ type: 'url', url: 'https://example.com/shop' }, { type: 'url', url: 'https://example.com/shop' }] } };
test('web search requires one completed bounded call and real public source URLs', () => {
  assert.deepEqual(completedSearchSources(searchResponse([completed]), 'test'), ['https://example.com/shop']);
  assert.deepEqual(completedSearchSources(searchResponse([{ ...completed, action: { type: 'open_page', url: 'https://example.com/shop' } }]), 'test'), ['https://example.com/shop']);
  assert.throws(() => completedSearchSources(searchResponse([]), 'test'), /exactly once/);
  assert.throws(() => completedSearchSources(searchResponse([completed, completed]), 'test'), /exactly once/);
  assert.throws(() => completedSearchSources({ ...searchResponse([completed]), max_tool_calls: 2 }, 'test'), /configuration/);
  assert.throws(() => completedSearchSources({ ...searchResponse([completed]), parallel_tool_calls: true }, 'test'), /configuration/);
  assert.throws(() => completedSearchSources(searchResponse([{ ...completed, action: { type: 'open_page', url: 'http://127.0.0.1/internal' } }]), 'test'), /non-public/);
  assert.throws(() => completedSearchSources(searchResponse([{ ...completed, action: { type: 'open_page', url: 'https://name:secret@example.com/' } }]), 'test'), /invalid source/);
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

test('AI settings keep existing models and fail without a configured key before network access', async () => {
  const settings = readAISettings({}); assert.equal(settings.EXTRACTION_MODEL, 'gpt-6.1-sol'); assert.equal(settings.RESOLUTION_REASONING_EFFORT, 'high');
  assert.throws(() => readAISettings({ AI_MAX_CONCURRENCY: 'invalid' }), /AI_MAX_CONCURRENCY/);
  const client = new RestaurantAI(settings, '.local/no-ai-fixture');
  await assert.rejects(client.extract(''), /empty message/);
  await assert.rejects(client.extract('テスト食堂'), error => error instanceof Error && error.message === 'OPENAI_API_KEY is not configured');
});

test('model accounting preserves estimates and retries without claiming current API prices', () => {
  assert.equal(estimateCost('gpt-6.1-sol', 100, 20, 1, {}), 10400);
  assert.equal(estimateCost('synthetic', 1, 0, 0, { SYNTHETIC_INPUT_USD_PER_M: '2.5' }), 2);
  const response: Pick<Response, 'usage' | 'output'> = { usage: { input_tokens: 100, output_tokens: 20, total_tokens: 120, input_tokens_details: { cached_tokens: 0 }, output_tokens_details: { reasoning_tokens: 0 } }, output: [completed] };
  const metrics = callMetrics(response, 'gpt-6.1-sol', 15.9, 2, 1, true);
  assert.equal(metrics.web_search_calls, 2); assert.equal(metrics.estimated_cost_microusd, 510400); assert.equal(metrics.latency_ms, 15);
});

