import { lookup } from 'node:dns/promises';
import { readFileSync } from 'node:fs';
import { readdir, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { z } from 'zod';
import { openDatabase } from '../db/database.js';

const command = z.enum(['volume', 'image-bytes', 'database-identity', 'database-counts']).parse(process.argv[2]);
try {
  if (command === 'volume') {
    const schema = z.object({ services: z.object({ web: z.object({ environment: z.object({ SHOP_IMAGE_CACHE_DIR: z.literal('/app-data/shop-images') }), volumes: z.array(z.object({ type: z.string(), source: z.string(), target: z.string() })) }) }), volumes: z.record(z.string(), z.object({ name: z.string() })) });
    const config = schema.parse(JSON.parse(readFileSync(0, 'utf8')));
    const mounts = config.services.web.volumes.filter(mount => mount.type === 'volume' && mount.target === '/app-data/shop-images');
    const mount = mounts[0]; if (mounts.length !== 1 || !mount) throw new Error('Web must use exactly one shop image volume');
    const volume = config.volumes[mount.source]; if (!volume) throw new Error('Shop image volume has no configured name'); console.info(volume.name);
  } else if (command === 'image-bytes') {
    const directory = z.string().min(1).parse(process.argv[3]);
    async function size(path: string): Promise<number> {
      let total = 0;
      for (const entry of await readdir(path, { withFileTypes: true })) { const full = join(path, entry.name); if (entry.isSymbolicLink()) throw new Error(`Image cache contains a symbolic link: ${entry.name}`); if (entry.isDirectory()) total += await size(full); else if (entry.isFile()) total += (await stat(full)).size; }
      return total;
    }
    console.info(await size(directory));
  } else {
    const databaseUrl = z.string().min(1).parse(process.env.DATABASE_URL); const database = openDatabase(databaseUrl);
    try {
      if (database.dialect !== 'postgres') throw new Error('Production deployment requires PostgreSQL');
      if (command === 'database-identity') {
        const host = new URL(databaseUrl).hostname; const resolved = await lookup(host, { family: 4 });
        const row = (await database.query('SELECT current_database() AS database,host(inet_server_addr()) AS address', z.object({ database: z.string(), address: z.string() })))[0];
        if (!row) throw new Error('Database identity query returned no row'); console.info([row.database, host, resolved.address, row.address].join('|'));
      } else {
        const counts: Record<string, string | number> = {};
        for (const table of ['shops', 'messages', 'shop_mentions', 'source_assets']) counts[table] = (await database.query(`SELECT count(*) AS count FROM ${table}`, z.object({ count: z.coerce.number().int().nonnegative() })))[0]?.count ?? 0;
        const row = (await database.query('SELECT current_database() AS database,version_num FROM alembic_version', z.object({ database: z.string(), version_num: z.string() })))[0];
        if (!row) throw new Error('Legacy revision query returned no row'); counts.database = row.database; counts.alembic_version = row.version_num; console.info(JSON.stringify(counts));
      }
    } finally { await database.close(); }
  }
} catch (error) { console.error('Deployment verification failed', error); process.exitCode = 1; }
