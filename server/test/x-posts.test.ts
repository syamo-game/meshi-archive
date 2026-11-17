import assert from 'node:assert/strict';
import { test } from 'node:test';
import { envelopeSchema, withXPosts } from '../src/ingestion.js';
import { fetchXPost, xStatusId, XPostError } from '../src/x-posts.js';
import { withJsonServer } from './helpers.js';

const post = { id_str: '1900000000000000001', text: '合成の本文です。鍋の季節になりました。\n\n飲み物もおかわりしました。', user: { name: 'Synthetic', screen_name: 'synthetic_user' }, photos: [{ url: 'https://pbs.twimg.com/media/synthetic.jpg' }], quoted_tweet: { id_str: '1900000000000000000', text: '引用元の本文' } };

test('X status URLs give their post ID and other URLs do not', () => {
  assert.equal(xStatusId('https://x.com/synthetic_user/status/1900000000000000001?s=46'), '1900000000000000001');
  assert.equal(xStatusId('https://twitter.com/synthetic_user/status/1900000000000000001'), '1900000000000000001');
  assert.equal(xStatusId('https://x.com/synthetic_user'), null);
  assert.equal(xStatusId('https://example.com/synthetic_user/status/1900000000000000001'), null);
});

test('full X post text is read from the embed endpoint and added to the message', async () => {
  await withJsonServer(() => ({ status: 200, body: post }), async (baseUrl, requests) => {
    const fetched = await fetchXPost(post.id_str, baseUrl + '/tweet-result');
    assert.equal(fetched.text, post.text); assert.deepEqual(fetched.photo_urls, ['https://pbs.twimg.com/media/synthetic.jpg']); assert.equal(fetched.quoted?.text, '引用元の本文');
    const url = new URL(requests[0]?.url ?? '', baseUrl); assert.equal(url.searchParams.get('id'), post.id_str); assert.ok(url.searchParams.get('token'));
    const link = 'https://x.com/synthetic_user/status/1900000000000000001?s=46';
    const envelope = envelopeSchema.parse({ message_id: '90000000000000001', channel_id: '123456789012345678', content: link + '\n[Embed Description] 合成の本文です。鍋の季節…', created_at: '2026-11-17T00:00:00Z', assets: [{ kind: 'link', url: link }, { kind: 'embed', url: link }], omitted_asset_count: 0 });
    const enriched = await withXPosts(envelope, id => fetchXPost(id, baseUrl + '/tweet-result'));
    assert.match(enriched.content, /\[X Post Full Text\] id=1900000000000000001 author=Synthetic \(@synthetic_user\)\n合成の本文です。鍋の季節/);
    assert.match(enriched.content, /飲み物もおかわりしました。\n\[X Post Photos\] 1\n\[X Quoted Post\] id=1900000000000000000\n引用元の本文/);
    assert.equal(requests.length, 2, 'the same post linked twice is fetched once per call');
  });
});

test('a failed X request raises with its status and leaves the message unchanged', async () => {
  await withJsonServer(() => ({ status: 404, body: {} }), async baseUrl => {
    await assert.rejects(fetchXPost(post.id_str, baseUrl + '/tweet-result'), (error: unknown) => error instanceof XPostError && /id=1900000000000000001 status=404/.test(error.message));
    const link = 'https://x.com/synthetic_user/status/1900000000000000001';
    const envelope = envelopeSchema.parse({ message_id: '90000000000000001', channel_id: '123456789012345678', content: link, created_at: '2026-11-17T00:00:00Z', assets: [{ kind: 'link', url: link }], omitted_asset_count: 0 });
    assert.deepEqual(await withXPosts(envelope, id => fetchXPost(id, baseUrl + '/tweet-result')), envelope);
  });
  await withJsonServer(() => ({ status: 200, body: { ...post, id_str: '1' } }), async baseUrl => {
    await assert.rejects(fetchXPost(post.id_str, baseUrl + '/tweet-result'), /returned another post/);
  });
});
