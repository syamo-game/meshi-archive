import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { z } from 'zod';
import { readAISettings, RestaurantAI } from '../src/ai.js';
import { searchCandidateSchema } from '../src/ai-models.js';
import { AppError } from '../src/errors.js';
import { mentionRecord, shopRecord } from '../src/records.js';
import { normalizeUrlIdentity } from '../src/resolution.js';
import { externalIdentity } from '../src/shop-creation.js';
import { citationUrlIdentity, sanitizeCitedCandidate, sourceEvidenceUrlIdentity } from '../src/source-evidence.js';
import { ReviewSuggestions, suggestionRequest, validateReferenceUrl } from '../src/suggestions.js';
import { fixture, headers } from './helpers.js';

test('citation identity, external IDs and canonical normalization match 15 Python cases', async () => {
  const cases = z.array(z.object({ url: z.string(), citation: z.string(), source: z.string(), external: z.tuple([z.string().nullable(), z.string().nullable()]), normalized: z.string() })).parse(JSON.parse(await readFile(new URL('./citation-parity.json', import.meta.url), 'utf8')));
  for (const item of cases) {
    assert.equal(citationUrlIdentity(item.url), item.citation, item.url); assert.equal(sourceEvidenceUrlIdentity(item.url), item.source, item.url);
    assert.deepEqual(externalIdentity(item.url) ?? [null, null], item.external, item.url); assert.equal(normalizeUrlIdentity(item.url), item.normalized, item.url);
  }
});
test('suggestions retain only cited URLs and derive external IDs from the retained sources', () => {
  const candidate = searchCandidateSchema.parse({ name: 'テスト店舗', canonical_url: 'https://tabelog.com/tokyo/A1301/A130101/12345678/', evidence_url: 'https://example.com/not-cited', external_source: 'tabelog', external_id: '87654321', confidence_reason: '検索の出典' });
  assert.equal(sanitizeCitedCandidate(candidate, ['https://unrelated.example/']), null);
  const sanitized = sanitizeCitedCandidate(candidate, [candidate.canonical_url ?? '']); assert.ok(sanitized);
  assert.equal(sanitized.evidence_url, null); assert.equal(sanitized.external_id, '12345678');
  for (const url of ['http://localhost/path', 'http://server.local/path', 'http://10.0.0.1/path', 'https://example.com:999/path', 'https://user:pass@example.com/']) assert.throws(() => validateReferenceUrl(url));
});
test('suggestion permission, rejected items and stale input fail before AI access', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    const mention = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(mention);
    const request = (body: object, member = false): Promise<Response> => app.request(`/api/admin/reviews/${mention.id}/suggestions`, { method: 'POST', headers: { ...headers(member ? viewer : admin), 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    assert.equal((await request({ expected_version: 1, shop_version: 1 }, true)).status, 403);
    assert.equal((await request({ expected_version: 99, shop_version: 1 })).status, 409);
    await database.execute("UPDATE shop_mentions SET review_status='rejected' WHERE id=$1", [mention.id]);
    assert.equal((await request({ expected_version: 1, shop_version: 1 })).status, 409);
    assert.equal((await request({ expected_version: 1, shop_version: 1, reference_url: 'http://127.0.0.1/' })).status, 422);
  } finally { await database.close(); }
});
test('a missing AI key leaves every saved field and review version untouched', async () => {
  const { database, config } = await fixture();
  try {
    const mention = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(mention);
    const before = (await database.query('SELECT * FROM shops WHERE id=31', shopRecord))[0];
    const suggestions = new ReviewSuggestions(database, config, new RestaurantAI(readAISettings({}), config.imageDirectory));
    await assert.rejects(suggestions.suggest(mention.id, suggestionRequest.parse({ expected_version: 1, shop_version: 1, draft: { shop_name: '入力中の店名' }, supplement: '入力は保存されない' })), error => error instanceof AppError && error.status === 502);
    assert.deepEqual((await database.query('SELECT * FROM shops WHERE id=31', shopRecord))[0], before);
    assert.equal((await database.query('SELECT * FROM shop_mentions WHERE id=$1', mentionRecord, [mention.id]))[0]?.version, 1);
  } finally { await database.close(); }
});
