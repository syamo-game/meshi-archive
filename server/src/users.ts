import { randomBytes } from 'node:crypto';
import type { Hono } from 'hono';
import { z } from 'zod';
import type { Feedback, User } from '@meshi/contracts';
import type { WebEnvironment } from './app.js';
import { Authentication, requireAdmin, requireWrite } from './auth.js';
import type { Session } from './auth.js';
import { discordId } from './config.js';
import type { Config } from './config.js';
import type { Database, Executor } from './db/database.js';
import { booleanColumn, timestampColumn } from './db/database.js';
import { AppError } from './errors.js';

const userRow = z.object({ discord_user_id: z.string(), is_admin: booleanColumn, generation: z.string() });
const actorRow = z.object({ actor_discord_user_id: z.string(), auth_method: z.literal('discord'), expires_at: timestampColumn });
const roleSchema = z.enum(['viewer', 'admin']);
type Role = z.infer<typeof roleSchema>;
export function feedback(message: string): Feedback { return { ok: true, errors: [], message, preview: null, changes: [] }; }
export function formValue(form: FormData, field: string): string {
  const value = form.get(field);
  if (value === null) return '';
  if (typeof value !== 'string') throw new AppError(422, `${field}の形式が不正です。`);
  return value;
}
export class Users {
  private readonly auth: Authentication;
  private readonly configured: ReadonlySet<string>;
  constructor(private readonly database: Database, private readonly config: Config) {
    this.auth = new Authentication(database, config);
    this.configured = new Set([...config.admins, ...config.configuredViewers]);
  }
  private async actor(connection: Executor, session: Session): Promise<string> {
    requireAdmin(session);
    const userId = session.stored.discord_user_id;
    if (!userId || !(await this.auth.access(userId, connection))?.admin) throw new AppError(403, '管理者権限が更新されています。ログインし直してください。');
    const rows = await connection.query('SELECT actor_discord_user_id,auth_method,expires_at FROM admin_sessions WHERE token_hash=$1', actorRow, [session.stored.admin_session_hash]);
    if (rows[0]?.actor_discord_user_id !== userId || Date.parse(rows[0].expires_at) <= Date.now()) throw new AppError(401, 'ログインの有効期限が切れました。');
    return userId;
  }
  private async audit(connection: Executor, session: Session, userId: string, action: 'grant' | 'revoke' | 'replace' | 'update', previous: Role | null, next: Role | null, replacement: string | null): Promise<void> {
    await connection.execute('INSERT INTO discord_viewer_grant_events(discord_user_id,actor_method,actor_discord_user_id,actor_session_hash,action,replacement_user_id,previous_role,new_role,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)', [userId, 'discord', session.stored.discord_user_id, session.stored.admin_session_hash, action, replacement, previous, next, new Date().toISOString()]);
  }
  async list(): Promise<User[]> {
    const rows = await this.database.query('SELECT discord_user_id,is_admin,generation FROM discord_viewers ORDER BY created_at,discord_user_id', userRow);
    const configured: User[] = [...this.configured].sort().map(id => ({ user_id: id, is_admin: this.config.admins.has(id), source: 'configuration', generation: null }));
    const stored: User[] = rows.filter(row => !this.configured.has(row.discord_user_id)).map(row => ({ user_id: row.discord_user_id, is_admin: row.is_admin, source: 'database', generation: row.generation }));
    return [...configured, ...stored];
  }
  async add(userId: string, role: Role, session: Session): Promise<Feedback> {
    discordId.parse(userId);
    return this.database.transaction(async connection => {
      await this.actor(connection, session);
      if (await this.auth.access(userId, connection)) return feedback('登録済みのユーザーです。');
      const changed = await connection.execute('INSERT INTO discord_viewers(discord_user_id,is_admin,generation,created_at) VALUES($1,$2,$3,$4) ON CONFLICT(discord_user_id) DO NOTHING', [userId, role === 'admin', randomBytes(16).toString('hex'), new Date().toISOString()]);
      if (!changed) return feedback('登録済みのユーザーです。');
      await this.audit(connection, session, userId, 'grant', null, role, null);
      return feedback('追加しました。');
    });
  }
  async change(userId: string, generation: string, replacement: string, role: Role, revoke: boolean, session: Session): Promise<Feedback> {
    discordId.parse(userId); if (!revoke) discordId.parse(replacement);
    if (this.configured.has(userId)) throw new AppError(409, '初期設定で固定されたユーザーは、この画面では変更できません。');
    return this.database.transaction(async connection => {
      await this.actor(connection, session);
      const row = (await connection.query('SELECT discord_user_id,is_admin,generation FROM discord_viewers WHERE discord_user_id=$1', userRow, [userId]))[0];
      if (!row || row.generation !== generation) throw new AppError(409, '利用許可が変更されています。一覧を再読み込みしてください。');
      const previous: Role = row.is_admin ? 'admin' : 'viewer';
      if (!revoke && userId === replacement && previous === role) return feedback('保存しました。');
      if (!revoke && userId !== replacement && await this.auth.access(replacement, connection)) throw new AppError(409, '変更先のユーザーはすでに登録されています。');
      const changed = revoke
        ? await connection.execute('DELETE FROM discord_viewers WHERE discord_user_id=$1 AND generation=$2', [userId, generation])
        : await connection.execute('UPDATE discord_viewers SET discord_user_id=$3,is_admin=$4,generation=$5 WHERE discord_user_id=$1 AND generation=$2', [userId, generation, replacement, role === 'admin', randomBytes(16).toString('hex')]);
      if (changed !== 1) throw new AppError(409, '利用許可が変更されています。一覧を再読み込みしてください。');
      await connection.execute('DELETE FROM admin_sessions WHERE actor_discord_user_id=$1', [userId]);
      await connection.execute('DELETE FROM web_sessions WHERE discord_user_id=$1', [userId]);
      await this.audit(connection, session, userId, revoke ? 'revoke' : userId !== replacement ? 'replace' : 'update', previous, revoke ? null : role, !revoke && replacement !== userId ? replacement : null);
      return feedback(revoke ? '削除しました。' : '保存しました。');
    });
  }
}
export function registerUserRoutes(app: Hono<WebEnvironment>, database: Database, config: Config): void {
  const users = new Users(database, config);
  app.get('/api/ui/admin/users', async context => { requireAdmin(context.get('session')); return context.json({ entries: await users.list() }); });
  app.post('/api/ui/admin/users', async context => {
    const form = await context.req.formData();
    const session = requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token') ?? formValue(form, 'csrf_token'), config));
    return context.json(await users.add(discordId.parse(formValue(form, 'discord_user_id')), roleSchema.parse(formValue(form, 'role')), session));
  });
  app.post('/api/ui/admin/users/:id', async context => {
    const form = await context.req.formData();
    const session = requireAdmin(requireWrite(context.get('session'), context.req.header('X-CSRF-Token') ?? formValue(form, 'csrf_token'), config));
    const action = z.enum(['save', 'revoke']).parse(formValue(form, 'action'));
    return context.json(await users.change(context.req.param('id'), formValue(form, 'expected_generation'), formValue(form, 'replacement_user_id'), roleSchema.parse(formValue(form, 'role')), action === 'revoke', session));
  });
}
