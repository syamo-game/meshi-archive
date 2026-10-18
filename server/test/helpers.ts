import { z } from 'zod';
import { Authentication, sessionCookie } from '../src/auth.js';
import type { IssuedSession } from '../src/auth.js';
import { readConfig } from '../src/config.js';
import { openDatabase } from '../src/db/database.js';
import { migrateDatabase } from '../src/db/migrate.js';
import { createApp } from '../src/app.js';

export async function fixture(): Promise<{ database: ReturnType<typeof openDatabase>; app: ReturnType<typeof createApp>; config: ReturnType<typeof readConfig>; admin: IssuedSession; viewer: IssuedSession }> {
  const database = openDatabase('sqlite://');
  const config = readConfig({ SECRET_KEY: 'synthetic-test-session-secret-at-least-32', ADMIN_USER_ID: '123456789012345678', DISCORD_CLIENT_ID: 'synthetic-client', DISCORD_CLIENT_SECRET: 'synthetic-secret', DISCORD_REDIRECT_URI: 'http://localhost:8021/auth/discord/callback' });
  await migrateDatabase(database);
  await database.transaction(async connection => {
    for (let id = 1; id <= 31; id++) {
      const messageId = '9' + String(id).padStart(16, '0');
      const now = new Date().toISOString();
      await connection.execute('INSERT INTO messages(message_id,is_target,processing_status) VALUES($1,$2,$3)', [messageId, true, 'succeeded']);
      await connection.execute('INSERT INTO shops(id,shop_name,area,category,is_visited,rating,created_at,updated_at,version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)', [id, '店舗' + id.toString().padStart(2, '0'), id <= 20 ? '木場' : '門前仲町', '和食', id === 2, id === 1 ? 4 : null, now, now, 1]);
      await connection.execute('INSERT INTO shop_mentions(message_id,shop_id,occurrence_index,extracted_name,resolution_status,review_status,metadata_review_status,extraction_source,version,created_at,source_url) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)', [messageId, id, 0, '店舗' + id, 'resolved', id === 31 ? 'pending' : 'approved', 'approved', 'synthetic-test', 1, now, `https://x.com/example/status/${messageId}`]);
    }
    await connection.execute('INSERT INTO discord_viewers(discord_user_id,is_admin,generation,created_at) VALUES($1,$2,$3,$4)', ['223456789012345678', false, 'synthetic-generation', new Date().toISOString()]);
  });
  const auth = new Authentication(database, config);
  const admin = await auth.issue('123456789012345678', '管理者');
  const viewer = await auth.issue('223456789012345678', '一般ユーザー');
  return { database, app: createApp(database, config), config, admin, viewer };
}
export function headers(session: IssuedSession): { Cookie: string; 'X-CSRF-Token': string } {
  return { Cookie: `${sessionCookie}=${session.token}`, 'X-CSRF-Token': session.session.stored.csrf_token };
}
export const versionRow = z.object({ version: z.number() });
