import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { z } from 'zod';
import type { Config } from './config.js';
import type { Database, Executor } from './db/database.js';
import { booleanColumn, timestampColumn } from './db/database.js';
import { AppError } from './errors.js';

export const sessionCookie = 'meshi_ts_session';
const lifetime = 24 * 60 * 60 * 1000;
const sessionRow = z.object({ token_hash: z.string(), discord_user_id: z.string().nullable(), discord_username: z.string().nullable(), grant_generation: z.string().nullable(), csrf_token: z.string(), oauth_state: z.string().nullable(), oauth_expires_at: z.coerce.number().nullable(), admin_session_hash: z.string().nullable(), csv_update_sha256: z.string().nullable(), created_at: z.coerce.number(), expires_at: z.coerce.number() });
export type StoredSession = z.infer<typeof sessionRow>;
const viewerRow = z.object({ discord_user_id: z.string(), is_admin: booleanColumn, generation: z.string() });
const adminRow = z.object({ token_hash: z.string(), auth_method: z.string(), actor_discord_user_id: z.string().nullable(), credential_binding: z.string(), expires_at: timestampColumn });
export interface Access { admin: boolean; generation: string; }
export interface Session { stored: StoredSession; access: Access | null; }
export interface IssuedSession { token: string; session: Session; }
export function hashToken(value: string): string { return createHash('sha256').update(value).digest('hex'); }
export function equalSecret(left: string, right: string): boolean {
  const a = Buffer.from(left); const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}
