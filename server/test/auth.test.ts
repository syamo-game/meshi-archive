import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Authentication, hashToken, requireAdmin, requireUser, requireWrite } from '../src/auth.js';
import { discordId, readConfig } from '../src/config.js';
import { openDatabase } from '../src/db/database.js';
import { migrateDatabase } from '../src/db/migrate.js';

const adminId = '123456789012345678';
const viewerId = '223456789012345678';
function config(overrides: NodeJS.ProcessEnv = {}): ReturnType<typeof readConfig> {
  return readConfig({ SECRET_KEY: 'synthetic-test-session-secret-at-least-32', ADMIN_USER_ID: adminId, DISCORD_CLIENT_ID: 'synthetic-client', DISCORD_CLIENT_SECRET: 'synthetic-secret', DISCORD_REDIRECT_URI: 'http://localhost:8021/auth/discord/callback', ...overrides });
}

test('opaque sessions rotate and revoke real database session records', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    const auth = new Authentication(db, config());
    const anonymous = await auth.anonymous();
    assert.equal((await auth.read(anonymous.token))?.access, null);
    assert.throws(() => requireUser(anonymous.session), /ログイン/);
    const issued = await auth.issue(adminId, '管理者', anonymous.session);
    assert.notEqual(issued.token, anonymous.token);
    assert.equal(await auth.read(anonymous.token), null);
    const session = await auth.read(issued.token);
    assert.equal(requireAdmin(session).stored.discord_user_id, adminId);
    assert.throws(() => requireWrite(session, 'wrong-csrf', config()), /再読み込み/);
    assert.equal(requireWrite(session, issued.session.stored.csrf_token, config()).stored.token_hash, hashToken(issued.token));
    assert.throws(() => requireWrite(session, issued.session.stored.csrf_token, config({ APP_READ_ONLY: 'true' })), /読取専用/);
    await auth.revoke(issued.session);
    assert.equal(await auth.read(issued.token), null);
  } finally { await db.close(); }
});

test('uses the current database grant and rejects revoked or regranted cookies', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    const auth = new Authentication(db, config());
    await assert.rejects(auth.issue(viewerId, '一般'), /利用許可/);
    await db.execute('INSERT INTO discord_viewers(discord_user_id,is_admin,generation,created_at) VALUES($1,$2,$3,$4)', [viewerId, false, 'first-generation', new Date().toISOString()]);
    const issued = await auth.issue(viewerId, '一般');
    const session = await auth.read(issued.token);
    assert.equal(requireUser(session).access?.admin, false);
    assert.throws(() => requireAdmin(session), /管理者権限/);
    await db.execute('DELETE FROM discord_viewers WHERE discord_user_id=$1', [viewerId]);
    assert.equal(await auth.read(issued.token), null);
    await db.execute('INSERT INTO discord_viewers(discord_user_id,is_admin,generation,created_at) VALUES($1,$2,$3,$4)', [viewerId, false, 'new-generation', new Date().toISOString()]);
    assert.equal(await auth.read(issued.token), null);
  } finally { await db.close(); }
});

test('permission changes cannot revive an old administrator session', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    const auth = new Authentication(db, config());
    await db.execute('INSERT INTO discord_viewers(discord_user_id,is_admin,generation,created_at) VALUES($1,$2,$3,$4)', [viewerId, true, 'first-generation', new Date().toISOString()]);
    const issued = await auth.issue(viewerId, '管理者');
    assert.equal((await auth.read(issued.token))?.access?.admin, true);
    await db.execute('UPDATE discord_viewers SET is_admin=$1,generation=$2 WHERE discord_user_id=$3', [false, 'next-generation', viewerId]);
    assert.equal(await auth.read(issued.token), null);
    await db.execute('UPDATE discord_viewers SET is_admin=$1,generation=$2 WHERE discord_user_id=$3', [true, 'third-generation', viewerId]);
    assert.equal(await auth.read(issued.token), null);
    const fixedViewer = new Authentication(db, config({ DISCORD_ALLOWED_USER_IDS: viewerId }));
    assert.equal((await fixedViewer.access(viewerId))?.admin, false);
  } finally { await db.close(); }
});

test('OAuth states are single-use, bound to the session, and expire', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    const auth = new Authentication(db, config());
    const first = await auth.anonymous(); const second = await auth.anonymous();
    const url = new URL(await auth.beginOAuth(first.session));
    const state = url.searchParams.get('state');
    assert.ok(state);
    const current = await auth.read(first.token); assert.ok(current);
    assert.equal(await auth.consumeOAuth(second.session, state), false);
    assert.equal(await auth.consumeOAuth(current, 'mismatch'), false);
    assert.equal(await auth.consumeOAuth(current, state), true);
    assert.equal(await auth.consumeOAuth(current, state), false);
    const next = new URL(await auth.beginOAuth(current));
    const stale = await auth.read(first.token); assert.ok(stale);
    await db.execute('UPDATE web_sessions SET oauth_expires_at=$1 WHERE token_hash=$2', [Date.now() - 1, stale.stored.token_hash]);
    assert.equal(await auth.consumeOAuth(stale, next.searchParams.get('state') ?? ''), false);
  } finally { await db.close(); }
});

test('sessions expire without extending their absolute lifetime on read', async () => {
  const db = openDatabase('sqlite://');
  try {
    await migrateDatabase(db);
    const auth = new Authentication(db, config());
    const issued = await auth.issue(adminId, '管理者');
    const expires = (await auth.read(issued.token))?.stored.expires_at;
    assert.equal((await auth.read(issued.token))?.stored.expires_at, expires);
    await db.execute('UPDATE web_sessions SET expires_at=$1 WHERE token_hash=$2', [Date.now() - 1, hashToken(issued.token)]);
    assert.equal(await auth.read(issued.token), null);
  } finally { await db.close(); }
});

test('validates snowflakes and production authentication settings', () => {
  for (const id of ['01234567890123456', '18446744073709551616', '1e18', ' 123456789012345678']) assert.equal(discordId.safeParse(id).success, false);
  assert.equal(discordId.parse('18446744073709551615'), '18446744073709551615');
  assert.throws(() => config({ APP_ENV: 'production' }), /callback URL/);
  assert.throws(() => config({ APP_ENV: 'production', DISCORD_REDIRECT_URI: 'https://example.com/auth/discord/callback', HTTPS_ONLY: 'true', FORWARDED_ALLOW_IPS: '0.0.0.0/0' }), /explicit trusted/);
  assert.throws(() => config({ DISCORD_REDIRECT_URI: 'https://example.com/auth/discord/callback?extra=1' }), /callback URL/);
  assert.equal(config({ APP_ENV: 'production', DISCORD_REDIRECT_URI: 'https://example.com/auth/discord/callback', HTTPS_ONLY: 'true', FORWARDED_ALLOW_IPS: '127.0.0.1,172.16.0.0/24' }).secureCookies, true);
});
