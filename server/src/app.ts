import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Hono } from 'hono';
import type { HttpBindings } from '@hono/node-server';
import { serveStatic } from '@hono/node-server/serve-static';
import { bodyLimit } from 'hono/body-limit';
import { deleteCookie, getCookie, setCookie } from 'hono/cookie';
import { z } from 'zod';
import { annotationUpdateSchema, sessionSchema } from '@meshi/contracts';
import { Authentication, equalSecret, requireAdmin, requireUser, requireWrite, sessionCookie } from './auth.js';
import type { Session } from './auth.js';
import { discordId } from './config.js';
import type { Config } from './config.js';
import type { Database } from './db/database.js';
import { AppError } from './errors.js';
import { areaLabel, areas } from './geography.js';
import { listQuerySchema, Shops } from './shops.js';
import { registerUserRoutes } from './users.js';
import { registerEditorRoutes } from './shop-editor.js';
import { registerCsvRoutes } from './csv.js';
import { registerReviewRoutes } from './reviews.js';
import { registerDecisionRoutes } from './review-decisions.js';
import { registerLinkRoutes } from './mention-linking.js';
import { registerSuggestionRoutes } from './suggestions.js';

export type WebEnvironment = { Bindings: HttpBindings; Variables: { session: Session | null } };
const tokenResponse = z.object({ access_token: z.string().min(1) });
const discordUser = z.object({ id: discordId, username: z.string().min(1) });
const positiveId = z.coerce.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const loginErrors: Readonly<Record<string, string>> = { discord_forbidden: 'このアカウントには利用許可がありません。', discord_state_mismatch: '認証の有効期限が切れました。もう一度ログインしてください。', discord_token_failed: 'Discord認証に失敗しました。', discord_user_failed: 'Discordのユーザー情報を取得できませんでした。', discord_access_denied: 'Discord認証がキャンセルされました。', discord_network: 'Discordと通信できませんでした。時間をおいて再度お試しください。', discord_missing_params: 'Discord認証パラメーターが不足しています。' };

