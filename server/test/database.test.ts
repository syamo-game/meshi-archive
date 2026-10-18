import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { booleanColumn, openDatabase } from '../src/db/database.js';
import { legacyRevision, migrateDatabase, verifyDatabase } from '../src/db/migrate.js';

test('initializes a real SQLite schema and preserves it on repeat migration', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    await db.execute('INSERT INTO discord_viewers(discord_user_id,is_admin,generation,created_at) VALUES($1,$2,$3,$4)', ['123456789012345678', true, 'fixed-generation', '2026-10-18T00:00:00Z']);
    await migrateDatabase(db);
    const rows = await db.query('SELECT * FROM discord_viewers', z.object({ discord_user_id: z.string(), is_admin: booleanColumn, generation: z.string() }));
    assert.deepEqual(rows, [{ discord_user_id: '123456789012345678', is_admin: true, generation: 'fixed-generation' }]);
    const revision = await db.query('SELECT version_num FROM alembic_version', z.object({ version_num: z.string() }));
    assert.equal(revision[0]?.version_num, legacyRevision);
  } finally { await db.close(); }
});

test('rolls back all writes when an operation fails', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    await assert.rejects(db.transaction(async connection => {
      await connection.execute('INSERT INTO discord_viewers(discord_user_id,is_admin,generation,created_at) VALUES($1,$2,$3,$4)', ['123456789012345678', false, 'generation', '2026-10-18']);
      throw new Error('Abort the actual transaction');
    }), /Abort the actual transaction/);
    assert.equal((await db.query('SELECT discord_user_id FROM discord_viewers', z.object({ discord_user_id: z.string() }))).length, 0);
  } finally { await db.close(); }
});

test('does not interleave SQLite transactions on the same connection', async () => {
  const db = openDatabase('sqlite://');
  try {
    await db.script('CREATE TABLE counter(value INTEGER NOT NULL); INSERT INTO counter VALUES(0)');
    await Promise.all(Array.from({ length: 10 }, () => db.transaction(async connection => {
      const rows = await connection.query('SELECT value FROM counter', z.object({ value: z.number() }));
      await new Promise<void>(resolve => setTimeout(resolve, 1));
      await connection.execute('UPDATE counter SET value=$1', [(rows[0]?.value ?? 0) + 1]);
    })));
    assert.equal((await db.query('SELECT value FROM counter', z.object({ value: z.number() })))[0]?.value, 10);
  } finally { await db.close(); }
});

test('refuses an unrelated database instead of creating application tables', async () => {
  const db = openDatabase('sqlite://');
  try {
    await db.script('CREATE TABLE unrelated(value TEXT)');
    await assert.rejects(migrateDatabase(db), /no Alembic revision/);
    const tables = await db.query("SELECT name FROM sqlite_master WHERE type='table'", z.object({ name: z.string() }));
    assert.deepEqual(tables, [{ name: 'unrelated' }]);
  } finally { await db.close(); }
});

test('refuses unsupported or incomplete schemas without stamping a migration', async () => {
  const db = openDatabase('sqlite://');
  try {
    await db.script("CREATE TABLE alembic_version(version_num TEXT); INSERT INTO alembic_version VALUES('future_revision')");
    await assert.rejects(migrateDatabase(db), /Unsupported legacy database revision/);
    await db.execute('UPDATE alembic_version SET version_num=$1', [legacyRevision]);
    await assert.rejects(migrateDatabase(db), /missing table/);
    await assert.rejects(verifyDatabase(db), /migrations are required/);
  } finally { await db.close(); }
});

test('refuses startup when the TypeScript session table is missing', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    await db.script('DROP TABLE web_sessions');
    await assert.rejects(verifyDatabase(db), /session schema is incomplete/);
  } finally { await db.close(); }
});
