import { blake2b } from '@noble/hashes/blake2.js';
import { DiscordAPIError, RESTJSONErrorCodes } from 'discord.js';
import type { Message, SendableChannels } from 'discord.js';
import { z } from 'zod';
import type { Database } from './db/database.js';
import { booleanColumn, timestampColumn } from './db/database.js';
import { acquireMessageEnvelope, discordInput } from './ingestion.js';
import type { DiscordInput, MessageProcessor } from './ingestion.js';

const week = 7 * 24 * 60 * 60 * 1000;
const states = z.object({ last_contiguous_message_id: z.string().nullable(), last_weekly_sync_started_at: timestampColumn.nullable() });
const processing = z.object({ processing_status: z.string() });
const active = new Set<string>();
export class SyncAlreadyRunning extends Error {}
// Stop when this many messages fail in a row. That pattern means an outage, not one bad post.
export const consecutiveFailureLimit = 3;
export class HistorySyncError extends Error {
  constructor(readonly channelId: string, readonly messageId: string, readonly failed: readonly string[], options: ErrorOptions) { super(`History sync stopped after ${consecutiveFailureLimit} failures in a row: channel_id=${channelId} message_id=${messageId} failed=${failed.join(',')}`, options); }
}
export interface SyncSummary { fetched: number; processed: number; registered: number; pending: number; skipped: number; retried: number; failed: string[]; limit_reached: boolean; }
export interface SyncMessage { id: string; authorId: string; empty: boolean; input: DiscordInput; }
export interface HistorySource { id: string; after(cursor: string, limit: number): Promise<SyncMessage[]>; one(id: string): Promise<SyncMessage | null>; }
function syncMessage(message: Message): SyncMessage { return { id: message.id, authorId: message.author.id, empty: !message.content.trim() && !message.embeds.length && !message.attachments.size, input: discordInput(message) }; }
export function discordHistory(channel: SendableChannels): HistorySource {
  return {
    id: channel.id,
    after: async (cursor, limit) => [...(await channel.messages.fetch({ after: cursor, limit, cache: false })).values()].map(syncMessage),
    // A deleted message gives null. Every other error is raised.
    one: async id => {
      try { return syncMessage(await channel.messages.fetch({ message: id, cache: false, force: true })); }
      catch (error) { if (error instanceof DiscordAPIError && error.code === RESTJSONErrorCodes.UnknownMessage) return null; throw error; }
    },
  };
}
export function syncReport(label: string, result: SyncSummary): string {
  const lines = [`${label}が完了しました。処理=${result.processed}件、店舗候補=${result.registered}件、データ確認=${result.pending}件、失敗=${result.failed.length}件。`];
  if (result.failed.length) lines.push(`失敗した投稿は次回の同期で再試行します。message_id=${result.failed.slice(0, 10).join(', ')}${result.failed.length > 10 ? ' ほか' : ''}`);
  return lines.join('\n');
}
export interface WeeklyAttempt { slot: Date; startedAt: Date; force: boolean; }
export function weeklySlot(now: Date): Date {
  if (!Number.isFinite(now.getTime())) throw new Error('Weekly sync requires a valid timestamp');
  const local = new Date(now.getTime() + 9 * 60 * 60 * 1000);
  let slot = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate() - (local.getUTCDay() + 6) % 7, 4) - 9 * 60 * 60 * 1000;
  if (slot > now.getTime()) slot -= week;
  return new Date(slot);
}
export function nextWeeklyRun(now: Date): Date { return new Date(weeklySlot(now).getTime() + week); }
export function syncLockKey(channelId: string): string { return Buffer.from(blake2b(new TextEncoder().encode('meshi-history-sync:' + channelId), { dkLen: 8 })).readBigInt64BE().toString(); }
export async function lockChannelSync<Result>(database: Database, channelId: string, operation: () => Promise<Result>): Promise<Result> {
  if (active.has(channelId)) throw new SyncAlreadyRunning(`Channel sync is already running: channel_id=${channelId}`);
  active.add(channelId);
  try {
    if (database.dialect === 'sqlite') return await operation();
    return await database.withConnection(async connection => {
      const row = (await connection.query('SELECT pg_try_advisory_lock($1::bigint) AS acquired', z.object({ acquired: booleanColumn }), [syncLockKey(channelId)]))[0];
      if (!row?.acquired) throw new SyncAlreadyRunning(`Channel sync is already running: channel_id=${channelId}`);
      try { return await operation(); }
      finally {
        const unlocked = (await connection.query('SELECT pg_advisory_unlock($1::bigint) AS released', z.object({ released: booleanColumn }), [syncLockKey(channelId)]))[0];
        if (!unlocked?.released) throw new Error(`Channel sync lock was lost: channel_id=${channelId}`);
      }
    });
  } finally { active.delete(channelId); }
}
async function* history(channel: HistorySource, after: string | null, limit: number): AsyncGenerator<SyncMessage> {
  let cursor = after ?? '0'; let remaining = limit;
  while (remaining > 0) {
    const batch = await channel.after(cursor, Math.min(100, remaining));
    const ordered = [...batch].sort((a, b) => BigInt(a.id) < BigInt(b.id) ? -1 : BigInt(a.id) > BigInt(b.id) ? 1 : 0);
    if (!ordered.length) return;
    for (const message of ordered) {
      if (BigInt(message.id) <= BigInt(cursor)) throw new Error(`Discord history did not advance: channel_id=${channel.id}`);
      yield message; cursor = message.id; remaining--;
    }
  }
}
export async function syncChannelHistory(database: Database, processor: MessageProcessor, channel: HistorySource, selfId: string, batchLimit: number, readOnly: boolean, weekly?: WeeklyAttempt): Promise<SyncSummary | null> {
  if (readOnly) throw new Error('Channel sync refused because APP_READ_ONLY=true');
  return lockChannelSync(database, channel.id, async () => {
    await database.execute('INSERT INTO sync_states(channel_id,updated_at) VALUES($1,$2) ON CONFLICT(channel_id) DO NOTHING', [channel.id, new Date().toISOString()]);
    const state = (await database.query('SELECT last_contiguous_message_id,last_weekly_sync_started_at FROM sync_states WHERE channel_id=$1', states, [channel.id]))[0];
    if (!state) throw new Error(`Missing channel sync state: channel_id=${channel.id}`);
    if (weekly) {
      if (!weekly.force && state.last_weekly_sync_started_at && Date.parse(state.last_weekly_sync_started_at) >= weekly.slot.getTime()) return null;
      await database.execute('UPDATE sync_states SET last_weekly_sync_started_at=$1 WHERE channel_id=$2', [weekly.startedAt.toISOString(), channel.id]);
    }
    const result: SyncSummary = { fetched: 0, processed: 0, registered: 0, pending: 0, skipped: 0, retried: 0, failed: [], limit_reached: false };
    let consecutive = 0; let lastError: Error | null = null;
    // One failed message must not block the ones after it. It stays failed and the next sync retries it.
    const attempt = async (message: SyncMessage): Promise<void> => {
      try {
        const response = await processor.processMessage(await acquireMessageEnvelope(message.input, processor));
        result.processed++; result.registered += response.shop_ids.length; result.pending += response.pending_mention_ids.length; consecutive = 0;
      } catch (error) {
        console.error('History sync message failed', { channelId: channel.id, messageId: message.id, error });
        const failure = error instanceof Error ? error : new Error(String(error)); lastError = failure;
        await database.execute("INSERT INTO messages(message_id,channel_id,content,source_created_at,is_target,processing_status,fetch_error) VALUES($1,$2,$3,$4,$5,'failed',$6) ON CONFLICT(message_id) DO UPDATE SET processing_status='failed',channel_id=excluded.channel_id", [message.id, channel.id, message.input.content.trim() || null, message.input.createdAt.toISOString(), true, `${failure.name}: ${failure.message}`]);
        result.failed.push(message.id); consecutive++;
      }
    };
    const stopIfRepeated = (messageId: string): void => { if (consecutive >= consecutiveFailureLimit) throw new HistorySyncError(channel.id, messageId, result.failed, { cause: lastError }); };
    if (state.last_contiguous_message_id) {
      const cursor = BigInt(state.last_contiguous_message_id);
      const unfinished = await database.query("SELECT message_id FROM messages WHERE channel_id=$1 AND processing_status NOT IN ('succeeded','ignored')", z.object({ message_id: z.string() }), [channel.id]);
      for (const id of unfinished.map(row => row.message_id).filter(value => BigInt(value) <= cursor).sort((a, b) => BigInt(a) < BigInt(b) ? -1 : 1)) {
        const message = await channel.one(id);
        if (!message) { await database.execute("UPDATE messages SET processing_status='ignored',is_target=$1,fetch_error=$2,processed_at=$3 WHERE message_id=$4", [false, 'Discord message was deleted before it could be processed', new Date().toISOString(), id]); result.skipped++; continue; }
        result.retried++; await attempt(message); stopIfRepeated(id);
      }
    }
    for await (const message of history(channel, state.last_contiguous_message_id, batchLimit)) {
      result.fetched++;
      const existing = (await database.query('SELECT processing_status FROM messages WHERE message_id=$1', processing, [message.id]))[0];
      if (message.authorId === selfId || message.empty || (existing && ['succeeded', 'ignored'].includes(existing.processing_status))) result.skipped++;
      else await attempt(message);
      await database.execute('UPDATE sync_states SET last_contiguous_message_id=$1,updated_at=$2 WHERE channel_id=$3', [message.id, new Date().toISOString(), channel.id]);
      stopIfRepeated(message.id);
    }
    if (weekly) await database.execute('UPDATE sync_states SET last_weekly_sync_completed_at=$1 WHERE channel_id=$2', [new Date().toISOString(), channel.id]);
    result.limit_reached = result.fetched >= batchLimit;
    console.info('Channel sync completed', { channelId: channel.id, result });
    return result;
  });
}
