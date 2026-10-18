import type { Message } from 'discord.js';
import { z } from 'zod';
import { safeHttpUrl } from './links.js';

const assetSchema = z.strictObject({ kind: z.enum(['link', 'embed', 'image', 'attachment']), url: z.string().trim().refine(value => safeHttpUrl(value) !== null, 'Asset URL must use HTTP or HTTPS'), title: z.string().trim().max(2000).nullable().default(null), description: z.string().trim().max(20000).nullable().default(null), mime_type: z.string().trim().max(255).nullable().default(null), is_embed_preview: z.boolean().default(false) }).refine(value => !value.is_embed_preview || value.kind === 'image', 'Only image assets can be embed previews');
const snowflake = z.string().regex(/^\d{17,20}$/);
export const envelopeSchema = z.strictObject({ message_id: snowflake, channel_id: snowflake, content: z.string().trim().min(1).max(100000), created_at: z.string().datetime(), assets: z.array(assetSchema), omitted_asset_count: z.number().int().nonnegative() });
export type MessageEnvelope = z.infer<typeof envelopeSchema>;
export interface ProcessResult { message_id: string; shop_ids: readonly number[]; pending_mention_ids: readonly number[]; ignored: boolean; }
export interface MessageProcessor {
  processMessage(envelope: MessageEnvelope): Promise<ProcessResult>;
  cacheImage(url: string): Promise<void>;
  preflightModels(): Promise<void>;
}
interface EmbedInput { url: string | null; title: string | null; description: string | null; author: string | null; image: string | null; thumbnail: string | null; fields: readonly { name: string; value: string }[]; }
export interface DiscordInput { id: string; channelId: string; content: string; createdAt: Date; embeds: readonly EmbedInput[]; attachments: readonly { url: string; filename: string; contentType: string | null }[]; }
export function discordInput(message: Message): DiscordInput {
  return { id: message.id, channelId: message.channelId, content: message.content, createdAt: message.createdAt, embeds: message.embeds.map(embed => ({ url: embed.url, title: embed.title, description: embed.description, author: embed.author?.name ?? null, image: embed.image?.url ?? null, thumbnail: embed.thumbnail?.url ?? null, fields: embed.fields })), attachments: [...message.attachments.values()].map(attachment => ({ url: attachment.url, filename: attachment.name, contentType: attachment.contentType })) };
}
export function buildMessageEnvelope(message: DiscordInput): MessageEnvelope {
  const assets: z.infer<typeof assetSchema>[] = [];
  const parts = message.content.trim() ? [message.content.trim()] : [];
  const append = (asset: z.infer<typeof assetSchema>): void => {
    const previous = assets.findIndex(value => value.kind === asset.kind && value.url === asset.url);
    if (previous === -1) assets.push(asset);
    else if (!asset.is_embed_preview && assets[previous]?.is_embed_preview) assets[previous] = asset;
  };
  for (const match of message.content.matchAll(/https?:\/\/[^\s<>]+/g)) {
    const result = assetSchema.safeParse({ kind: 'link', url: match[0].replace(/[.,);\]」』]+$/, '') });
    if (result.success) append(result.data);
    else console.warn('Invalid plain-text URL excluded', { channelId: message.channelId, messageId: message.id, offset: match.index });
  }
  for (const embed of message.embeds) {
    if (embed.author) parts.push('[Embed Author] ' + embed.author);
    if (embed.url) { parts.push('[Embed URL] ' + embed.url); append(assetSchema.parse({ kind: 'embed', url: embed.url, title: embed.title, description: embed.description })); }
    if (embed.title) parts.push('[Embed Title] ' + embed.title);
    if (embed.description) parts.push('[Embed Description] ' + embed.description);
    for (const field of embed.fields) parts.push(`[Embed Field: ${field.name}] ${field.value}`);
    for (const image of [embed.image, embed.thumbnail]) if (image) append(assetSchema.parse({ kind: 'image', url: image, is_embed_preview: Boolean(embed.url) }));
  }
  for (const attachment of message.attachments) {
    parts.push(`[Attachment] filename=${attachment.filename} content_type=${attachment.contentType ?? 'unknown'} url=${attachment.url}`);
    const contentType = attachment.contentType ?? 'application/octet-stream';
    append(assetSchema.parse({ kind: contentType.toLowerCase().startsWith('image/') ? 'image' : 'attachment', url: attachment.url, title: attachment.filename, mime_type: contentType }));
  }
  return envelopeSchema.parse({ message_id: message.id, channel_id: message.channelId, content: parts.join('\n') || '[Empty Discord message]', created_at: message.createdAt.toISOString(), assets, omitted_asset_count: Math.max(0, assets.length - 50) });
}
const imageHosts = new Set(['pbs.twimg.com', 'media.discordapp.net', 'cdn.discordapp.com', 'images-ext-1.discordapp.net', 'images-ext-2.discordapp.net', 'www.oimachi-tracks.com']);
export async function acquireMessageEnvelope(message: DiscordInput, processor: MessageProcessor): Promise<MessageEnvelope> {
  const envelope = buildMessageEnvelope(message);
  for (const asset of envelope.assets) {
    if (asset.kind !== 'image') continue;
    const host = new URL(asset.url).hostname.toLowerCase().replace(/\.$/, '');
    if (!imageHosts.has(host)) { console.warn('Source image host is not supported', { messageId: envelope.message_id, host }); continue; }
    try { await processor.cacheImage(asset.url); }
    catch (error) { console.error('Initial image acquisition failed', { messageId: envelope.message_id, host, error }); throw error; }
  }
  return envelope;
}
