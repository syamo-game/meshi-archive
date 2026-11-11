import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { z } from 'zod';
import { readBotSettings } from '../src/bot.js';
import { openDatabase } from '../src/db/database.js';
import type { Database } from '../src/db/database.js';
import { buildMessageEnvelope } from '../src/ingestion.js';
import type { DiscordInput, MessageEnvelope, MessageProcessor, ProcessResult } from '../src/ingestion.js';
import { HistorySyncError, lockChannelSync, nextWeeklyRun, syncChannelHistory, syncLockKey, syncReport, weeklySlot } from '../src/sync.js';
import type { HistorySource, SyncMessage } from '../src/sync.js';
import { fixture } from './helpers.js';

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

const syncChannel = '423456789012345678';
function syncMessages(count: number): SyncMessage[] {
  return Array.from({ length: count }, (_, index) => { const id = '8000000000000000' + (index + 1); return { id, authorId: '323456789012345678', empty: false, input: { id, channelId: syncChannel, content: '投稿 ' + id, createdAt: new Date('2026-10-18T00:00:00Z'), embeds: [], attachments: [] } }; });
}
function historySource(messages: readonly SyncMessage[], deleted: ReadonlySet<string> = new Set()): HistorySource {
  return { id: syncChannel, after: async (cursor, limit) => messages.filter(item => BigInt(item.id) > BigInt(cursor)).slice(0, limit), one: async id => deleted.has(id) ? null : messages.find(item => item.id === id) ?? null };
}
// Stands in for the AI pipeline: chosen messages fail, the others are stored as succeeded.
class SyncProcessor implements MessageProcessor {
  readonly failing = new Set<string>();
  constructor(private readonly database: Database) {}
  async processMessage(envelope: MessageEnvelope): Promise<ProcessResult> {
    if (this.failing.has(envelope.message_id)) throw new Error('Synthetic pipeline failure: message_id=' + envelope.message_id);
    await this.database.execute("INSERT INTO messages(message_id,channel_id,is_target,processing_status) VALUES($1,$2,$3,'succeeded') ON CONFLICT(message_id) DO UPDATE SET processing_status='succeeded'", [envelope.message_id, envelope.channel_id, true]);
    return { message_id: envelope.message_id, shop_ids: [], pending_mention_ids: [], ignored: false };
  }
  async cacheImage(): Promise<void> { throw new Error('Sync fixtures have no images'); }
  async preflightModels(): Promise<void> { throw new Error('Sync fixtures do not call models'); }
}
const syncRow = z.object({ processing_status: z.string(), channel_id: z.string().nullable(), fetch_error: z.string().nullable() });
const cursorRow = z.object({ last_contiguous_message_id: z.string().nullable() });

test('history sync passes a failed message, keeps it failed and retries it on the next sync', async () => {
  const { database } = await fixture();
  try {
    const messages = syncMessages(5); const processor = new SyncProcessor(database); const failed = messages[1]?.id ?? ''; processor.failing.add(failed);
    const first = await syncChannelHistory(database, processor, historySource(messages), '523456789012345678', 500, false);
    assert.deepEqual(first, { fetched: 5, processed: 4, registered: 0, pending: 0, skipped: 0, retried: 0, failed: [failed], limit_reached: false });
    assert.equal((await database.query('SELECT last_contiguous_message_id FROM sync_states WHERE channel_id=$1', cursorRow, [syncChannel]))[0]?.last_contiguous_message_id, messages[4]?.id);
    const stored = (await database.query('SELECT processing_status,channel_id,fetch_error FROM messages WHERE message_id=$1', syncRow, [failed]))[0];
    assert.equal(stored?.processing_status, 'failed'); assert.equal(stored?.channel_id, syncChannel); assert.match(stored?.fetch_error ?? '', /Synthetic pipeline failure/);
    assert.equal(syncReport('同期', first), `同期が完了しました。処理=4件、店舗候補=0件、データ確認=0件、失敗=1件。\n失敗した投稿は次回の同期で再試行します。message_id=${failed}`);
    const again = await syncChannelHistory(database, processor, historySource(messages), '523456789012345678', 500, false);
    assert.deepEqual(again?.failed, [failed]); assert.equal(again?.retried, 1); assert.equal(again?.fetched, 0);
    processor.failing.clear();
    const recovered = await syncChannelHistory(database, processor, historySource(messages), '523456789012345678', 500, false);
    assert.deepEqual(recovered, { fetched: 0, processed: 1, registered: 0, pending: 0, skipped: 0, retried: 1, failed: [], limit_reached: false });
    assert.equal((await database.query('SELECT processing_status,channel_id,fetch_error FROM messages WHERE message_id=$1', syncRow, [failed]))[0]?.processing_status, 'succeeded');
    assert.equal((await syncChannelHistory(database, processor, historySource(messages), '523456789012345678', 500, false))?.retried, 0);
  } finally { await database.close(); }
});

test('history sync stops after three failures in a row and closes a deleted failed message', async () => {
  const { database } = await fixture();
  try {
    const messages = syncMessages(6); const processor = new SyncProcessor(database); const ids = messages.map(item => item.id);
    for (const id of ids.slice(1, 4)) processor.failing.add(id);
    await assert.rejects(syncChannelHistory(database, processor, historySource(messages), '523456789012345678', 500, false), error => error instanceof HistorySyncError && error.failed.join() === ids.slice(1, 4).join() && error.messageId === ids[3]);
    assert.equal((await database.query('SELECT last_contiguous_message_id FROM sync_states WHERE channel_id=$1', cursorRow, [syncChannel]))[0]?.last_contiguous_message_id, ids[3]);
    assert.equal((await database.query('SELECT processing_status,channel_id,fetch_error FROM messages WHERE message_id=$1', syncRow, [ids[4]])).length, 0);
    processor.failing.clear();
    const resumed = await syncChannelHistory(database, processor, historySource(messages, new Set([ids[2] ?? ''])), '523456789012345678', 500, false);
    assert.deepEqual(resumed, { fetched: 2, processed: 4, registered: 0, pending: 0, skipped: 1, retried: 2, failed: [], limit_reached: false });
    const deleted = (await database.query('SELECT processing_status,channel_id,fetch_error FROM messages WHERE message_id=$1', syncRow, [ids[2]]))[0];
    assert.equal(deleted?.processing_status, 'ignored'); assert.match(deleted?.fetch_error ?? '', /deleted/);
  } finally { await database.close(); }
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
