import assert from 'node:assert/strict';
import { test } from 'node:test';
import { extractedMentionSchema } from '../src/ai-models.js';
import { pipelineCandidateSchema } from '../src/pipeline-candidates.js';
import { PipelineShops } from '../src/pipeline-shops.js';
import { fixture } from './helpers.js';

test('pipeline reuses approved identities and blocks unapproved or conflicting external identities', async () => {
  const { database } = await fixture();
  try {
    const shops = new PipelineShops(database);
    const mention = extractedMentionSchema.parse({ shop_name: '店舗31', area: '門前仲町', needs_review: false, confidence_reason: 'Synthetic evidence' });
    assert.equal((await shops.fromMention(mention, null))[0], null);
    await database.execute("UPDATE shop_mentions SET review_status='approved' WHERE shop_id=31");
    assert.equal((await shops.fromMention(mention, null))[0]?.id, 31);
    await database.execute("UPDATE shops SET external_source='tabelog',external_id='12345678',canonical_url='https://tabelog.com/tokyo/A1313/A131303/87654321/' WHERE id=31");
    assert.match((await shops.fromMention(mention, null))[1] ?? '', /外部IDが矛盾/);
    await database.execute("UPDATE shops SET canonical_url=NULL WHERE id=31");
    assert.match((await shops.fromMention(mention, 'https://tabelog.com/tokyo/A1313/A131303/87654321/'))[1] ?? '', /競合/);
  } finally { await database.close(); }
});

test('creation collision links only one approved and verified store, never an evidence URL alone', async () => {
  const { database } = await fixture();
  try {
    const mention = extractedMentionSchema.parse({ shop_name: '店舗31', area: '門前仲町', needs_review: false, confidence_reason: 'Synthetic evidence' });
    const candidate = pipelineCandidateSchema.parse({ name: '店舗31', area: '門前仲町', provenance: 'structured_data', is_verified: true, canonical_url: 'https://example.com/shop31' });
    await database.execute("UPDATE shops SET canonical_url='https://example.com/shop31' WHERE id=31");
    const guard = (): ReturnType<PipelineShops['guardCreation']> => database.transaction(connection => new PipelineShops(connection).guardCreation(mention, candidate, null));
    assert.match((await guard())[2] ?? '', /未承認/);
    await database.execute("UPDATE shop_mentions SET review_status='approved' WHERE shop_id=31");
    assert.equal((await guard())[1]?.id, 31);
    const unverified = await database.transaction(connection => new PipelineShops(connection).guardCreation(mention, { ...candidate, is_verified: false }, null));
    assert.match(unverified[2] ?? '', /未検証/);
    const other = { ...candidate, name: '別店舗', area: '木場', canonical_url: null, evidence_url: candidate.canonical_url };
    const evidenceOnly = await database.transaction(connection => new PipelineShops(connection).guardCreation({ ...mention, shop_name: '別店舗', area: '木場' }, other, null));
    assert.match(evidenceOnly[2] ?? '', /根拠URLだけ/);
  } finally { await database.close(); }
});
