import { blake2b } from '@noble/hashes/blake2.js';
import type { Message, SendableChannels } from 'discord.js';
import { z } from 'zod';
import type { Database } from './db/database.js';
import { booleanColumn, timestampColumn } from './db/database.js';
import { acquireMessageEnvelope, discordInput } from './ingestion.js';
import type { MessageProcessor } from './ingestion.js';

const week = 7 * 24 * 60 * 60 * 1000;
const states = z.object({ last_contiguous_message_id: z.string().nullable(), last_weekly_sync_started_at: timestampColumn.nullable() });
const processing = z.object({ processing_status: z.string() });
const active = new Set<string>();
export class SyncAlreadyRunning extends Error {}
export class HistorySyncError extends Error {
  constructor(readonly channelId: string, readonly messageId: string, options: ErrorOptions) { super(`History sync stopped: channel_id=${channelId} message_id=${messageId}`, options); }
}
export interface SyncSummary { fetched: number; processed: number; registered: number; pending: number; skipped: number; limit_reached: boolean; }
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
async function* history(channel: SendableChannels, after: string | null, limit: number): AsyncGenerator<Message> {
  let cursor = after ?? '0'; let remaining = limit;
  while (remaining > 0) {
    const batch = await channel.messages.fetch({ after: cursor, limit: Math.min(100, remaining), cache: false });
    const ordered = [...batch.values()].sort((a, b) => BigInt(a.id) < BigInt(b.id) ? -1 : BigInt(a.id) > BigInt(b.id) ? 1 : 0);
    if (!ordered.length) return;
    for (const message of ordered) {
      if (BigInt(message.id) <= BigInt(cursor)) throw new Error(`Discord history did not advance: channel_id=${channel.id}`);
      yield message; cursor = message.id; remaining--;
    }
  }
}
export async function syncChannelHistory(database: Database, processor: MessageProcessor, channel: SendableChannels, selfId: string, batchLimit: number, readOnly: boolean, weekly?: WeeklyAttempt): Promise<SyncSummary | null> {
  if (readOnly) throw new Error('Channel sync refused because APP_READ_ONLY=true');
  return lockChannelSync(database, channel.id, async () => {
    await database.execute('INSERT INTO sync_states(channel_id,updated_at) VALUES($1,$2) ON CONFLICT(channel_id) DO NOTHING', [channel.id, new Date().toISOString()]);
    const state = (await database.query('SELECT last_contiguous_message_id,last_weekly_sync_started_at FROM sync_states WHERE channel_id=$1', states, [channel.id]))[0];
    if (!state) throw new Error(`Missing channel sync state: channel_id=${channel.id}`);
    if (weekly) {
      if (!weekly.force && state.last_weekly_sync_started_at && Date.parse(state.last_weekly_sync_started_at) >= weekly.slot.getTime()) return null;
      await database.execute('UPDATE sync_states SET last_weekly_sync_started_at=$1 WHERE channel_id=$2', [weekly.startedAt.toISOString(), channel.id]);
    }
    const result: SyncSummary = { fetched: 0, processed: 0, registered: 0, pending: 0, skipped: 0, limit_reached: false };
    for await (const message of history(channel, state.last_contiguous_message_id, batchLimit)) {
      result.fetched++;
      const existing = (await database.query('SELECT processing_status FROM messages WHERE message_id=$1', processing, [message.id]))[0];
      if (message.author.id === selfId || (!message.content.trim() && !message.embeds.length && !message.attachments.size) || (existing && ['succeeded', 'ignored'].includes(existing.processing_status))) result.skipped++;
      else {
        try {
          const response = await processor.processMessage(await acquireMessageEnvelope(discordInput(message), processor));
          result.processed++; result.registered += response.shop_ids.length; result.pending += response.pending_mention_ids.length;
        } catch (error) { throw new HistorySyncError(channel.id, message.id, { cause: error }); }
      }
      await database.execute('UPDATE sync_states SET last_contiguous_message_id=$1,updated_at=$2 WHERE channel_id=$3', [message.id, new Date().toISOString(), channel.id]);
    }
    if (weekly) await database.execute('UPDATE sync_states SET last_weekly_sync_completed_at=$1 WHERE channel_id=$2', [new Date().toISOString(), channel.id]);
    result.limit_reached = result.fetched >= batchLimit;
    console.info('Channel sync completed', { channelId: channel.id, result });
    return result;
  });
}