export function createApp(database: Database, config: Config): Hono<WebEnvironment> {
  const app = new Hono<WebEnvironment>();
  const auth = new Authentication(database, config);
  const shops = new Shops(database, config);
  app.use('*', async (context, next) => {
    context.header('X-Content-Type-Options', 'nosniff');
    context.header('X-Frame-Options', 'DENY');
    context.header('Referrer-Policy', 'strict-origin-when-cross-origin');
    context.header('Permissions-Policy', 'geolocation=(), microphone=(), camera=()');
    if (config.secureCookies) context.header('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
    if (!context.req.path.startsWith('/static/')) context.header('Cache-Control', 'private, no-store');
    context.set('session', null);
    await next();
  });
  app.use('*', bodyLimit({ maxSize: 21 * 1024 * 1024, onError: () => { throw new AppError(413, '送信内容が大きすぎます。写真は20MiB以下にしてください。'); } }));
  app.use('*', async (context, next) => {
    if (!context.req.path.startsWith('/static/') && context.req.path !== '/healthz') context.set('session', await auth.read(getCookie(context, sessionCookie)));
    await next();
  });
  const cookieOptions = { path: '/', httpOnly: true, secure: config.secureCookies, sameSite: 'Lax' as const, maxAge: 24 * 60 * 60 };
  app.get('/healthz', async context => {
    await database.query('SELECT 1 AS healthy', z.object({ healthy: z.literal(1) }));
    return context.json({ status: 'ok' });
  });
  app.get('/api/ui/session', async context => {
    let session = context.get('session');
    if (!session) { const issued = await auth.anonymous(); session = issued.session; setCookie(context, sessionCookie, issued.token, cookieOptions); }
    const error = context.req.query('error');
    return context.json(sessionSchema.parse({ authenticated: Boolean(session.access), is_admin: session.access?.admin ?? false, can_annotate: Boolean(session.access) && !config.readOnly, read_only: config.readOnly, csrf_token: session.stored.csrf_token, discord_enabled: true, username: session.stored.discord_username, login_error: error ? loginErrors[error] ?? 'ログインに失敗しました。' : null }));
  });
  app.get('/auth/discord', async context => {
    let session = context.get('session');
    if (!session) { const issued = await auth.anonymous(); session = issued.session; setCookie(context, sessionCookie, issued.token, cookieOptions); }
    return context.redirect(await auth.beginOAuth(session), 302);
  });
  app.get('/auth/discord/callback', async context => {
    const session = context.get('session');
    const code = context.req.query('code'); const state = context.req.query('state'); const denied = context.req.query('error');
    if (denied) return context.redirect('/login?error=discord_access_denied', 302);
    if (!code || !state) return context.redirect('/login?error=discord_missing_params', 302);
    if (!session || !await auth.consumeOAuth(session, state)) return context.redirect('/login?error=discord_state_mismatch', 302);
    try {
      const response = await fetch('https://discord.com/api/oauth2/token', { method: 'POST', body: new URLSearchParams({ client_id: config.clientId, client_secret: config.clientSecret, grant_type: 'authorization_code', code, redirect_uri: config.redirectUri }), signal: AbortSignal.timeout(10_000) });
      if (!response.ok) { console.error('Discord token exchange failed', { status: response.status }); return context.redirect('/login?error=discord_token_failed', 302); }
      const token = tokenResponse.safeParse(await response.json());
      if (!token.success) { console.error('Discord token response did not match its schema'); return context.redirect('/login?error=discord_token_failed', 302); }
      const userResponse = await fetch('https://discord.com/api/users/@me', { headers: { Authorization: 'Bearer ' + token.data.access_token }, signal: AbortSignal.timeout(10_000) });
      if (!userResponse.ok) { console.error('Discord user fetch failed', { status: userResponse.status }); return context.redirect('/login?error=discord_user_failed', 302); }
      const user = discordUser.safeParse(await userResponse.json());
      if (!user.success) { console.error('Discord user response did not match its schema'); return context.redirect('/login?error=discord_user_failed', 302); }
      const issued = await auth.issue(user.data.id, user.data.username, session);
      setCookie(context, sessionCookie, issued.token, cookieOptions);
      return context.redirect(issued.session.access?.admin ? '/admin/' : '/', 302);
    } catch (error) {
      if (error instanceof AppError && error.status === 403) return context.redirect('/login?error=discord_forbidden', 302);
      console.error('Discord OAuth callback failed', { error });
      return context.redirect('/login?error=discord_network', 302);
    }
  });
  app.post('/logout', async context => {
    const session = requireUser(context.get('session'));
    const body = await context.req.parseBody();
    const csrf = typeof body['csrf_token'] === 'string' ? body['csrf_token'] : '';
    if (!equalSecret(csrf, session.stored.csrf_token)) throw new AppError(403, '画面を再読み込みしてください。');
    await auth.revoke(session); deleteCookie(context, sessionCookie, cookieOptions);
    return context.redirect('/login', 302);
  });
  app.get('/api/ui/shops', async context => {
    requireUser(context.get('session'));
    return context.json(await shops.list(listQuerySchema.parse(Object.fromEntries(new URL(context.req.url).searchParams))));
  });
  app.get('/api/ui/shops/:id', async context => {
    const session = requireUser(context.get('session'));
    return context.json(await shops.detail(positiveId.parse(context.req.param('id')), session.access?.admin ?? false));
  });
  app.patch('/api/ui/shops/:id/annotation', async context => {
    const session = requireWrite(context.get('session'), context.req.header('X-CSRF-Token'), config);
    return context.json(await shops.annotate(positiveId.parse(context.req.param('id')), annotationUpdateSchema.parse(await context.req.json()), session.access?.admin ?? false));
  });
  app.get('/api/ui/editor-options', async context => {
    requireAdmin(context.get('session'));
    const categories = await database.query("SELECT DISTINCT s.category FROM shops s WHERE s.category IS NOT NULL AND s.category<>'' AND EXISTS(SELECT 1 FROM shop_mentions m WHERE m.shop_id=s.id AND m.review_status='approved' AND m.metadata_review_status='approved') ORDER BY s.category", z.object({ category: z.string() }));
    return context.json({ areas: [...areas].map(([value]) => ({ value, label: areaLabel(value), count: 0 })), categories: categories.map(row => row.category) });
  });
  registerUserRoutes(app, database, config);
  registerEditorRoutes(app, database, config);
  registerCsvRoutes(app, database, config);
  registerReviewRoutes(app, database, config);
  registerDecisionRoutes(app, database, config);
  registerLinkRoutes(app, database, config);
  registerSuggestionRoutes(app, database, config);
  app.get('/media/shop-uploads/:filename', async context => {
    const session = requireUser(context.get('session'));
    const filename = context.req.param('filename');
    if (!/^[a-f0-9]{64}\.webp$/.test(filename)) throw new AppError(404, '画像が見つかりません。');
    const rows = await database.query(`SELECT s.id FROM shops s WHERE s.image_key=$1${session.access?.admin ? '' : " AND EXISTS(SELECT 1 FROM shop_mentions m WHERE m.shop_id=s.id AND m.review_status='approved' AND m.metadata_review_status='approved')"} LIMIT 1`, z.object({ id: z.number() }), [filename.slice(0, -5)]);
    if (!rows.length) throw new AppError(404, '画像が見つかりません。');
    try { return context.body(await readFile(join(config.imageDirectory, 'uploads', filename)), 200, { 'Content-Type': 'image/webp' }); }
    catch (error) { if (error instanceof Error && 'code' in error && error.code === 'ENOENT') throw new AppError(404, '画像が見つかりません。'); throw error; }
  });
  app.use('/static/*', serveStatic({ root: fileURLToPath(new URL('../../web', import.meta.url)) }));
  const documentPaths = ['/', '/login', '/logout', '/admin/login', '/admin', '/admin/', '/admin/review', '/admin/users', '/admin/exports', '/admin/shops/bulk', '/shop/:id'];
  for (const path of documentPaths) app.get(path, async context => {
    const session = context.get('session'); const pathname = context.req.path;
    const login = pathname === '/login' || pathname === '/admin/login';
    if (!session?.access && !login && pathname !== '/logout') return context.redirect(pathname.startsWith('/admin') ? '/admin/login' : '/login', 302);
    if (pathname.startsWith('/admin') && !login && !session?.access?.admin) return context.redirect('/admin/login', 302);
    if (login && session?.access && (pathname === '/login' || session.access.admin)) return context.redirect(pathname === '/admin/login' ? '/admin/' : '/', 302);
    return context.html(await readFile(new URL('../../web/static/app/index.html', import.meta.url), 'utf8'));
  });
  app.notFound(() => { throw new AppError(404, 'ページが見つかりません。'); });
  app.onError((error, context) => {
    if (error instanceof SyntaxError) return context.json({ detail: 'JSONの形式を確認してください。' }, 422);
    if (error instanceof z.ZodError) return context.json({ detail: error.issues.map(issue => ({ msg: issue.path.join('.') + ': ' + issue.message })) }, 422);
    if (error instanceof AppError) {
      console.warn('Request rejected', { path: context.req.path, status: error.status, message: error.message });
      return context.json({ detail: error.message }, error.status);
    }
    console.error('Request failed', { path: context.req.path, method: context.req.method, error });
    return context.json({ detail: '処理を完了できませんでした。時間をおいて再試行してください。' }, 503);
  });
  return app;
}
