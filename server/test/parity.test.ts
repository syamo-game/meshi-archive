import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { z } from 'zod';
import { branchesConflict, evaluateIdentity, extractBranchToken, identitySchema, nameSimilarity, normalizeName } from '../src/resolution.js';
import { sourceAssetFingerprint, sourceAssetIdentity } from '../src/source-identity.js';

const fingerprintInput = z.object({ kind: z.string(), normalized_url: z.string(), title: z.string().nullable(), description: z.string().nullable(), extracted_text: z.string().nullable() });
const paritySchema = z.object({
  source_revision: z.string(),
  sources: z.array(z.object({ input: z.string(), expected: z.object({ source_service: z.string().nullable(), source_item_id: z.string().nullable(), normalized_url: z.string() }).nullable() })),
  normalized: z.array(z.object({ input: z.string(), name: z.string(), branch: z.string().nullable() })),
  pairs: z.array(z.object({ left: z.string(), right: z.string(), ratio: z.number(), branch_conflict: z.boolean() })),
  identities: z.array(z.object({ existing: identitySchema, candidate: identitySchema, expected: z.object({ is_strong_match: z.boolean(), name_similarity: z.number(), matched_fields: z.array(z.string()), conflicting_fields: z.array(z.string()), reason: z.string() }) })),
  fingerprints: z.array(z.object({ input: fingerprintInput, expected: z.string() })),
});
const baseline = paritySchema.parse(JSON.parse(readFileSync(new URL('./parity.json', import.meta.url), 'utf8')));

test('source normalization matches the recorded production implementation', () => {
  assert.equal(baseline.source_revision, 'c790aa246f6ff9ce2aec7fc6a2d9e8fe16c4184d');
  for (const vector of baseline.sources) assert.deepEqual(sourceAssetIdentity(vector.input), vector.expected, vector.input);
});
test('name normalization and branch detection match the recorded implementation', () => {
  for (const vector of baseline.normalized) {
    assert.equal(normalizeName(vector.input), vector.name, vector.input);
    assert.equal(extractBranchToken(vector.input), vector.branch, vector.input);
  }
});
test('similarity preserves SequenceMatcher behavior including repeated long names', () => {
  for (const vector of baseline.pairs) {
    assert.equal(nameSimilarity(vector.left, vector.right), vector.ratio, vector.left + ' / ' + vector.right);
    assert.equal(branchesConflict(vector.left, vector.right), vector.branch_conflict, vector.left + ' / ' + vector.right);
  }
});
test('identity decisions retain strengths, conflicts, and reasons', () => {
  for (const vector of baseline.identities) assert.deepEqual(evaluateIdentity(vector.existing, vector.candidate), vector.expected, vector.candidate.name);
});
test('source fingerprints remain identical for normalized Japanese and Unicode text', () => {
  for (const vector of baseline.fingerprints) assert.equal(sourceAssetFingerprint(vector.input), vector.expected, vector.input.title ?? '');
});
