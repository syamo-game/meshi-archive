import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { test } from 'node:test';
import { z } from 'zod';
import { shopsSchema } from '@meshi/contracts';
import { createApp } from '../src/app.js';
import { openDatabase, timestampColumn } from '../src/db/database.js';
import { migrateDatabase, verifyDatabase } from '../src/db/migrate.js';
import { duplicateReport } from '../src/operations/duplicates.js';
import { transferDatabase } from '../src/operations/transfer.js';
import { fixture, headers } from './helpers.js';

const url = process.env.TEST_POSTGRES_URL;
test('PostgreSQL migration, transfer, sessions, concurrent annotations and duplicate merges preserve data', { skip: url ? false : 'TEST_POSTGRES_URL is not set' }, async () => {
  assert.ok(url); const schema = 'meshi_test_' + randomUUID().replaceAll('-', '');
  assert.match(schema, /^meshi_test_[a-f0-9]{32}$/);
  const control = openDatabase(url); const scoped = new URL(url); scoped.searchParams.set('options', '-c search_path=' + schema);
  const database = openDatabase(scoped.href); const source = await fixture();
  try {
    await control.execute(`CREATE SCHEMA ${schema}`);
    await Promise.all([migrateDatabase(database), migrateDatabase(database)]); await verifyDatabase(database);
    await source.database.execute("UPDATE shops SET created_at='2026-10-18 01:02:03.123456' WHERE id=1");
    assert.equal((await transferDatabase(source.database, database, 'copy')).status, 'transferred');
    assert.equal((await transferDatabase(source.database, database, 'verify')).status, 'verified');
    const timestamp = (await database.query('SELECT created_at FROM shops WHERE id=1', z.object({ created_at: timestampColumn })))[0]?.created_at;
    assert.ok(timestamp); assert.match(timestamp, /\.123456/); assert.equal(new Date(timestamp).toISOString(), '2026-10-18T01:02:03.123Z');
    const app = createApp(database, source.config);
    const listing = await app.request('/api/ui/shops', { headers: headers(source.viewer) });
    assert.equal(listing.status, 200); assert.equal(shopsSchema.parse(await listing.json()).items.length, 24);
    const mutate = (rating: number): Promise<Response> => app.request('/api/ui/shops/2/annotation', { method: 'PATCH', headers: { ...headers(source.viewer), 'Content-Type': 'application/json' }, body: JSON.stringify({ version: 1, rating, memo: '一行目\n\n二行目' }) });
    assert.deepEqual((await Promise.all([mutate(3), mutate(5)])).map(response => response.status).sort(), [200, 409]);
    await database.execute("UPDATE shops SET shop_name='共通食堂',phone='03-1234-5678',address='東京都江東区木場1-2-3',is_visited=false,rating=NULL WHERE id IN (3,4)");
    assert.equal((await duplicateReport(database, true)).merged_shop_count, 1);
    const inserted = (await database.query("INSERT INTO shops(shop_name,is_visited,created_at,updated_at,version) VALUES('追加店舗',false,$1,$1,1) RETURNING id", z.object({ id: z.number() }), [new Date().toISOString()]))[0];
    assert.ok(inserted && inserted.id > 31);
  } finally {
    await source.database.close(); await database.close();
    try { await control.execute(`DROP SCHEMA IF EXISTS ${schema} CASCADE`); } finally { await control.close(); }
  }
});
