import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { z } from 'zod';
import { extractedMentionSchema } from '../src/ai-models.js';
import { candidateForNewShop, containedNameCandidates, dedupeCandidates, pageVerifiedCandidateValid, pipelineCandidateSchema, relaxedVerifiedCandidateValid, structuredBlockReason, topWebCandidate, verifyFromStructuredPage } from '../src/pipeline-candidates.js';
import type { PipelineCandidate } from '../src/pipeline-candidates.js';

test('candidate selection and conflicting sources match the original pipeline', async () => {
  const data = z.object({ cases: z.array(z.object({ mention: extractedMentionSchema, candidates: z.array(pipelineCandidateSchema), deduped: z.array(pipelineCandidateSchema), new_shop: pipelineCandidateSchema.nullable(), blocked: z.string().nullable(), top: pipelineCandidateSchema.nullable(), precise: pipelineCandidateSchema.nullable(), excluded: pipelineCandidateSchema.nullable(), verified: pipelineCandidateSchema.nullable() })) }).parse(JSON.parse(await readFile(new URL('./pipeline-candidates-parity.json', import.meta.url), 'utf8')));
  for (const [index, item] of data.cases.entries()) {
    const label = 'case=' + index;
    assert.deepEqual(dedupeCandidates(item.candidates), item.deduped, label);
    assert.deepEqual(candidateForNewShop(item.mention, item.candidates), item.new_shop, label);
    assert.equal(structuredBlockReason(item.mention, item.candidates), item.blocked, label);
    assert.deepEqual(topWebCandidate(item.mention, item.candidates), item.top, label);
    assert.deepEqual(topWebCandidate(item.mention, item.candidates, true), item.precise, label);
    assert.deepEqual(topWebCandidate(item.mention, item.candidates, false, ['https://example.com/shop']), item.excluded, label);
    const first = item.candidates[0]; assert.ok(first);
    assert.deepEqual(verifyFromStructuredPage(item.mention, first, item.candidates), item.verified, label);
  }
});

const posted = (name: string, area: string | null, branch: string | null = null) => extractedMentionSchema.parse({ shop_name: name, branch_name: branch, area, needs_review: false, confidence_reason: 'synthetic' });
const found = (name: string, address: string | null, url: string | null, verified = false): PipelineCandidate => pipelineCandidateSchema.parse({ name, area: '清澄白河', address, canonical_url: url, evidence_url: url, provenance: 'web_search', is_verified: verified });

test('a posted name inside the candidate name is accepted only for one place and a post with a master area', () => {
  const tabelog = found('居酒屋 さんぷる', '東京都江東区高橋99-9', 'https://example.com/a'); const official = found('さんぷる', '〒135-0000 東京都江東区高橋99番9号 サンプルビル1F', 'https://example.org/b');
  const picked = containedNameCandidates(posted('さんぷる', '清澄白河'), [tabelog, official]);
  assert.equal(picked.reason, null); assert.deepEqual(picked.candidates.map(item => item.name), ['居酒屋 さんぷる', 'さんぷる']);
  assert.equal(picked.candidates[0]?.verification_reason, 'policy=unique_place_contained_name; canonical_area=清澄白河; source_verified=false');
  assert.equal(topWebCandidate(posted('さんぷる', '清澄白河'), [tabelog]), null);
  const elsewhere = found('さんぷる 別館', '東京都江東区高橋11-1', 'https://example.net/c');
  assert.deepEqual(containedNameCandidates(posted('さんぷる', '清澄白河'), [tabelog, elsewhere]), { candidates: [], reason: '投稿の店名を含むWeb候補が複数の場所にある' });
  assert.match(containedNameCandidates(posted('さんぷる', null), [tabelog]).reason ?? '', /エリア/);
  assert.match(containedNameCandidates(posted('さんぷる', '東京都江東区'), [tabelog]).reason ?? '', /エリア/);
  assert.deepEqual(containedNameCandidates(posted('べつの店', '清澄白河'), [tabelog]), { candidates: [], reason: 'Web候補に投稿の店名を含む店舗がない' });
  assert.match(containedNameCandidates(posted('さ', '清澄白河'), [tabelog]).reason ?? '', /短すぎる/);
  assert.match(containedNameCandidates(posted('さんぷる', '清澄白河'), [found('居酒屋 さんぷる', '東京都江東区高橋', 'https://example.com/a')]).reason ?? '', /番地/);
  assert.match(containedNameCandidates(posted('さんぷる', '清澄白河'), [found('居酒屋 さんぷる', '東京都江東区高橋99-9', null)]).reason ?? '', /店舗URL/);
  assert.deepEqual(containedNameCandidates(posted('さんぷる', '清澄白河'), []), { candidates: [], reason: null });
});

test('a proved address in the same ward passes the relaxed area check unless it names another area', () => {
  const proved = (name: string, address: string): PipelineCandidate => found(name, address, 'https://example.com/a', true); const mention = posted('さんぷる', '清澄白河');
  assert.equal(pageVerifiedCandidateValid(mention, proved('居酒屋 さんぷる', '東京都江東区高橋99-9')), false);
  assert.equal(relaxedVerifiedCandidateValid(mention, proved('居酒屋 さんぷる', '東京都江東区高橋99-9')), true);
  assert.equal(relaxedVerifiedCandidateValid(mention, proved('居酒屋 さんぷる', '東京都江東区豊洲9-9-9')), false);
  assert.equal(relaxedVerifiedCandidateValid(mention, proved('居酒屋 さんぷる', '東京都墨田区緑9-9-9')), false);
  assert.equal(relaxedVerifiedCandidateValid(mention, proved('べつの店', '東京都江東区高橋99-9')), false);
  assert.equal(relaxedVerifiedCandidateValid(posted('さんぷる', '清澄白河', '森下店'), proved('さんぷる 豊洲店', '東京都江東区高橋99-9')), false);
  assert.equal(relaxedVerifiedCandidateValid(posted('さんぷる', null), proved('居酒屋 さんぷる', '東京都江東区高橋99-9')), false);
  assert.equal(relaxedVerifiedCandidateValid(mention, found('居酒屋 さんぷる', '東京都江東区高橋99-9', 'https://example.com/a')), false);
});
