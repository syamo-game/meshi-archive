import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { z } from 'zod';
import { readBotSettings } from '../src/bot.js';
import { openDatabase } from '../src/db/database.js';
import { buildMessageEnvelope } from '../src/ingestion.js';
import type { DiscordInput } from '../src/ingestion.js';
import { lockChannelSync, nextWeeklyRun, syncLockKey, weeklySlot } from '../src/sync.js';

function message(): DiscordInput { return { id: '123456789012345678', channelId: '223456789012345678', content: '店舗の投稿 https://x.com/example/status/123456789012345678', createdAt: new Date('2026-10-18T00:00:00Z'), embeds: [], attachments: [] }; }
const baseline = z.object({ sync_keys: z.array(z.object({ channel_id: z.string(), expected: z.string() })), weekly: z.array(z.object({ input: z.string(), slot: z.string(), next: z.string() })) }).parse(JSON.parse(readFileSync(new URL('./parity.json', import.meta.url), 'utf8')));

test('Bot configuration keeps Bot administrator access separate from Web permissions', () => {
  const settings = readBotSettings({ DISCORD_TOKEN: 'synthetic-bot-token', ADMIN_USER_ID: '123456789012345678', WEB_ADMIN_USER_IDS: '323456789012345678', WEEKLY_SYNC_ENABLED: 'false' });
  assert.equal(settings.adminId, '123456789012345678');
  assert.equal(settings.batchLimit, 500);
  assert.throws(() => readBotSettings({ DISCORD_TOKEN: 'synthetic-bot-token', ADMIN_USER_ID: '123456789012345678', WEEKLY_SYNC_ENABLED: 'true' }), /DISCORD_CHANNEL_ID/);
  assert.throws(() => readBotSettings({ DISCORD_TOKEN: '', ADMIN_USER_ID: '123456789012345678' }), /DISCORD_TOKEN/);
});

test('Discord content retains embeds, source links, and attachment metadata', () => {
  const input = message();
  input.embeds = [{ url: 'https://x.com/example/status/123456789012345678', title: '店名', description: '所在地', author: '投稿者', image: 'https://cdn.discordapp.com/attachments/1/2/photo.jpg', thumbnail: null, fields: [{ name: '情報', value: '営業時間' }] }];
  input.attachments = [{ url: 'https://cdn.discordapp.com/attachments/1/2/photo.jpg', filename: 'photo.jpg', contentType: 'image/jpeg' }];
  const envelope = buildMessageEnvelope(input);
  assert.equal(envelope.assets.length, 3);
  assert.equal(envelope.assets.find(asset => asset.kind === 'image')?.is_embed_preview, false);
  assert.equal(envelope.assets.find(asset => asset.kind === 'image')?.title, 'photo.jpg');
  assert.match(envelope.content, /\[Embed Author\] 投稿者/);
  assert.match(envelope.content, /\[Embed Field: 情報\] 営業時間/);
  assert.match(envelope.content, /\[Attachment\] filename=photo.jpg content_type=image\/jpeg/);
  assert.equal(envelope.created_at, '2026-10-18T00:00:00.000Z');
});

test('asset limits are recorded without discarding original attachment metadata', () => {
  const input = message();
  input.attachments = Array.from({ length: 51 }, (_, i) => ({ url: `https://cdn.discordapp.com/attachments/1/${i}/photo.jpg`, filename: 'photo.jpg', contentType: 'image/jpeg' }));
  const envelope = buildMessageEnvelope(input);
  assert.equal(envelope.assets.length, 52);
  assert.equal(envelope.omitted_asset_count, 2);
  assert.throws(() => buildMessageEnvelope({ ...input, id: '1e18' }), /Invalid/);
});

test('weekly schedule and advisory lock keys match the Python production baseline', () => {
  for (const vector of baseline.sync_keys) assert.equal(syncLockKey(vector.channel_id), vector.expected);
  for (const vector of baseline.weekly) {
    assert.equal(weeklySlot(new Date(vector.input)).getTime(), Date.parse(vector.slot));
    assert.equal(nextWeeklyRun(new Date(vector.input)).getTime(), Date.parse(vector.next));
  }
});

test('channel lock rejects concurrent sync and releases after failures', async () => {
  const db = openDatabase('sqlite://');
  try {
    let release: () => void = () => { throw new Error('Lock gate was not initialized'); };
    const gate = new Promise<void>(resolve => { release = resolve; });
    const first = lockChannelSync(db, '123456789012345678', async () => { await gate; return 1; });
    await assert.rejects(lockChannelSync(db, '123456789012345678', async () => 2), /already running/);
    release(); assert.equal(await first, 1);
    await assert.rejects(lockChannelSync(db, '123456789012345678', async () => { throw new Error('Actual sync failure'); }), /Actual sync failure/);
    assert.equal(await lockChannelSync(db, '123456789012345678', async () => 3), 3);
  } finally { await db.close(); }
});
