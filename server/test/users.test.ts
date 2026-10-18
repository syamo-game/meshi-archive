import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { usersSchema } from '@meshi/contracts';
import { Authentication } from '../src/auth.js';
import { fixture, headers } from './helpers.js';

test('user changes are guarded, audited, and invalidate the previous session', async () => {
  const { app, database, admin, config } = await fixture();
  try {
    const userId = '323456789012345678';
    const post = (path: string, values: Record<string, string>): Promise<Response> => app.request(path, { method: 'POST', headers: headers(admin), body: new URLSearchParams(values) });
    assert.equal((await post('/api/ui/admin/users', { discord_user_id: userId, role: 'viewer' })).status, 200);
    const auth = new Authentication(database, config);
    const oldSession = await auth.issue(userId, '追加ユーザー');
    const list = usersSchema.parse(await (await app.request('/api/ui/admin/users', { headers: headers(admin) })).json());
    const user = list.entries.find(entry => entry.user_id === userId); assert.ok(user?.generation);
    assert.equal((await post('/api/ui/admin/users/' + userId, { expected_generation: user.generation, replacement_user_id: userId, role: 'admin', action: 'save' })).status, 200);
    assert.equal(await auth.read(oldSession.token), null);
    const changed = usersSchema.parse(await (await app.request('/api/ui/admin/users', { headers: headers(admin) })).json()).entries.find(entry => entry.user_id === userId); assert.ok(changed?.generation);
    assert.equal(changed.is_admin, true); assert.notEqual(changed.generation, user.generation);
    assert.equal((await post('/api/ui/admin/users/' + userId, { expected_generation: user.generation, replacement_user_id: userId, role: 'viewer', action: 'save' })).status, 409);
    assert.equal((await post('/api/ui/admin/users/' + userId, { expected_generation: changed.generation, role: 'admin', action: 'revoke' })).status, 200);
    assert.equal(await auth.access(userId), null);
    const events = await database.query('SELECT action,previous_role,new_role FROM discord_viewer_grant_events WHERE discord_user_id=$1 ORDER BY id', z.object({ action: z.string(), previous_role: z.string().nullable(), new_role: z.string().nullable() }), [userId]);
    assert.deepEqual(events, [{ action: 'grant', previous_role: null, new_role: 'viewer' }, { action: 'update', previous_role: 'viewer', new_role: 'admin' }, { action: 'revoke', previous_role: 'admin', new_role: null }]);
  } finally { await database.close(); }
});

test('failed auditing rolls back the permission change instead of leaving an untracked grant', async () => {
  const { app, database, admin } = await fixture();
  try {
    await database.script('DROP TABLE discord_viewer_grant_events');
    const response = await app.request('/api/ui/admin/users', { method: 'POST', headers: headers(admin), body: new URLSearchParams({ discord_user_id: '323456789012345678', role: 'viewer' }) });
    assert.equal(response.status, 503);
    const rows = await database.query('SELECT discord_user_id FROM discord_viewers WHERE discord_user_id=$1', z.object({ discord_user_id: z.string() }), ['323456789012345678']);
    assert.equal(rows.length, 0);
    assert.doesNotMatch(await response.text(), /no such table|INSERT|token_hash/);
  } finally { await database.close(); }
});

test('configuration grants stay fixed and duplicate adds do not add duplicate audit records', async () => {
  const { app, database, admin, viewer } = await fixture();
  try {
    assert.equal((await app.request('/api/ui/admin/users', { headers: headers(viewer) })).status, 403);
    const values = { discord_user_id: '323456789012345678', role: 'viewer' };
    const add = (): Promise<Response> => app.request('/api/ui/admin/users', { method: 'POST', headers: headers(admin), body: new URLSearchParams(values) });
    assert.deepEqual((await Promise.all([add(), add()])).map(response => response.status), [200, 200]);
    const events = await database.query('SELECT action FROM discord_viewer_grant_events', z.object({ action: z.string() }));
    assert.equal(events.length, 1);
    const fixed = await app.request('/api/ui/admin/users/123456789012345678', { method: 'POST', headers: headers(admin), body: new URLSearchParams({ expected_generation: 'configuration', replacement_user_id: '123456789012345678', role: 'admin', action: 'revoke' }) });
    assert.equal(fixed.status, 409);
  } finally { await database.close(); }
});
