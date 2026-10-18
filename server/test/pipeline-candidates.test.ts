import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { z } from 'zod';
import { extractedMentionSchema } from '../src/ai-models.js';
import { candidateForNewShop, dedupeCandidates, pipelineCandidateSchema, structuredBlockReason, topWebCandidate, verifyFromStructuredPage } from '../src/pipeline-candidates.js';

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
