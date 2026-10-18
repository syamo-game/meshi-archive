import assert from 'node:assert/strict';
import { test } from 'node:test';
import { detailSchema, shopsSchema } from '@meshi/contracts';
import { createApp } from '../src/app.js';
import { booleanColumn } from '../src/db/database.js';
import { z } from 'zod';
import { fixture, headers } from './helpers.js';

test('serves the same public 24-item pages and stable area counts', async () => {
  const { database, app, viewer } = await fixture();
  try {
    const firstResponse = await app.request('/api/ui/shops', { headers: headers(viewer) });
    const first = shopsSchema.parse(await firstResponse.json());
    const second = shopsSchema.parse(await (await app.request('/api/ui/shops?page=2', { headers: headers(viewer) })).json());
    assert.equal(firstResponse.headers.get('cache-control'), 'private, no-store');
    assert.equal(first.total, 30); assert.equal(first.per_page, 24); assert.equal(first.items.length, 24); assert.equal(second.items.length, 6);
    await database.execute('UPDATE shops SET category=$1 WHERE id=$2', ['非公開カテゴリ', 31]);
    const privateCategory = shopsSchema.parse(await (await app.request('/api/ui/shops', { headers: headers(viewer) })).json());
    assert.deepEqual(privateCategory.categories, ['和食']);
    assert.equal(new Set([...first.items, ...second.items].map(item => item.id)).size, 30);
    assert.deepEqual(first.areas, [{ value: '木場', label: '木場', count: 20 }, { value: '門前仲町', label: '門前仲町', count: 10 }]);
    const filtered = shopsSchema.parse(await (await app.request('/api/ui/shops?area=門前仲町&page=2', { headers: headers(viewer) })).json());
    assert.equal(filtered.total, 10); assert.equal(filtered.page, 1); assert.deepEqual(filtered.areas, first.areas);
    const visited = shopsSchema.parse(await (await app.request('/api/ui/shops?status=visited', { headers: headers(viewer) })).json());
    assert.equal(visited.total, 1); assert.equal(visited.items[0]?.id, 1);
  } finally { await database.close(); }
});

test('members update shared ratings and preserve exact memo whitespace', async () => {
  const { database, app, viewer } = await fixture();
  try {
    const memo = '  1行目\n\n3行目\n末尾  ';
    const response = await app.request('/api/ui/shops/2/annotation', { method: 'PATCH', headers: { ...headers(viewer), 'Content-Type': 'application/json' }, body: JSON.stringify({ version: 1, rating: 5, memo }) });
    assert.equal(response.status, 200);
    const shop = detailSchema.parse(await response.json());
    assert.equal(shop.version, 2); assert.equal(shop.rating, 5); assert.equal(shop.memo, memo); assert.equal(shop.visited_at, null);
    const clear = detailSchema.parse(await (await app.request('/api/ui/shops/2/annotation', { method: 'PATCH', headers: { ...headers(viewer), 'Content-Type': 'application/json' }, body: JSON.stringify({ version: 2, rating: null }) })).json());
    assert.equal(clear.rating, null); assert.equal(clear.memo, memo);
    const stored = await database.query('SELECT is_visited FROM shops WHERE id=2', z.object({ is_visited: booleanColumn }));
    assert.equal(stored[0]?.is_visited, false);
  } finally { await database.close(); }
});

