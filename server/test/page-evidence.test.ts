import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { z } from 'zod';
import { fetchHtmlDocument } from '../src/html-fetch.js';
import { extractRestaurantPageEvidence, provePageCandidate, provePageCandidateByStreet, streetAddress } from '../src/page-evidence.js';
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
test('street-address proof accepts name variants on one address and rejects other lots', () => {
  assert.equal(streetAddress('〒135-0000 東京都江東区高橋9丁目3番1号 サンプルビル2F'), '東京都江東区高橋9-3-1');
  assert.equal(streetAddress('京都府京都市下京区斉藤町134'), null);
  const url = 'https://example.com/shop';
  const candidate = identitySchema.parse({ name: 'SAMPLE （さんぷる）', address: '東京都江東区高橋99-9 サンプルビル1F', canonical_url: url, evidence_url: url });
  const structured = (items: readonly { name: string; address: string }[]) => extractRestaurantPageEvidence('<html><head><title>店舗</title>' + items.map(item => '<script type="application/ld+json">' + JSON.stringify({ '@type': 'Restaurant', name: item.name, address: item.address }) + '</script>').join('') + '</head><body></body></html>', url);
  const one = structured([{ name: 'SAMPLE', address: '1350000 東京都 江東区 高橋99-9' }]);
  assert.equal(provePageCandidate(candidate, one), null);
  const proof = provePageCandidateByStreet(candidate, one); assert.equal(proof?.method, 'structured_data_street'); assert.equal(proof?.candidate.name, 'SAMPLE');
  assert.equal(provePageCandidateByStreet(candidate, structured([{ name: 'SAMPLE', address: '東京都江東区高橋99-90' }])), null);
  assert.equal(provePageCandidateByStreet(candidate, structured([{ name: 'べつの店', address: '東京都江東区高橋99-9' }])), null);
  assert.equal(provePageCandidateByStreet(candidate, structured([{ name: 'SAMPLE', address: '東京都江東区高橋99-9' }, { name: 'SAMPLE 2号店', address: '東京都江東区高橋99-9' }])), null);
  const plain = identitySchema.parse({ name: 'さんぷる食堂', address: '東京都江東区高橋99-9', canonical_url: url, evidence_url: url });
  const visible = (title: string, body: string) => extractRestaurantPageEvidence('<html><head><title>' + title + '</title></head><body><p>' + body + '</p></body></html>', url);
  const shown = provePageCandidateByStreet(plain, visible('さんぷる食堂 | 公式サイト', 'アクセス 〒135-0000 東京都江東区高橋99番9号 サンプルビル1F'));
  assert.equal(shown?.method, 'visible_page_street'); assert.equal(shown?.candidate.name, 'さんぷる食堂'); assert.equal(shown?.candidate.phone, null);
  assert.equal(provePageCandidateByStreet(plain, visible('さんぷる食堂 | 公式サイト', '東京都江東区高橋99-9-1')), null);
  assert.equal(provePageCandidateByStreet(plain, visible('さんぷる食堂 | 公式サイト', '東京都江東区高橋99-90')), null);
  assert.equal(provePageCandidateByStreet(plain, visible('江東区のおすすめ10選', 'さんぷる食堂 東京都江東区高橋99-9')), null);
  assert.equal(provePageCandidateByStreet(identitySchema.parse({ name: 'さんぷる食堂', address: '東京都江東区高橋', canonical_url: url, evidence_url: url }), visible('さんぷる食堂', '東京都江東区高橋')), null);
});
test('HTML retrieval blocks unsafe schemes, credentials and loopback addresses before connecting', async () => {
  for (const value of ['file:///etc/passwd', 'http://user:secret@example.com/', 'http://127.0.0.1/', 'http://[::1]/']) await assert.rejects(fetchHtmlDocument(value), /must use|outside public networks/);
});
