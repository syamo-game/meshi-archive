import { createHash } from 'node:crypto';
import { z } from 'zod';
import manifest from '../../migrations/transfer-schema.json' with { type: 'json' };
import type { Database, Executor, SqlValue } from '../db/database.js';
import { booleanColumn, countRow, timestampColumn } from '../db/database.js';

const columnSchema = z.object({ name: z.string().regex(/^[a-z_][a-z0-9_]*$/), kind: z.enum(['boolean', 'timestamp', 'integer', 'text']), nullable: z.boolean() });
const tableSchema = z.object({ name: z.string().regex(/^[a-z_][a-z0-9_]*$/), primary: z.array(z.string().regex(/^[a-z_][a-z0-9_]*$/)).min(1), columns: z.array(columnSchema) });
type Table = z.infer<typeof tableSchema>;
const tables: readonly Table[] = z.array(tableSchema).parse(manifest);
const scalarRow = z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()]));
export interface TableSummary { table: string; rows: number; sha256: string; }
export interface TransferReport { status: 'verified' | 'transferred'; tables: readonly TableSummary[]; total_rows: number; }
function timestamp(value: SqlValue): string {
  const normalized = timestampColumn.parse(value); const fraction = /\.(\d+)/.exec(normalized)?.[1] ?? '';
  return new Date(normalized).toISOString().replace(/\.\d{3}Z$/, '.' + fraction.padEnd(6, '0').slice(0, 6) + 'Z');
}
function validateValue(column: z.infer<typeof columnSchema>, value: SqlValue | undefined): SqlValue {
  if (value === undefined || (value === null && !column.nullable)) throw new Error(`Missing required transfer column: column=${column.name}`);
  if (value === null) return null;
  if (column.kind === 'boolean') return booleanColumn.parse(value);
  if (column.kind === 'integer') return z.coerce.number().int().safe().parse(value);
  if (column.kind === 'timestamp') return timestamp(value);
  return z.string().parse(value);
}
async function rows(connection: Executor, table: Table, offset: number): Promise<Record<string, SqlValue>[]> {
  return connection.query(`SELECT ${table.columns.map(column => column.name).join(',')} FROM ${table.name} ORDER BY ${table.primary.join(',')} LIMIT $1 OFFSET $2`, scalarRow, [500, offset]);
}
async function summarize(connection: Executor, table: Table): Promise<TableSummary> {
  const digest = createHash('sha256'); let count = 0;
  for (;;) {
    const batch = await rows(connection, table, count); if (!batch.length) break;
    for (const row of batch) { digest.update(JSON.stringify(table.columns.map(column => validateValue(column, row[column.name]))) + '\n'); count++; }
  }
  return { table: table.name, rows: count, sha256: digest.digest('hex') };
}
async function transferTables(source: Executor): Promise<readonly Table[]> {
  const present = source.dialect === 'sqlite' ? await source.query("SELECT name FROM sqlite_master WHERE type='table'", z.object({ name: z.string() })) : await source.query('SELECT table_name AS name FROM information_schema.tables WHERE table_schema=current_schema()', z.object({ name: z.string() }));
  const names = new Set(present.map(row => row.name));
  for (const table of tables) if (!names.has(table.name)) throw new Error(`Source database is missing table=${table.name}`);
  if (!names.has('web_sessions')) return tables;
  const namesOfColumns = ['token_hash', 'discord_user_id', 'discord_username', 'grant_generation', 'csrf_token', 'oauth_state', 'oauth_expires_at', 'admin_session_hash', 'csv_update_sha256', 'created_at', 'expires_at'];
  const nullable = new Set(['discord_user_id', 'discord_username', 'grant_generation', 'oauth_state', 'oauth_expires_at', 'admin_session_hash', 'csv_update_sha256']);
  return [...tables, { name: 'web_sessions', primary: ['token_hash'], columns: namesOfColumns.map(name => ({ name, kind: name.endsWith('_at') ? 'integer' as const : 'text' as const, nullable: nullable.has(name) })) }];
}
function verifySummaries(source: readonly TableSummary[], target: readonly TableSummary[]): void {
  const differences = source.filter((row, index) => row.table !== target[index]?.table || row.rows !== target[index]?.rows || row.sha256 !== target[index]?.sha256).map(row => row.table);
  if (differences.length) throw new Error('Database verification failed for tables: ' + differences.join(', '));
}
export async function transferDatabase(source: Database, target: Database, mode: 'copy' | 'verify'): Promise<TransferReport> {
  if (source === target) throw new Error('Source and target must be different databases');
  return source.transaction(async from => {
    if (from.dialect === 'postgres') await from.script('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY');
    const selected = await transferTables(from); const sourceSummaries: TableSummary[] = [];
    for (const table of selected) sourceSummaries.push(await summarize(from, table));
    return target.transaction(async to => {
      if (mode === 'copy') {
        if (to.dialect === 'postgres') await to.script(`LOCK TABLE ${selected.map(table => table.name).join(',')} IN ACCESS EXCLUSIVE MODE`);
        for (const table of selected) if ((await to.query(`SELECT COUNT(*) AS count FROM ${table.name}`, countRow))[0]?.count) throw new Error(`Target database is not empty: table=${table.name}`);
        const delayedReferences: { id: number; source: number }[] = [];
        for (const table of selected) {
          let offset = 0;
          for (;;) {
            const batch = await rows(from, table, offset); if (!batch.length) break;
            for (const row of batch) {
              const values = table.columns.map(column => validateValue(column, row[column.name]));
              if (table.name === 'shop_mentions' && row.reused_from_mention_id !== null) { delayedReferences.push({ id: z.number().parse(row.id), source: z.number().parse(row.reused_from_mention_id) }); values[table.columns.findIndex(column => column.name === 'reused_from_mention_id')] = null; }
              await to.execute(`INSERT INTO ${table.name}(${table.columns.map(column => column.name).join(',')}) VALUES(${values.map((_, index) => '$' + (index + 1)).join(',')})`, values);
            }
            offset += batch.length;
          }
          if (to.dialect === 'postgres') for (const key of table.primary) {
            if (table.columns.find(column => column.name === key)?.kind !== 'integer') continue;
            const sequence = (await to.query('SELECT pg_get_serial_sequence($1,$2) AS name', z.object({ name: z.string().nullable() }), [table.name, key]))[0]?.name;
            if (!sequence) continue;
            const maximum = (await to.query(`SELECT MAX(${key}) AS maximum FROM ${table.name}`, z.object({ maximum: z.coerce.number().nullable() })))[0]?.maximum ?? null;
            await to.execute('SELECT setval($1::regclass,$2,$3)', [sequence, maximum ?? 1, maximum !== null]);
          }
        }
        for (const reference of delayedReferences) await to.execute('UPDATE shop_mentions SET reused_from_mention_id=$1 WHERE id=$2', [reference.source, reference.id]);
      }
      const targetSummaries: TableSummary[] = []; for (const table of selected) targetSummaries.push(await summarize(to, table));
      verifySummaries(sourceSummaries, targetSummaries);
      return { status: mode === 'copy' ? 'transferred' : 'verified', tables: sourceSummaries, total_rows: sourceSummaries.reduce((sum, table) => sum + table.rows, 0) };
    });
  });
}
export function validateTransferUrls(source: string, target: string, confirmation: string): void {
  if (target !== confirmation) throw new Error('CONFIRM_TARGET_DATABASE_URL must exactly match TARGET_DATABASE_URL');
  if (source === target) throw new Error('Source and target database URLs must be different');
  if (!source.startsWith('sqlite:///')) throw new Error('SOURCE_DATABASE_URL must use SQLite');
  if (!/^postgres(?:ql)?:\/\//.test(target)) throw new Error('TARGET_DATABASE_URL must use PostgreSQL');
}