test('rejects anonymous, CSRF, hidden-shop, invalid-input, and stale writes', async () => {
  const { database, app, viewer, admin } = await fixture();
  try {
    assert.equal((await app.request('/api/ui/shops')).status, 401);
    assert.equal((await app.request('/api/ui/editor-options', { headers: headers(viewer) })).status, 403);
    assert.equal((await app.request('/api/ui/shops/31', { headers: headers(viewer) })).status, 404);
    assert.equal((await app.request('/api/ui/shops/31', { headers: headers(admin) })).status, 200);
    const mutate = (body: object, id = 2, csrf = true): Promise<Response> => app.request(`/api/ui/shops/${id}/annotation`, { method: 'PATCH', headers: { Cookie: headers(viewer).Cookie, ...(csrf ? { 'X-CSRF-Token': headers(viewer)['X-CSRF-Token'] } : {}), 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    assert.equal((await mutate({ version: 1, rating: 3 }, 2, false)).status, 403);
    assert.equal((await mutate({ version: 1, rating: 3 }, 31)).status, 404);
    for (const rating of [0, 6, -1, true, '4', 1.5]) assert.equal((await mutate({ version: 1, rating })).status, 422);
    assert.equal((await mutate({ version: 1, memo: 'x'.repeat(20001) })).status, 422);
    assert.equal((await mutate({ version: 1, shop_name: 'forbidden' })).status, 422);
    assert.equal((await mutate({ version: 1 })).status, 422);
    const results = await Promise.all([mutate({ version: 1, rating: 3 }), mutate({ version: 1, rating: 4 })]);
    assert.deepEqual(results.map(response => response.status).sort(), [200, 409]);
    assert.equal((await database.query('SELECT version FROM shops WHERE id=2', z.object({ version: z.number() })))[0]?.version, 2);
  } finally { await database.close(); }
});

test('search escapes literal wildcard characters and does not execute SQL input', async () => {
  const { database, app, viewer } = await fixture();
  try {
    await database.execute('UPDATE shops SET memo=$1 WHERE id=$2', ['100%_確認', 1]);
    const literal = shopsSchema.parse(await (await app.request('/api/ui/shops?q=' + encodeURIComponent('%_'), { headers: headers(viewer) })).json());
    assert.equal(literal.total, 1); assert.equal(literal.items[0]?.id, 1);
    const attack = shopsSchema.parse(await (await app.request('/api/ui/shops?q=' + encodeURIComponent("' OR 1=1; --"), { headers: headers(viewer) })).json());
    assert.equal(attack.total, 0);
    assert.equal((await app.request('/api/ui/shops?sort=DROP%20TABLE', { headers: headers(viewer) })).status, 422);
  } finally { await database.close(); }
});

test('read-only blocks annotations while session bootstrap and logout work', async () => {
  const { database, app, viewer, config } = await fixture();
  try {
    const readOnly = createApp(database, { ...config, readOnly: true });
    const response = await readOnly.request('/api/ui/shops/2/annotation', { method: 'PATCH', headers: { ...headers(viewer), 'Content-Type': 'application/json' }, body: JSON.stringify({ version: 1, rating: 3 }) });
    assert.equal(response.status, 403);
    const bootstrap = await app.request('/api/ui/session'); assert.equal(bootstrap.status, 200); assert.match(bootstrap.headers.get('set-cookie') ?? '', /HttpOnly/);
    const logout = await readOnly.request('/logout', { method: 'POST', headers: { ...headers(viewer), 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ csrf_token: viewer.session.stored.csrf_token }) });
    assert.equal(logout.status, 302);
    assert.equal((await app.request('/api/ui/shops', { headers: headers(viewer) })).status, 401);
  } finally { await database.close(); }
});

test('keeps a posted Tabelog link and omits conflicting source identities', async () => {
  const { database, app, viewer } = await fixture();
  try {
    const posted = 'https://tabelog.com/tokyo/A1313/A131303/123456/dtlmenu/';
    await database.execute('UPDATE shops SET external_source=$1,external_id=$2,canonical_url=$3 WHERE id=$4', ['食べログ', '123456', 'https://tabelog.com/tokyo/A1313/A131303/123456/', 1]);
    await database.execute('UPDATE shop_mentions SET source_url=NULL WHERE shop_id=$1', [1]);
    await database.execute('INSERT INTO source_assets(message_id,kind,url,fetch_status,created_at) VALUES($1,$2,$3,$4,$5)', ['90000000000000001', 'link', posted, 'available', new Date().toISOString()]);
    const shop = detailSchema.parse(await (await app.request('/api/ui/shops/1', { headers: headers(viewer) })).json());
    assert.equal(shop.source_url, posted); assert.equal(shop.source_label, '食べログ'); assert.equal(shop.canonical_url, null);
    await database.execute('UPDATE shops SET canonical_url=$1 WHERE id=$2', ['https://tabelog.com/tokyo/A1313/A131303/654321/', 1]);
    const conflict = detailSchema.parse(await (await app.request('/api/ui/shops/1', { headers: headers(viewer) })).json());
    assert.equal(conflict.source_url, null); assert.match(conflict.canonical_url ?? '', /654321/);
  } finally { await database.close(); }
});

test('category filters include descendants and aliases without guessing legacy compounds', async () => {
  const { database, app, viewer } = await fixture();
  try {
    const values = ['ピザ', 'ナポリピザ', 'ニューヨークピザ', 'イタリア料理', 'イタリアン・パスタ・ピザ', 'ピッツァ', null, '', '寿司・回転寿司', '回転寿司', '鮨', 'ジェラート', 'アイスクリーム', '　', '\t'];
    for (const [index, value] of values.entries()) await database.execute('UPDATE shops SET category=$1 WHERE id=$2', [value, index + 1]);
    await database.execute('UPDATE shops SET category=$1 WHERE id=$2', ['デトロイトピザ', 31]);
    const list = async (category: string): Promise<ReturnType<typeof shopsSchema.parse>> => shopsSchema.parse(await (await app.request('/api/ui/shops?' + new URLSearchParams({ category }), { headers: headers(viewer) })).json());
    const ids = async (category: string): Promise<number[]> => (await list(category)).items.map(shop => shop.id).sort((a, b) => a - b);
    assert.deepEqual(await ids('ピザ'), [1, 2, 3, 6]);
    assert.deepEqual(await ids('ピッツァ'), [1, 2, 3, 6]);
    assert.deepEqual(await ids('ニューヨークピザ'), [3]);
    assert.deepEqual(await ids('イタリアン'), [4]);
    assert.deepEqual(await ids('イタリアン・パスタ・ピザ'), [5]);
    assert.deepEqual(await ids('寿司'), [10, 11]);
    assert.deepEqual(await ids('回転寿司'), [10]);
    assert.deepEqual(await ids('寿司・回転寿司'), [9]);
    assert.deepEqual(await ids('__none__'), [7, 8, 14, 15]);
    assert.deepEqual(await ids("' OR 1=1 --"), []);
    assert.deepEqual(await ids('ジェラート'), [12]);
    assert.deepEqual(await ids('アイスクリーム'), [13]);
    const all = await list('');
    const pizzaOptions = all.category_groups.find(group => group.label === 'パスタ・ピザ');
    assert.deepEqual(pizzaOptions?.options.map(option => [option.value, option.depth]), [['ピザ', 0], ['ナポリピザ', 1], ['ニューヨークピザ', 1]]);
    assert.ok(!all.categories.includes('デトロイトピザ'));
    assert.ok(all.category_groups.find(group => group.label === '未整理')?.options.some(option => option.value === '寿司・回転寿司'));
    const sushi = await list('寿司');
    assert.equal(sushi.items.find(shop => shop.id === 11)?.category, '寿司');
    assert.deepEqual(sushi.category_groups, all.category_groups);
    const search = shopsSchema.parse(await (await app.request('/api/ui/shops?' + new URLSearchParams({ q: 'イタリアン' }), { headers: headers(viewer) })).json());
    assert.deepEqual(search.items.map(shop => shop.id).sort((a, b) => a - b), [4, 5]);
  } finally { await database.close(); }
});
