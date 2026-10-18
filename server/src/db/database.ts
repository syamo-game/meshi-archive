import { DatabaseSync } from 'node:sqlite';
import { Pool, types } from 'pg';
import type { PoolClient } from 'pg';
import { z } from 'zod';

export type SqlValue = string | number | boolean | null;
export type Dialect = 'sqlite' | 'postgres';
export interface Executor {
  readonly dialect: Dialect;
  query<Row>(sql: string, schema: z.ZodType<Row>, values?: readonly SqlValue[]): Promise<Row[]>;
  execute(sql: string, values?: readonly SqlValue[]): Promise<number>;
  script(sql: string): Promise<void>;
}
export interface Database extends Executor {
  transaction<Result>(operation: (connection: Executor) => Promise<Result>): Promise<Result>;
  withConnection<Result>(operation: (connection: Executor) => Promise<Result>): Promise<Result>;
  close(): Promise<void>;
}

class SqliteExecutor implements Executor {
  readonly dialect = 'sqlite';
  constructor(private readonly connection: DatabaseSync) {}
  private bindings(values: readonly SqlValue[]): Record<string, string | number | null> {
    const result: Record<string, string | number | null> = {};
    values.forEach((value, index) => { result[`$${index + 1}`] = typeof value === 'boolean' ? Number(value) : value; });
    return result;
  }
  async query<Row>(sql: string, schema: z.ZodType<Row>, values: readonly SqlValue[] = []): Promise<Row[]> {
    return this.connection.prepare(sql).all(this.bindings(values)).map(row => schema.parse(row));
  }
  async execute(sql: string, values: readonly SqlValue[] = []): Promise<number> {
    return Number(this.connection.prepare(sql).run(this.bindings(values)).changes);
  }
  async script(sql: string): Promise<void> { this.connection.exec(sql); }
}

class SqliteDatabase implements Database {
  readonly dialect = 'sqlite';
  private readonly connection: DatabaseSync;
  private readonly executor: SqliteExecutor;
  private pending: Promise<void> = Promise.resolve();
  constructor(path: string) {
    this.connection = new DatabaseSync(path);
    this.connection.exec('PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;');
    this.executor = new SqliteExecutor(this.connection);
  }
  private async exclusive<Result>(operation: () => Promise<Result>): Promise<Result> {
    let release: () => void = () => { throw new Error('SQLite lock was not initialized'); };
    const previous = this.pending;
    this.pending = new Promise<void>(resolve => { release = resolve; });
    await previous;
    try { return await operation(); } finally { release(); }
  }
  query<Row>(sql: string, schema: z.ZodType<Row>, values: readonly SqlValue[] = []): Promise<Row[]> {
    return this.exclusive(() => this.executor.query(sql, schema, values));
  }
  execute(sql: string, values: readonly SqlValue[] = []): Promise<number> {
    return this.exclusive(() => this.executor.execute(sql, values));
  }
  script(sql: string): Promise<void> { return this.exclusive(() => this.executor.script(sql)); }
  transaction<Result>(operation: (connection: Executor) => Promise<Result>): Promise<Result> {
    return this.exclusive(async () => {
      await this.executor.script('BEGIN IMMEDIATE');
      try {
        const result = await operation(this.executor);
        await this.executor.script('COMMIT');
        return result;
      } catch (error) {
        await this.executor.script('ROLLBACK');
        throw error;
      }
    });
  }
  withConnection<Result>(operation: (connection: Executor) => Promise<Result>): Promise<Result> { return operation(this); }
  async close(): Promise<void> { await this.exclusive(async () => { this.connection.close(); }); }
}

class PostgresExecutor implements Executor {
  readonly dialect = 'postgres';
  constructor(private readonly connection: Pool | PoolClient) {}
  async query<Row>(sql: string, schema: z.ZodType<Row>, values: readonly SqlValue[] = []): Promise<Row[]> {
    const result = await this.connection.query(sql, [...values]);
    return result.rows.map(row => schema.parse(row));
  }
  async execute(sql: string, values: readonly SqlValue[] = []): Promise<number> {
    const result = await this.connection.query(sql, [...values]);
    return result.rowCount ?? 0;
  }
  async script(sql: string): Promise<void> { await this.connection.query(sql); }
}

class PostgresDatabase implements Database {
  readonly dialect = 'postgres';
  private readonly pool: Pool;
  private readonly executor: PostgresExecutor;
  constructor(url: string) {
    this.pool = new Pool({ connectionString: url, max: 5, connectionTimeoutMillis: 10_000, idleTimeoutMillis: 30_000, types: { getTypeParser: (oid, format) => (oid === 1114 || oid === 1184) && format !== 'binary' ? (value: string): string => value : types.getTypeParser(oid, format) } });
    this.pool.on('error', error => { console.error('PostgreSQL pool failed', { message: error.message }); });
    this.executor = new PostgresExecutor(this.pool);
  }
  query<Row>(sql: string, schema: z.ZodType<Row>, values: readonly SqlValue[] = []): Promise<Row[]> { return this.executor.query(sql, schema, values); }
  execute(sql: string, values: readonly SqlValue[] = []): Promise<number> { return this.executor.execute(sql, values); }
  script(sql: string): Promise<void> { return this.executor.script(sql); }
  async transaction<Result>(operation: (connection: Executor) => Promise<Result>): Promise<Result> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      try {
        const result = await operation(new PostgresExecutor(client));
        await client.query('COMMIT');
        return result;
      } catch (error) {
        await client.query('ROLLBACK');
        throw error;
      }
    } finally { client.release(); }
  }
  async withConnection<Result>(operation: (connection: Executor) => Promise<Result>): Promise<Result> {
    const client = await this.pool.connect();
    try { return await operation(new PostgresExecutor(client)); } finally { client.release(); }
  }
  async close(): Promise<void> { await this.pool.end(); }
}

export function openDatabase(url: string): Database {
  if (/^postgres(?:ql)?:\/\//.test(url)) return new PostgresDatabase(url);
  if (url.startsWith('sqlite:///')) return new SqliteDatabase(url.slice('sqlite:///'.length));
  if (url === 'sqlite://') return new SqliteDatabase(':memory:');
  throw new Error('DATABASE_URL must use PostgreSQL or SQLite');
}

export const booleanColumn = z.union([z.boolean(), z.literal(0), z.literal(1)]).transform(value => Boolean(value));
export const timestampColumn = z.union([z.string(), z.date()]).transform(value => {
  if (value instanceof Date) return value.toISOString();
  const normalized = value.replace(' ', 'T').replace(/(T.*[+-]\d{2})$/, '$1:00').replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
  return /(?:Z|[+-]\d{2}:\d{2})$/.test(normalized) ? normalized : normalized + 'Z';
}).refine(value => Number.isFinite(Date.parse(value)), 'Invalid stored timestamp');
export const countRow = z.object({ count: z.coerce.number().int().nonnegative() });
export const idRow = z.object({ id: z.number().int().positive() });

