import assert from 'node:assert/strict';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import sharp from 'sharp';
import { z } from 'zod';
import { openDatabase } from '../src/db/database.js';
import { migrateDatabase } from '../src/db/migrate.js';
import { envelopeSchema } from '../src/ingestion.js';
import type { MessageEnvelope } from '../src/ingestion.js';
import { attachMissingShopPhotos, pageShareImage } from '../src/shop-photos.js';
import type { PhotoFetchers } from '../src/shop-photos.js';

const now = '2026-11-17T00:00:00Z';
async function setup(shops: readonly { name: string; url: string | null; image?: string }[], messageId = '90000000000000300'): Promise<{ database: ReturnType<typeof openDatabase>; directory: string; ids: number[] }> {
  const database = openDatabase('sqlite://'); await migrateDatabase(database);
  const directory = await mkdtemp(join(tmpdir(), 'meshi-shop-photo-test-'));
  await database.execute("INSERT INTO messages(message_id,channel_id,content,source_created_at,is_target,processing_status) VALUES($1,'123456789012345678','synthetic',$2,$3,'succeeded')", [messageId, now, true]);
  const ids: number[] = [];
  for (const [index, shop] of shops.entries()) {
    const row = (await database.query('INSERT INTO shops(shop_name,canonical_url,image_key,is_visited,created_at,updated_at,version) VALUES($1,$2,$3,$4,$5,$5,1) RETURNING id', z.object({ id: z.number() }), [shop.name, shop.url, shop.image ?? null, false, now]))[0];
    if (!row) throw new Error('Shop insert returned no row'); ids.push(row.id);
    await database.execute("INSERT INTO shop_mentions(message_id,shop_id,occurrence_index,extracted_name,resolution_status,review_status,metadata_review_status,extraction_source,version,created_at) VALUES($1,$2,$3,$4,'resolved','approved','approved','responses_structured',1,$5)", [messageId, row.id, index, shop.name, now]);
  }
  return { database, directory, ids };
}
function envelope(assets: MessageEnvelope['assets'] = [], messageId = '90000000000000300'): MessageEnvelope {
  return envelopeSchema.parse({ message_id: messageId, channel_id: '123456789012345678', content: 'synthetic', created_at: now, assets, omitted_asset_count: 0 });
}
const imageKey = z.object({ image_key: z.string().nullable(), version: z.number() });
async function photo(color: string): Promise<Uint8Array> { return sharp({ create: { width: 640, height: 480, channels: 3, background: color } }).jpeg().toBuffer(); }

test('share image tags are read in priority order and made absolute', () => {
  assert.equal(pageShareImage('<meta content="/img/a.jpg?x=1&amp;y=2" property="og:image">', 'https://shop.example.com/menu/'), 'https://shop.example.com/img/a.jpg?x=1&y=2');
  assert.equal(pageShareImage('<meta name="twitter:image" content="https://cdn.example.com/t.jpg"><meta property="og:image" content="https://cdn.example.com/o.jpg">', 'https://shop.example.com/'), 'https://cdn.example.com/o.jpg');
  assert.equal(pageShareImage('<meta name="twitter:image" content="https://cdn.example.com/t.jpg">', 'https://shop.example.com/'), 'https://cdn.example.com/t.jpg');
  assert.equal(pageShareImage('<meta property="og:image" content="javascript:alert(1)">', 'https://shop.example.com/'), null);
  assert.equal(pageShareImage('<title>no image</title>', 'https://shop.example.com/'), null);
});

