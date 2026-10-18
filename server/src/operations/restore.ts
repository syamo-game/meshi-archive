import { setTimeout as delay } from 'node:timers/promises';
import { z } from 'zod';
import type { Database, Executor, SqlValue } from '../db/database.js';
import { assetRecord, messageRecord } from '../records.js';
import { sourceAssetFingerprint, sourceAssetIdentity } from '../source-identity.js';

const id = z.string().regex(/^\d{17,20}$/);
const text = z.string().nullable().default(null);
const media = z.object({ url: z.string().url() }).nullable().default(null);
export const discordPayloadSchema = z.object({ id, channel_id: id, content: z.string(), timestamp: z.string().refine(value => Number.isFinite(Date.parse(value))), attachments: z.array(z.object({ id: z.string(), filename: z.string(), size: z.number().int().nonnegative(), url: z.string().url(), proxy_url: z.string().url(), content_type: text, description: text })), embeds: z.array(z.object({ title: text, description: text, url: text, image: media, thumbnail: media })) });
export type DiscordPayload = z.infer<typeof discordPayloadSchema>;
export interface FetchOutcome { payload: DiscordPayload | null; error: string | null; http_status: number | null; }
export interface RestoreOptions { apply: boolean; limit: number | null; channelId: string | null; messageIds: readonly string[]; }
export interface RestoreCounts { requested: number; restored: number; unavailable: number; failed: number; }
export async function fetchDiscordMessage(token: string, channelId: string, messageId: string): Promise<FetchOutcome> {
  id.parse(channelId); id.parse(messageId); if (!token) throw new Error('DISCORD_TOKEN is required');
  const context = `channel_id=${channelId} message_id=${messageId}`;
  for (let attempt = 0; attempt < 4; attempt++) {
    let response: Response;
    try { response = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages/${messageId}`, { headers: { Authorization: 'Bot ' + token }, signal: AbortSignal.timeout(30000), redirect: 'error' }); }
    catch (error) { console.error('Discord source request failed', { channelId, messageId, attempt, error }); if (attempt < 3) { await delay(Math.min(8, 2 ** attempt) * 1000); continue; } return { payload: null, error: `stage=discord_fetch ${context} error=${error instanceof Error ? error.message : String(error)}`, http_status: null }; }
    const body = await response.text();
    if (response.status === 200) {
      try {
        const payload = discordPayloadSchema.parse(JSON.parse(body));
        if (payload.id !== messageId || payload.channel_id !== channelId) return { payload: null, error: `stage=discord_validate http_status=200 ${context} payload_channel_id=${payload.channel_id} payload_message_id=${payload.id}`, http_status: 200 };
        return { payload, error: null, http_status: 200 };
      } catch (error) { if (!(error instanceof SyntaxError || error instanceof z.ZodError)) throw error; return { payload: null, error: `stage=discord_parse http_status=200 ${context} error=${error.message}`, http_status: 200 }; }
    }
    if (response.status === 401) throw new Error(`Discord API returned HTTP 401: ${context}`);
    if ([403, 404].includes(response.status)) return { payload: null, error: `source_unavailable stage=discord_fetch http_status=${response.status} ${context}`, http_status: response.status };
    if ((response.status === 429 || (response.status >= 500 && response.status <= 599)) && attempt < 3) {
      let seconds = Number(response.headers.get('retry-after') ?? 'NaN');
      if (!Number.isFinite(seconds) && response.status === 429) {
        try { seconds = z.object({ retry_after: z.number() }).parse(JSON.parse(body)).retry_after; }
        catch (error) { if (!(error instanceof SyntaxError || error instanceof z.ZodError)) throw error; console.warn('Discord rate limit response has no valid delay', { channelId, messageId, status: response.status }); }
      }
      await delay((Number.isFinite(seconds) ? Math.max(0, seconds) : Math.min(8, 2 ** attempt)) * 1000); continue;
    }
    return { payload: null, error: `stage=discord_fetch http_status=${response.status} ${context} response=${body.slice(0, 500)}`, http_status: response.status };
  }
  throw new Error(`Discord retry loop ended unexpectedly: ${context}`);
}
interface AssetInput { kind: 'link' | 'embed' | 'image' | 'attachment'; url: string; title: string | null; description: string | null; mime_type: string | null; }
export function restoredAssets(payload: DiscordPayload): AssetInput[] {
  const rows: AssetInput[] = [];
  for (const match of payload.content.matchAll(/https?:\/\/[^\s<>]+/g)) rows.push({ kind: 'link', url: match[0].replace(/[.,);\]）】]+$/, ''), title: null, description: null, mime_type: null });
  for (const embed of payload.embeds) {
    if (embed.url) rows.push({ kind: 'embed', url: embed.url, title: embed.title, description: embed.description, mime_type: null });
    for (const image of [embed.image, embed.thumbnail]) if (image) rows.push({ kind: 'image', url: image.url, title: embed.title, description: embed.description, mime_type: null });
  }
  for (const attachment of payload.attachments) rows.push({ kind: attachment.content_type?.startsWith('image/') ? 'image' : 'attachment', url: attachment.url, title: attachment.filename, description: attachment.description, mime_type: attachment.content_type });
  return [...new Map(rows.map(row => [JSON.stringify(row), row])).values()];
}
export async function saveRestoreOutcome(connection: Executor, messageId: string, outcome: FetchOutcome, latency: number): Promise<void> {
  const now = new Date().toISOString();
  if (outcome.payload) {
    const payload = outcome.payload;
    await connection.execute('UPDATE messages SET channel_id=$1,content=$2,source_created_at=$3,fetch_error=NULL WHERE message_id=$4', [payload.channel_id, payload.content, payload.timestamp, messageId]);
    for (const row of restoredAssets(payload)) {
      const previous = (await connection.query('SELECT * FROM source_assets WHERE message_id=$1 AND kind=$2 AND url=$3', assetRecord, [messageId, row.kind, row.url]))[0];
      const identity = sourceAssetIdentity(row.url); const normalized = identity?.normalized_url ?? row.url;
      const fingerprint = sourceAssetFingerprint({ ...row, normalized_url: normalized, extracted_text: previous?.extracted_text ?? null });
      await connection.execute("INSERT INTO source_assets(message_id,kind,url,title,description,mime_type,source_service,source_item_id,normalized_url,content_fingerprint,fetch_status,created_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'available',$11) ON CONFLICT(message_id,kind,url) DO UPDATE SET title=excluded.title,description=excluded.description,mime_type=excluded.mime_type,source_service=excluded.source_service,source_item_id=excluded.source_item_id,normalized_url=excluded.normalized_url,content_fingerprint=excluded.content_fingerprint,fetch_status='available',fetch_error=NULL", [messageId, row.kind, row.url, row.title, row.description, row.mime_type, identity?.source_service ?? null, identity?.source_item_id ?? null, normalized, fingerprint, now]);
    }
    await connection.execute("UPDATE shop_mentions SET difference_type='source_restored' WHERE message_id=$1 AND difference_type='source_unavailable'", [messageId]);
  } else {
    await connection.execute('UPDATE messages SET fetch_error=$1 WHERE message_id=$2', [outcome.error, messageId]);
    await connection.execute("UPDATE shop_mentions SET review_status='pending',difference_type=COALESCE(difference_type,'source_unavailable'),extraction_error=$1 WHERE message_id=$2", [outcome.error, messageId]);
  }
  await connection.execute("INSERT INTO processing_runs(message_id,stage,status,input_tokens,output_tokens,web_search_calls,image_count,latency_ms,estimated_cost_microusd,error,created_at) VALUES($1,'discord_restore',$2,0,0,0,0,$3,0,$4,$5)", [messageId, outcome.payload ? 'succeeded' : 'failed', Math.floor(latency), outcome.error, now]);
}
export async function restoreSources(database: Database, token: string, options: RestoreOptions): Promise<RestoreCounts> {
  if (options.limit !== null) z.number().int().positive().parse(options.limit);
  if (options.limit !== null && options.messageIds.length) throw new Error('Limit and message IDs cannot be combined');
  if (options.channelId) id.parse(options.channelId); for (const messageId of options.messageIds) id.parse(messageId);
  let sql = 'SELECT * FROM messages'; const values: SqlValue[] = [];
  if (options.messageIds.length) { values.push(...options.messageIds); sql += ' WHERE message_id IN (' + values.map((_, index) => '$' + (index + 1)).join(',') + ')'; }
  sql += ' ORDER BY message_id'; if (options.limit !== null) { values.push(options.limit); sql += ' LIMIT $' + values.length; }
  const messages = await database.query(sql, messageRecord, values); const found = new Set(messages.map(message => message.message_id)); const missing = options.messageIds.filter(messageId => !found.has(messageId));
  if (missing.length) throw new Error('Requested messages are missing from the database: message_ids=' + missing.join(','));
  const counts: RestoreCounts = { requested: 0, restored: 0, unavailable: 0, failed: 0 };
  for (const message of messages) {
    const channel = message.channel_id || options.channelId; if (!channel) throw new Error(`Discord channel is missing: message_id=${message.message_id}, set --channel-id or DISCORD_CHANNEL_ID`);
    counts.requested++; const started = performance.now(); const outcome = await fetchDiscordMessage(token, channel, message.message_id);
    if (outcome.payload) counts.restored++; else if (outcome.http_status === 403 || outcome.http_status === 404) counts.unavailable++; else counts.failed++;
    if (options.apply) await database.transaction(connection => saveRestoreOutcome(connection, message.message_id, outcome, performance.now() - started));
  }
  return counts;
}
