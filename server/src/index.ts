import 'dotenv/config';
import { serve } from '@hono/node-server';
import { createApp } from './app.js';
import { readConfig } from './config.js';
import { openDatabase } from './db/database.js';
import { verifyDatabase } from './db/migrate.js';

const config = readConfig();
const database = openDatabase(config.databaseUrl);
try { await verifyDatabase(database); }
catch (error) { console.error('Server startup refused', error); await database.close(); throw error; }
const app = createApp(database, config);
const server = serve({ fetch: app.fetch, port: config.port, hostname: config.host });
console.info('Web server started', { host: config.host, port: config.port });
let closing = false;
async function shutdown(): Promise<void> {
  if (closing) return;
  closing = true;
  try {
    await new Promise<void>((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); });
    await database.close();
  } catch (error) { console.error('Server shutdown failed', error); process.exitCode = 1; }
}
process.once('SIGTERM', () => { void shutdown(); });
process.once('SIGINT', () => { void shutdown(); });