test('a one-shop post uses its own image and stores a real photo file', async () => {
  const { database, directory, ids } = await setup([{ name: '合成食堂', url: 'https://shop.example.com/' }]);
  const calls: string[] = [];
  const fetchers: PhotoFetchers = { postImage: async url => { calls.push('post:' + url); return photo('#a03010'); }, page: async url => { calls.push('page:' + url); throw new Error('page must not be read'); }, pageImage: async url => { calls.push('pageImage:' + url); throw new Error('page image must not be read'); } };
  try {
    const attached = await attachMissingShopPhotos(database, directory, envelope([{ kind: 'image', url: 'https://jf.x.com/images/preview/1' }, { kind: 'image', url: 'https://pbs.twimg.com/media/synthetic.jpg' }]), fetchers);
    assert.deepEqual(attached, [{ shop_id: ids[0], source: 'post_image', url: 'https://pbs.twimg.com/media/synthetic.jpg' }]);
    assert.deepEqual(calls, ['post:https://pbs.twimg.com/media/synthetic.jpg'], 'hosts outside the image allowlist are skipped');
    const row = (await database.query('SELECT image_key,version FROM shops WHERE id=$1', imageKey, [ids[0]]))[0];
    assert.ok(row?.image_key); assert.equal(row.version, 2);
    assert.ok((await stat(join(directory, 'uploads', row.image_key + '.webp'))).size > 0);
  } finally { await database.close(); await rm(directory, { recursive: true, force: true }); }
});

test('a multi-shop post uses each shop page share image and keeps photos already set', async () => {
  const { database, directory, ids } = await setup([{ name: '合成A', url: 'https://a.example.com/' }, { name: '合成B', url: 'https://b.example.com/' }, { name: '合成C', url: null, image: 'existing' }]);
  const calls: string[] = [];
  const fetchers: PhotoFetchers = {
    postImage: async url => { calls.push('post:' + url); throw new Error('post image must not be used for several shops'); },
    page: async url => { calls.push('page:' + url); return { html: url.includes('a.example') ? '<meta property="og:image" content="/a.jpg">' : '<title>none</title>', final_url: url }; },
    pageImage: async url => { calls.push('pageImage:' + url); return photo('#3060a0'); },
  };
  try {
    const attached = await attachMissingShopPhotos(database, directory, envelope([{ kind: 'image', url: 'https://pbs.twimg.com/media/synthetic.jpg' }]), fetchers);
    assert.deepEqual(attached, [{ shop_id: ids[0], source: 'shop_page_og_image', url: 'https://a.example.com/a.jpg' }]);
    assert.deepEqual(calls, ['page:https://a.example.com/', 'pageImage:https://a.example.com/a.jpg', 'page:https://b.example.com/']);
    const rows = await database.query('SELECT image_key,version FROM shops ORDER BY id', imageKey);
    assert.ok(rows[0]?.image_key); assert.equal(rows[1]?.image_key, null); assert.deepEqual(rows[2], { image_key: 'existing', version: 1 });
  } finally { await database.close(); await rm(directory, { recursive: true, force: true }); }
});

test('a broken post image falls through to the shop page, and an invalid image file is not stored', async () => {
  const { database, directory, ids } = await setup([{ name: '合成D', url: 'https://d.example.com/' }]);
  const fetchers: PhotoFetchers = {
    postImage: async () => { throw new Error('Image request failed: status=404'); },
    page: async url => ({ html: '<meta property="og:image" content="https://d.example.com/d.jpg">', final_url: url }),
    pageImage: async () => new TextEncoder().encode('not an image'),
  };
  try {
    assert.deepEqual(await attachMissingShopPhotos(database, directory, envelope([{ kind: 'image', url: 'https://pbs.twimg.com/media/synthetic.jpg' }]), fetchers), []);
    assert.equal((await database.query('SELECT image_key,version FROM shops WHERE id=$1', imageKey, [ids[0]]))[0]?.image_key, null);
  } finally { await database.close(); await rm(directory, { recursive: true, force: true }); }
});

test('page images are fetched only from public networks', async () => {
  const { fetchPublicImage } = await import('../src/html-fetch.js');
  for (const value of ['file:///etc/passwd', 'http://user:secret@example.com/a.jpg', 'http://127.0.0.1/a.jpg', 'http://[::1]/a.jpg']) await assert.rejects(fetchPublicImage(value), /Image URL must|outside public networks/);
});
