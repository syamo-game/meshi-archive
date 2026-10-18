import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { z } from 'zod';
import { fetchHtmlDocument } from '../src/html-fetch.js';
import { extractRestaurantPageEvidence, provePageCandidate } from '../src/page-evidence.js';
import { identitySchema } from '../src/resolution.js';

const evidence = z.object({ headings: z.array(z.string()), text_blocks: z.array(z.string()), visible_text: z.string(), structured_candidates: z.array(identitySchema), page_sha256: z.string() });
const proof = z.object({ method: z.enum(['structured_data', 'visible_page']), page_sha256: z.string(), candidate: identitySchema }).nullable();
const reference = z.object({ reference: z.literal('c790aa246f6ff9ce2aec7fc6a2d9e8fe16c4184d'), synthetic: z.literal(true), cases: z.array(z.object({ html: z.string(), url: z.string(), candidate: identitySchema, evidence, proof })) });
test('HTML parsing and page proofs match the original parser on 16 source cases', async () => {
  const data = reference.parse(JSON.parse(await readFile(new URL('./page-parity.json', import.meta.url), 'utf8')));
  for (const item of data.cases) {
    const parsed = extractRestaurantPageEvidence(item.html, item.url);
    assert.deepEqual(parsed, item.evidence, item.html);
    assert.deepEqual(provePageCandidate(item.candidate, parsed), item.proof, item.html);
  }
});
test('HTML retrieval blocks unsafe schemes, credentials and loopback addresses before connecting', async () => {
  for (const value of ['file:///etc/passwd', 'http://user:secret@example.com/', 'http://127.0.0.1/', 'http://[::1]/']) await assert.rejects(fetchHtmlDocument(value), /must use|outside public networks/);
});