function randomToken(): string { return randomBytes(32).toString('base64url'); }
export class Authentication {
  constructor(readonly database: Database, readonly config: Config) {}
  async access(userId: string, connection: Executor = this.database): Promise<Access | null> {
    if (this.config.admins.has(userId)) return { admin: true, generation: 'configuration' };
    if (this.config.configuredViewers.has(userId)) return { admin: false, generation: 'configuration' };
    const rows = await connection.query('SELECT discord_user_id,is_admin,generation FROM discord_viewers WHERE discord_user_id=$1', viewerRow, [userId]);
    return rows[0] ? { admin: rows[0].is_admin, generation: rows[0].generation } : null;
  }
  private binding(userId: string): string { return createHmac('sha256', this.config.secret).update(`discord:${userId}`).digest('hex'); }
  async read(token: string | undefined): Promise<Session | null> {
    if (!token || !/^[A-Za-z0-9_-]{43}$/.test(token)) return null;
    const rows = await this.database.query('SELECT * FROM web_sessions WHERE token_hash=$1 AND expires_at>$2', sessionRow, [hashToken(token), Date.now()]);
    const stored = rows[0];
    if (!stored) return null;
    if (!stored.discord_user_id) return { stored, access: null };
    const access = await this.access(stored.discord_user_id);
    if (!access || access.generation !== stored.grant_generation) return null;
    if (stored.admin_session_hash) {
      const admins = await this.database.query('SELECT token_hash,auth_method,actor_discord_user_id,credential_binding,expires_at FROM admin_sessions WHERE token_hash=$1', adminRow, [stored.admin_session_hash]);
      const admin = admins[0];
      if (!access.admin || !admin || admin.auth_method !== 'discord' || admin.actor_discord_user_id !== stored.discord_user_id || Date.parse(admin.expires_at) <= Date.now() || !equalSecret(admin.credential_binding, this.binding(stored.discord_user_id))) return null;
    } else if (access.admin) return null;
    return { stored, access };
  }
  async anonymous(): Promise<IssuedSession> {
    return this.database.transaction(async connection => {
      const now = Date.now(); const token = randomToken();
      const stored: StoredSession = { token_hash: hashToken(token), discord_user_id: null, discord_username: null, grant_generation: null, csrf_token: randomToken(), oauth_state: null, oauth_expires_at: null, admin_session_hash: null, csv_update_sha256: null, created_at: now, expires_at: now + lifetime };
      await connection.execute('DELETE FROM web_sessions WHERE expires_at<=$1', [now]);
      await connection.execute('INSERT INTO web_sessions(token_hash,csrf_token,created_at,expires_at) VALUES($1,$2,$3,$4)', [stored.token_hash, stored.csrf_token, now, stored.expires_at]);
      return { token, session: { stored, access: null } };
    });
  }
  async beginOAuth(session: Session): Promise<string> {
    const state = randomToken();
    await this.database.execute('UPDATE web_sessions SET oauth_state=$1,oauth_expires_at=$2 WHERE token_hash=$3', [state, Date.now() + 10 * 60_000, session.stored.token_hash]);
    const url = new URL('https://discord.com/api/oauth2/authorize');
    url.search = new URLSearchParams({ client_id: this.config.clientId, redirect_uri: this.config.redirectUri, response_type: 'code', scope: 'identify', state, prompt: 'none' }).toString();
    return url.href;
  }
  async consumeOAuth(session: Session, state: string): Promise<boolean> {
    if (!session.stored.oauth_state || !equalSecret(session.stored.oauth_state, state)) return false;
    return (await this.database.execute('UPDATE web_sessions SET oauth_state=NULL,oauth_expires_at=NULL WHERE token_hash=$1 AND oauth_state=$2 AND oauth_expires_at>$3', [session.stored.token_hash, state, Date.now()])) === 1;
  }
  async issue(userId: string, username: string, previous?: Session): Promise<IssuedSession> {
    return this.database.transaction(async connection => {
      const access = await this.access(userId, connection);
      if (!access) throw new AppError(403, 'このアカウントには利用許可がありません。');
      const now = Date.now(); const token = randomToken(); const tokenHash = hashToken(token);
      const adminHash = access.admin ? hashToken(randomToken()) : null;
      if (previous) {
        await connection.execute('DELETE FROM web_sessions WHERE token_hash=$1', [previous.stored.token_hash]);
        if (previous.stored.admin_session_hash) await connection.execute('DELETE FROM admin_sessions WHERE token_hash=$1', [previous.stored.admin_session_hash]);
      }
      if (adminHash) await connection.execute('INSERT INTO admin_sessions(token_hash,auth_method,actor_discord_user_id,credential_binding,created_at,expires_at) VALUES($1,$2,$3,$4,$5,$6)', [adminHash, 'discord', userId, this.binding(userId), new Date(now).toISOString(), new Date(now + lifetime).toISOString()]);
      const stored: StoredSession = { token_hash: tokenHash, discord_user_id: userId, discord_username: username, grant_generation: access.generation, csrf_token: randomToken(), oauth_state: null, oauth_expires_at: null, admin_session_hash: adminHash, csv_update_sha256: null, created_at: now, expires_at: now + lifetime };
      await connection.execute('INSERT INTO web_sessions(token_hash,discord_user_id,discord_username,grant_generation,csrf_token,admin_session_hash,created_at,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8)', [tokenHash, userId, username, access.generation, stored.csrf_token, adminHash, now, stored.expires_at]);
      return { token, session: { stored, access } };
    });
  }
  async revoke(session: Session): Promise<void> {
    await this.database.transaction(async connection => {
      await connection.execute('DELETE FROM web_sessions WHERE token_hash=$1', [session.stored.token_hash]);
      if (session.stored.admin_session_hash) await connection.execute('DELETE FROM admin_sessions WHERE token_hash=$1', [session.stored.admin_session_hash]);
    });
  }
}

export function requireUser(session: Session | null): Session {
  if (!session?.access || !session.stored.discord_user_id) throw new AppError(401, 'ログインが必要です。');
  return session;
}
export function requireAdmin(session: Session | null): Session {
  const current = requireUser(session);
  if (!current.access?.admin || !current.stored.admin_session_hash) throw new AppError(403, '管理者権限が必要です。');
  return current;
}
export function requireWrite(session: Session | null, csrf: string | undefined, config: Config): Session {
  const current = requireUser(session);
  if (config.readOnly) throw new AppError(403, '現在は読取専用です。');
  if (!csrf || !equalSecret(csrf, current.stored.csrf_token)) throw new AppError(403, '画面を再読み込みしてから、もう一度お試しください。');
  return current;
}
