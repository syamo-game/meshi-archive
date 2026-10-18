import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { z } from 'zod';
import type { Database, Executor } from './database.js';
import { openDatabase } from './database.js';

export const legacyRevision = '20261007_01';
export const typescriptRevision = '20261018_01_typescript_sessions';
const tableRow = z.object({ name: z.string() });
const revisionRow = z.object({ version_num: z.string() });
const schemaManifest = z.record(z.string(), z.array(z.string()));

async function tables(connection: Executor): Promise<Set<string>> {
  const sql = connection.dialect === 'sqlite'
    ? "SELECT name FROM sqlite_master WHERE type = 'table'"
    : "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = current_schema()";
  return new Set((await connection.query(sql, tableRow)).map(row => row.name));
}

async function verifyLegacySchema(connection: Executor): Promise<void> {
  const manifest = schemaManifest.parse(JSON.parse(await readFile(new URL('../../migrations/baseline-columns.json', import.meta.url), 'utf8')));
  const existing = await tables(connection);
  for (const [table, expected] of Object.entries(manifest)) {
    if (!existing.has(table)) throw new Error(`Database schema is incomplete: missing table=${table}`);
    if (!/^[a-z_]+$/.test(table)) throw new Error(`Invalid schema manifest table=${table}`);
    const rows = connection.dialect === 'sqlite'
      ? await connection.query(`PRAGMA table_info(${table})`, tableRow)
      : await connection.query('SELECT column_name AS name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = $1', tableRow, [table]);
    const columns = new Set(rows.map(row => row.name));
    const missing = expected.filter(column => !columns.has(column));
    if (missing.length) throw new Error(`Database schema is incomplete: table=${table} missing_columns=${missing.join(',')}`);
  }
}

async function verifySessionSchema(connection: Executor): Promise<void> {
  const required = ['token_hash', 'discord_user_id', 'discord_username', 'grant_generation', 'csrf_token', 'oauth_state', 'oauth_expires_at', 'admin_session_hash', 'csv_update_sha256', 'created_at', 'expires_at'];
  const rows = connection.dialect === 'sqlite'
    ? await connection.query('PRAGMA table_info(web_sessions)', tableRow)
    : await connection.query("SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='web_sessions'", tableRow);
  const columns = new Set(rows.map(row => row.name));
  const missing = required.filter(column => !columns.has(column));
  if (missing.length) throw new Error(`TypeScript session schema is incomplete: missing_columns=${missing.join(',')}`);
}

export async function verifyDatabase(database: Database): Promise<void> {
  const existing = await tables(database);
  if (!existing.has('alembic_version') || !existing.has('typescript_migrations')) throw new Error('Database migrations are required before startup');
  const legacy = await database.query('SELECT version_num FROM alembic_version', revisionRow);
  const current = await database.query('SELECT version_num FROM typescript_migrations', revisionRow);
  if (legacy.length !== 1 || legacy[0]?.version_num !== legacyRevision || current.length !== 1 || current[0]?.version_num !== typescriptRevision) {
    throw new Error('Database revision does not match the TypeScript application');
  }
  await verifyLegacySchema(database);
  await verifySessionSchema(database);
}

export async function migrateDatabase(database: Database): Promise<void> {
  await database.transaction(async connection => {
    if (connection.dialect === 'postgres') await connection.script('SELECT pg_advisory_xact_lock(747125109)');
    const existing = await tables(connection);
    const applicationTables = [...existing].filter(name => !name.startsWith('sqlite_'));
    if (applicationTables.length === 0) {
      await connection.script(await readFile(new URL(`../../migrations/baseline.${connection.dialect}.sql`, import.meta.url), 'utf8'));
      await connection.script('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)');
      await connection.execute('INSERT INTO alembic_version (version_num) VALUES ($1)', [legacyRevision]);
    } else {
      if (!existing.has('alembic_version')) throw new Error('Refusing to adopt a nonempty database with no Alembic revision');
      const revisions = await connection.query('SELECT version_num FROM alembic_version', revisionRow);
      if (revisions.length !== 1 || revisions[0]?.version_num !== legacyRevision) throw new Error(`Unsupported legacy database revision; expected=${legacyRevision}`);
    }
    await verifyLegacySchema(connection);
    if (existing.has('typescript_migrations')) {
      const versions = await connection.query('SELECT version_num FROM typescript_migrations', revisionRow);
      if (versions.length !== 1 || versions[0]?.version_num !== typescriptRevision) throw new Error('Unsupported TypeScript database revision');
      return;
    }
    await connection.script(`
      CREATE TABLE web_sessions (
        token_hash VARCHAR(64) PRIMARY KEY,
        discord_user_id VARCHAR(20),
        discord_username TEXT,
        grant_generation VARCHAR(32),
        csrf_token VARCHAR(64) NOT NULL,
        oauth_state VARCHAR(64),
        oauth_expires_at BIGINT,
        admin_session_hash VARCHAR(64),
        csv_update_sha256 VARCHAR(64),
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL
      );
      CREATE INDEX ix_web_sessions_user ON web_sessions(discord_user_id);
      CREATE INDEX ix_web_sessions_expiry ON web_sessions(expires_at);
      CREATE TABLE typescript_migrations (version_num VARCHAR(64) PRIMARY KEY);
    `);
    await connection.execute('INSERT INTO typescript_migrations (version_num) VALUES ($1)', [typescriptRevision]);
  });
  await verifyDatabase(database);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await import('dotenv/config');
  const database = openDatabase(process.env['DATABASE_URL'] ?? 'sqlite:///meshi.db');
  try {
    await migrateDatabase(database);
    console.info('Database migration completed', { revision: typescriptRevision });
  } catch (error) {
    console.error('Database migration failed', error);
    process.exitCode = 1;
  } finally { await database.close(); }
}
