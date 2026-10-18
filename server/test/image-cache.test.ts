import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { test } from 'node:test';
import sharp from 'sharp';
import { z } from 'zod';
import { createApp } from '../src/app.js';
import { ImageCache, publicAddress, sourceImagePath } from '../src/image-cache.js';
import { storeUploadedImage, validateImageSourceUrl, writeImageFile } from '../src/images.js';
import { mentionRecord, shopRecord } from '../src/records.js';
import { fixture, headers } from './helpers.js';

async function directory(): Promise<{ path: string; clean: () => Promise<void> }> {
  const prefix = 'meshi-ts-cache-test-'; const path = await mkdtemp(join(tmpdir(), prefix));
  return { path, clean: async () => { const absolute = resolve(path); if (dirname(absolute) !== resolve(tmpdir()) || !basename(absolute).startsWith(prefix)) throw new Error('Refusing cleanup outside the test cache'); await rm(absolute, { recursive: true, force: true }); } };
}
const originalUrl = 'https://cdn.discordapp.com/attachments/100/200/photo.png?ex=synthetic&is=synthetic&hm=synthetic';
test('image URLs and resolved addresses reject private or unapproved destinations', () => {
  for (const value of ['http://cdn.discordapp.com/a.png', 'https://cdn.discordapp.com:444/a.png', 'https://cdn.discordapp.com.evil.example/a.png', 'https://name:secret@cdn.discordapp.com/a.png', 'https://127.0.0.1/a.png', 'https://[::1]/a.png']) assert.throws(() => validateImageSourceUrl(value));
  for (const address of ['127.0.0.1', '10.0.0.1', '100.100.100.200', '169.254.169.254', '172.16.1.1', '192.168.1.1', '0.0.0.0', '224.0.0.1', '::1', '::ffff:127.0.0.1', 'fc00::1', 'fe80::1', '2001:db8::1', '2002:7f00:1::']) assert.equal(publicAddress(address), false, address);
  assert.equal(publicAddress('1.1.1.1'), true); assert.equal(publicAddress('2606:4700:4700::1111'), true);
});

test('saved originals survive rotating Discord signatures and AI uses only saved files', async () => {
  const temporary = await directory();
  try {
    const cache = new ImageCache(temporary.path);
    const rotating = 'https://media.discordapp.net/attachments/100/200/photo.png?ex=new&is=new&hm=new';
    assert.equal(sourceImagePath(originalUrl, temporary.path), sourceImagePath(rotating, temporary.path));
    const source = await sharp({ create: { width: 2400, height: 1200, channels: 4, background: '#00000000' } }).png().toBuffer();
    await writeImageFile(sourceImagePath(originalUrl, temporary.path), source);
    const loaded = await cache.original(rotating); assert.deepEqual(loaded.bytes, source); assert.equal(loaded.contentType, 'image/png');
    const output = await cache.processed(rotating); const metadata = await sharp(output).metadata(); assert.equal(metadata.format, 'webp'); assert.equal(metadata.width, 960); assert.equal(metadata.height, 540); assert.equal(metadata.hasAlpha, false);
    const pixel = await sharp(output).resize(1, 1).raw().toBuffer(); assert.ok([...pixel].every(value => value >= 250));
    const ai = await cache.aiImages([rotating, originalUrl, 'https://pbs.twimg.com/missing.png']); assert.equal(ai.length, 2);
    const decoded = Buffer.from((ai[0] ?? '').split(',')[1] ?? '', 'base64'); const aiMetadata = await sharp(decoded).metadata(); assert.equal(aiMetadata.format, 'jpeg'); assert.equal(aiMetadata.width, 2048); assert.equal(aiMetadata.height, 1024);
    await assert.rejects(cache.aiImages(['https://pbs.twimg.com/never-saved.png']), /ENOENT/);
  } finally { await temporary.clean(); }
});

test('invalid saved images fail instead of silently downloading replacement content', async () => {
  const temporary = await directory();
  try {
    const path = sourceImagePath(originalUrl, temporary.path); await writeImageFile(path, new TextEncoder().encode('<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'));
    await assert.rejects(new ImageCache(temporary.path).original(originalUrl), /format or dimensions/);
    await writeFile(path, new Uint8Array([0x89, 0x50]));
    await assert.rejects(new ImageCache(temporary.path).original(originalUrl));
  } finally { await temporary.clean(); }
});

test('cached review evidence and photo approval use real files and permission guards', async () => {
  const temporary = await directory(); const { database, config, admin, viewer } = await fixture();
  try {
    const source = (await database.query('SELECT * FROM shop_mentions WHERE shop_id=31', mentionRecord))[0]; assert.ok(source);
    const image = await sharp({ create: { width: 1000, height: 600, channels: 3, background: '#204080' } }).png().toBuffer();
    await writeImageFile(sourceImagePath(originalUrl, temporary.path), image);
    const asset = (await database.query("INSERT INTO source_assets(message_id,kind,url,fetch_status,created_at) VALUES($1,'image',$2,'available',$3) RETURNING id", z.object({ id: z.number() }), [source.message_id, originalUrl, new Date().toISOString()]))[0]; assert.ok(asset);
    const app = createApp(database, { ...config, imageDirectory: temporary.path });
    assert.equal((await app.request(`/api/admin/evidence/image/${asset.id}`, { headers: headers(viewer) })).status, 403);
    const original = await app.request(`/api/admin/evidence/image/${asset.id}`, { headers: headers(admin) }); assert.equal(original.status, 200); assert.deepEqual(Buffer.from(await original.arrayBuffer()), image);
    const preview = await app.request(`/api/admin/evidence/photo/${asset.id}`, { headers: headers(admin) }); assert.equal(preview.status, 200); assert.equal(preview.headers.get('content-type'), 'image/webp');
    const approval = await app.request(`/api/admin/reviews/${source.id}/decision`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'edit_and_approve', expected_version: 1, shop_version: 1, confirm_metadata: true, photo_asset_id: asset.id, shop: { shop_name: '店舗31', area: '門前仲町', category: '定食', branch_name: null, canonical_url: null } }) });
    assert.equal(approval.status, 200);
    const shop = (await database.query('SELECT * FROM shops WHERE id=31', shopRecord))[0]; assert.ok(shop?.image_key);
    const stored = await readFile(join(temporary.path, 'uploads', shop.image_key + '.webp')); assert.equal((await sharp(stored).metadata()).width, 960);
    assert.equal((await app.request('/media/shop-uploads/' + shop.image_key + '.webp', { headers: headers(viewer) })).status, 200);
  } finally { await database.close(); await temporary.clean(); }
});

test('transparent uploads use the same white photo background as the source processor', async () => {
  const temporary = await directory();
  try {
    const source = await sharp({ create: { width: 10, height: 10, channels: 4, background: '#00000000' } }).png().toBuffer();
    const stored = await storeUploadedImage(source, temporary.path); const bytes = await readFile(stored.path); const metadata = await sharp(bytes).metadata(); assert.equal(metadata.hasAlpha, false);
    assert.ok([...(await sharp(bytes).resize(1, 1).raw().toBuffer())].every(value => value >= 250));
  } finally { await temporary.clean(); }
});
