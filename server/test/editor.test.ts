import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { test } from 'node:test';
import sharp from 'sharp';
import { z } from 'zod';
import { detailSchema } from '@meshi/contracts';
import { createApp } from '../src/app.js';
import { storeUploadedImage } from '../src/images.js';
import { fixture, headers } from './helpers.js';

function form(): FormData {
  const result = new FormData();
  for (const [key, value] of Object.entries({ expected_version: '1', shop_name: '入力した店名', branch_name: '', area: '木場', category: '和食', url: '', address: '', phone: '', memo: '  1行目\n\n3行目  ', rating: '', visited_at: '', return_to: '/' })) result.set(key, value);
  return result;
}
async function temporaryDirectory(): Promise<{ path: string; clean: () => Promise<void> }> {
  const prefix = 'meshi-ts-image-test-'; const path = await mkdtemp(join(tmpdir(), prefix));
  return { path, clean: async () => {
    const absolute = resolve(path);
    if (dirname(absolute) !== resolve(tmpdir()) || !basename(absolute).startsWith(prefix)) throw new Error('Refusing cleanup outside the test image directory');
    await rm(absolute, { recursive: true, force: true });
  } };
}

test('metadata saves preserve memo whitespace and reject member edits', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    assert.equal((await app.request('/shop/2/edit', { method: 'POST', headers: headers(viewer), body: form() })).status, 403);
    const response = await app.request('/shop/2/edit', { method: 'POST', headers: { ...headers(admin), 'X-Requested-With': 'XMLHttpRequest' }, body: form() });
    assert.equal(response.status, 200);
    const result = z.object({ redirect_url: z.string() }).parse(await response.json());
    assert.equal(result.redirect_url, '/shop/2?saved=true&return_to=%2F');
    const shop = detailSchema.parse(await (await app.request('/api/ui/shops/2', { headers: headers(admin) })).json());
    // Multipart text fields use CRLF, as in the previous server.
    assert.equal(shop.name, '入力した店名'); assert.equal(shop.memo, '  1行目\r\n\r\n3行目  '); assert.equal(shop.version, 2);
  } finally { await database.close(); }
});

test('conflict requires explicit choices and retains a concurrent member memo', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    await app.request('/api/ui/shops/2/annotation', { method: 'PATCH', headers: { ...headers(viewer), 'Content-Type': 'application/json' }, body: JSON.stringify({ version: 1, memo: '別メンバーのメモ' }) });
    const stale = await app.request('/shop/2/edit', { method: 'POST', headers: headers(admin), body: form() });
    assert.equal(stale.status, 409);
    const confirmed = form(); confirmed.set('confirmed_version', '2'); confirmed.set('confirm_conflict', 'on');
    confirmed.append('conflict_choices', 'shop_name:input'); confirmed.append('conflict_choices', 'memo:latest');
    const response = await app.request('/shop/2/edit', { method: 'POST', headers: { ...headers(admin), 'X-Requested-With': 'XMLHttpRequest' }, body: confirmed });
    assert.equal(response.status, 200);
    const shop = detailSchema.parse(await (await app.request('/api/ui/shops/2', { headers: headers(admin) })).json());
    assert.equal(shop.name, '入力した店名'); assert.equal(shop.memo, '別メンバーのメモ'); assert.equal(shop.version, 3);
  } finally { await database.close(); }
});

test('real image uploads produce a reusable WebP and protected media response', async () => {
  const directory = await temporaryDirectory();
  const { database, admin, viewer, config } = await fixture();
  try {
    const image = await sharp({ create: { width: 1200, height: 800, channels: 3, background: '#a03010' } }).png().toBuffer();
    const first = await storeUploadedImage(image, directory.path); const second = await storeUploadedImage(image, directory.path);
    assert.equal(first.key, second.key);
    const encoded = await readFile(first.path); const metadata = await sharp(encoded).metadata();
    assert.equal(metadata.format, 'webp'); assert.equal(metadata.width, 960); assert.equal(metadata.height, 540);
    const app = createApp(database, { ...config, imageDirectory: directory.path });
    const data = form(); data.set('photo', new Blob([new Uint8Array(image)], { type: 'image/png' }), 'photo.png');
    const response = await app.request('/shop/2/edit', { method: 'POST', headers: { ...headers(admin), 'X-Requested-With': 'XMLHttpRequest' }, body: data });
    assert.equal(response.status, 200);
    const shop = detailSchema.parse(await (await app.request('/api/ui/shops/2', { headers: headers(admin) })).json());
    assert.equal(shop.image_url, '/media/shop-uploads/' + first.key + '.webp');
    assert.equal((await app.request(shop.image_url ?? '', { headers: headers(viewer) })).headers.get('content-type'), 'image/webp');
    assert.equal((await app.request(shop.image_url ?? '')).status, 401);
  } finally { await database.close(); await directory.clean(); }
});

test('invalid photos roll back reserved versions and all metadata changes', async () => {
  const { app, database, admin } = await fixture();
  try {
    const data = form(); data.set('photo', new Blob(['<svg xmlns="http://www.w3.org/2000/svg"/>'], { type: 'image/jpeg' }), 'photo.jpg');
    const response = await app.request('/shop/2/edit', { method: 'POST', headers: headers(admin), body: data });
    assert.equal(response.status, 422);
    const shop = detailSchema.parse(await (await app.request('/api/ui/shops/2', { headers: headers(admin) })).json());
    assert.equal(shop.version, 1); assert.equal(shop.name, '店舗02'); assert.equal(shop.image_url, null);
  } finally { await database.close(); }
});

test('delete protects merged targets and preserves original mention records', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    const remove = (id: number, version: number): Promise<Response> => app.request(`/api/ui/admin/shops/${id}/delete`, { method: 'POST', headers: { ...headers(admin), 'Content-Type': 'application/json' }, body: JSON.stringify({ version }) });
    assert.equal((await app.request('/api/ui/admin/shops/3/delete', { method: 'POST', headers: { ...headers(viewer), 'Content-Type': 'application/json' }, body: JSON.stringify({ version: 1 }) })).status, 403);
    assert.equal((await remove(3, 99)).status, 409);
    await database.execute('INSERT INTO shop_redirects(source_shop_id,target_shop_id,reason,created_at) VALUES($1,$2,$3,$4)', [999, 2, 'merge', new Date().toISOString()]);
    assert.equal((await remove(2, 1)).status, 409);
    assert.equal((await remove(3, 1)).status, 200);
    assert.equal((await app.request('/api/ui/shops/3', { headers: headers(admin) })).status, 404);
    const mention = await database.query('SELECT shop_id FROM shop_mentions WHERE message_id=$1', z.object({ shop_id: z.number().nullable() }), ['90000000000000003']);
    assert.deepEqual(mention, [{ shop_id: null }]);
  } finally { await database.close(); }
});
